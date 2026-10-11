"""Isolated client configuration and content-free lifecycle notifications.

This module can also be executed by absolute filename from a client hook. Hook
payloads (which can contain the complete conversation) are deliberately ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".adapter-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("adapter requires an HTTP loopback gateway URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("gateway URL must not contain credentials, query or fragment")
    path = parsed.path.rstrip("/")
    return url.rstrip("/") if path.endswith("/v1") else url.rstrip("/") + "/v1"


def _install_files(root: Path, files: dict[str, bytes], kind: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    record_dir = root / ".relayguard-adapter"
    if record_dir.is_symlink():
        raise ValueError("refusing symlink adapter record directory")
    record_dir.mkdir(exist_ok=True)
    manifest_path = record_dir / f"{kind}-manifest.json"
    if manifest_path.exists():
        raise ValueError("adapter manifest already exists; detach before attaching again")
    records = []
    for name, content in files.items():
        target = root / name
        if target.is_symlink():
            raise ValueError("refusing symlink configuration target")
        old = target.read_bytes() if target.exists() else None
        backup = record_dir / (name.replace("/", "_") + ".backup")
        if backup.is_symlink():
            raise ValueError("refusing symlink backup")
        if old is not None:
            backup.write_bytes(old)
            backup.chmod(0o600)
        records.append({"path": str(target), "previous_exists": old is not None,
                        "backup_path": str(backup) if old is not None else None,
                        "backup_sha256": _hash(old) if old is not None else None,
                        "installed_sha256": _hash(content)})
    # All targets have been checked before any configuration is changed.
    for name, content in files.items():
        target = root / name
        target.write_bytes(content)
        target.chmod(0o600)
    manifest = {"schema_version": 1, "adapter": kind, "root": str(root),
                "files": records, "identity_status": "unverified"}
    _json_write(manifest_path, manifest)
    return {"manifest_path": str(manifest_path), "configuration_files": [r["path"] for r in records],
            "identity_status": "unverified"}


def attach_codex(*, codex_home: str | Path, guard_url: str, model: str,
                 state_dir: str | Path, local_token_env: str = "RELAY_GUARD_TOKEN",
                 profile: str = "relayguard", python_executable: str | None = None) -> dict:
    """Write a dedicated profile only; never read/copy subscription credentials."""
    root = Path(codex_home).expanduser().resolve()
    protected = (Path.home() / ".codex").resolve()
    if root == protected or protected in root.parents:
        raise ValueError("use a separate CODEX_HOME; the active ~/.codex tree is protected")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("invalid profile name")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", local_token_env):
        raise ValueError("invalid local token environment variable name")
    base_url = _loopback_url(guard_url)
    executable = python_executable or sys.executable
    notify_command = [executable, str(Path(__file__).resolve()), "notify", "--state-dir",
                      str(Path(state_dir).expanduser().resolve()), "--channel", "codex"]
    toml = (f"model = {json.dumps(model)}\nmodel_provider = \"relayguard\"\n"
            'approval_policy = "never"\nsandbox_mode = "read-only"\nweb_search = "disabled"\n'
            f"notify = {json.dumps(notify_command)}\n"
            '[history]\npersistence = "none"\n'
            '[feedback]\nenabled = false\n'
            '[otel]\nmetrics_exporter = "none"\ntrace_exporter = "none"\n'
            '[model_providers.relayguard]\nname = "RelayAudit AgentGuard"\n'
            f"base_url = {json.dumps(base_url)}\nenv_key = {json.dumps(local_token_env)}\n"
            'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n')
    # This tested alpha ignored the profile with --ignore-user-config. Explicit
    # overrides prevent a silent fallback to the built-in public endpoint.
    overrides = {
        "model": json.dumps(model), "model_provider": '"relayguard"',
        "model_providers.relayguard.name": '"RelayAudit AgentGuard"',
        "model_providers.relayguard.base_url": json.dumps(base_url),
        "model_providers.relayguard.env_key": json.dumps(local_token_env),
        "model_providers.relayguard.wire_api": '"responses"',
        "model_providers.relayguard.requires_openai_auth": "false",
        "model_providers.relayguard.supports_websockets": "false",
        "model_providers.relayguard.request_max_retries": "0",
        "model_providers.relayguard.stream_max_retries": "0",
        "notify": json.dumps(notify_command), "history.persistence": '"none"',
        "feedback.enabled": "false", "otel.metrics_exporter": '"none"',
        "otel.trace_exporter": '"none"', "web_search": '"disabled"',
        "project_doc_max_bytes": "0", "project_root_markers": "[]",
        "sandbox_mode": '"read-only"', "approval_policy": '"never"',
    }
    launch_argv = ["codex", "exec", "--ignore-user-config", "--ephemeral", "-p", profile]
    for key, value in overrides.items():
        launch_argv += ["-c", key + "=" + value]
    launcher_name = f"{profile}-launch.py"
    launcher = ("#!/usr/bin/env python3\nimport os,sys\n"
                f"os.environ['CODEX_HOME'] = {str(root)!r}\n"
                f"argv = {launch_argv!r} + sys.argv[1:]\nos.execvp(argv[0], argv)\n")
    result = _install_files(root, {f"{profile}.config.toml": toml.encode(), launcher_name: launcher.encode()}, "codex")
    result.update({"codex_home": str(root), "profile": profile, "transport": "HTTP/SSE",
                   "launch_argv": launch_argv,
                   "launch_command": [executable, str(root / launcher_name)],
                   "launch_env": {"CODEX_HOME": str(root), local_token_env: "<local gateway token>"},
                   "notification_channel": "external notify hook; terminal/system notification"})
    return result


def attach_claude(*, settings_dir: str | Path, guard_url: str, model: str,
                  state_dir: str | Path, python_executable: str | None = None) -> dict:
    """Generate settings for an explicit API route, without reading OAuth state."""
    root = Path(settings_dir).expanduser().resolve()
    protected = (Path.home() / ".claude").resolve()
    if root == protected or protected in root.parents:
        raise ValueError("use a separate settings directory; ~/.claude is protected")
    base_url = _loopback_url(guard_url).removesuffix("/v1")
    executable = python_executable or sys.executable
    command = shlex.join([executable, str(Path(__file__).resolve()), "notify", "--state-dir",
                          str(Path(state_dir).expanduser().resolve()), "--channel", "claude"])
    settings = {"env": {"ANTHROPIC_BASE_URL": base_url, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
                "model": model, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command,
                                                               "timeout": 5}]}]}}
    result = _install_files(root, {"relayguard-settings.json":
                                  (json.dumps(settings, indent=2) + "\n").encode()}, "claude")
    result.update({"launch_argv": ["claude", "--settings", str(root / "relayguard-settings.json"),
                                   "--setting-sources", "", "--no-session-persistence", "-p"],
                   "credential_requirement": "explicit ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN for local gateway",
                   "notification_channel": "Stop hook; external stderr/system notification"})
    return result


def detach(manifest_path: str | Path) -> dict:
    """Restore only when installed files and backups still match their hashes."""
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("adapter") not in {"codex", "claude"}:
        raise ValueError("unsupported adapter manifest")
    root = Path(manifest["root"]).resolve()
    if path.parent != root / ".relayguard-adapter":
        raise ValueError("manifest location does not match installation root")
    checked = []
    for record in manifest["files"]:
        target = Path(record["path"])
        if target.parent.resolve() != root or target.is_symlink():
            raise ValueError("unsafe configuration target")
        if not target.exists() or _hash(target.read_bytes()) != record["installed_sha256"]:
            raise ValueError("configuration changed after attach; preserving user changes")
        old = None
        if record["previous_exists"]:
            backup = Path(record["backup_path"])
            if backup.parent.resolve() != path.parent or backup.is_symlink():
                raise ValueError("unsafe backup target")
            old = backup.read_bytes()
            if _hash(old) != record["backup_sha256"]:
                raise ValueError("backup hash mismatch; refusing restoration")
        checked.append((target, old))
    for target, old in checked:
        if old is None:
            target.unlink()
        else:
            target.write_bytes(old)
            target.chmod(0o600)
    manifest["detached"] = True
    archived = path.with_name(path.name + ".detached")
    _json_write(archived, manifest)
    path.unlink()
    return {"detached": True, "restored_files": len(checked), "manifest_path": str(archived)}


_TITLES = {
    "declared_model_mismatch": "声明模型标签不一致；真实模型身份尚未验证",
    "protocol_or_capability_error": "观察到协议或能力错误；真实模型身份尚未验证",
    "service_degradation": "可比窗口出现服务质量变化；描述性告警",
    "behavioral_drift_suspected": "疑似行为变化；尚未校准模型身份",
    "monitoring_degraded": "监测降级；不能据此判断上游正常",
    "recovery": "已记录恢复事件；真实模型身份尚未验证",
    "recovered": "已记录恢复事件；真实模型身份尚未验证",
}


def _safe_id(value: object) -> str:
    # Never echo arbitrary upstream/model/prompt text through a lifecycle hook.
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _notify_unlocked(state_dir: str | Path, channel: str = "codex", system_notification: bool = False) -> dict:
    """Read new incidents and emit fixed text; ignore argv/stdin hook bodies."""
    if channel not in {"codex", "claude", "terminal", "harness"}:
        raise ValueError("unsupported notification channel")
    root = Path(state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cursor_path = root / f"notify_cursor_{channel}.json"
    if cursor_path.is_symlink() or (root / "incidents.jsonl").is_symlink():
        raise ValueError("refusing symlink notification state")
    cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
    source = root / "incidents.jsonl"
    offset = int(cursor.get("offset", 0))
    count = 0
    system_ok = 0
    if source.exists():
        size = source.stat().st_size
        if size < offset:
            offset = 0
        # Bound one lifecycle invocation. Never read conversation transcripts.
        with source.open("rb") as stream:
            stream.seek(offset)
            for _ in range(200):
                line = stream.readline(65537)
                if not line or not line.endswith(b"\n"):
                    break
                offset = stream.tell()
                if len(line) > 65536:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                kind = event.get("type", event.get("kind", event.get("event_type", "")))
                title = _TITLES.get(kind)
                if title is None:
                    continue
                event_id = _safe_id(event.get("incident_id", event.get("id", "unknown")))
                message = f"[RelayAudit AgentGuard] {title} (incident {event_id})。已保存本地脱敏报告。"
                print(message, file=sys.stderr, flush=True)
                count += 1
                if system_notification and sys.platform == "darwin":
                    try:
                        # The script is static; content is an argv value, never shell source.
                        subprocess.run(["osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "RelayAudit AgentGuard"\nend run', message],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=True)
                        system_ok += 1
                    except (OSError, subprocess.SubprocessError):
                        pass
    _json_write(cursor_path, {"offset": offset})
    receipt = {"timestamp": time.time(), "channel": channel, "hook_called": True,
               "new_incidents": count, "stderr_emitted": count, "system_notifications_submitted": system_ok,
               "hook_payload_saved": False, "identity_status": "unverified"}
    fd = os.open(root / "hook_receipts.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt) + "\n")
    return receipt


def notify(state_dir: str | Path, channel: str = "codex", system_notification: bool = False) -> dict:
    """Serialize cursor updates across concurrent lifecycle processes."""
    if channel not in {"codex", "claude", "terminal", "harness"}:
        raise ValueError("unsupported notification channel")
    root = Path(state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / f".notify-{channel}.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            import fcntl
        except ImportError:
            # Config generation is portable; concurrent Windows hooks are not verified.
            return _notify_unlocked(root, channel, system_notification)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"channel": channel, "hook_called": True, "new_incidents": 0,
                    "deferred_concurrent_hook": True, "hook_payload_saved": False}
        return _notify_unlocked(root, channel, system_notification)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["notify"])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--channel", default="codex")
    parser.add_argument("--system-notification", action="store_true")
    # Codex adds a JSON argv; Claude supplies a JSON stdin. Neither is parsed.
    args, _ignored_hook_payload = parser.parse_known_args(argv)
    try:
        notify(args.state_dir, args.channel, args.system_notification)
        return 0
    except Exception:
        print("[RelayAudit AgentGuard] lifecycle notification failed; monitoring status unknown", file=sys.stderr)
        return 0  # A notification error must not block an Agent's completed turn.


if __name__ == "__main__":
    raise SystemExit(main())
