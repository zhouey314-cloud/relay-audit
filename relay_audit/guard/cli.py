"""AgentGuard lifecycle CLI. Real API calls require explicit environment setup."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from aiohttp import web

from . import __version__
from .config import DEFAULT, load, write_new
from .store import Store


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _base(config):
    host = config["listen_host"]
    if ":" in host:
        host = "[" + host + "]"
    path = Path(config["state_dir"]) / "runtime.json"
    port = config["listen_port"]
    if path.exists():
        info = json.loads(path.read_text())
        port = info.get("port", port)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid local runtime port")
    return f"http://{host}:{port}"


def _control(config, route, method="GET"):
    token = os.environ.get(config["local_token_env"], "")
    if not token:
        raise ValueError("local token environment variable is required")
    request = Request(_base(config) + route, data=b"" if method == "POST" else None,
                      headers={"Authorization": "Bearer " + token}, method=method)
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise URLError("local control redirects are disabled")
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=3) as response:
        return json.load(response)


async def serve(config):
    from .proxy import create_app
    app = create_app(config)
    gateway = app["gateway"]
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True, auto_decompress=False)
    try:
        await runner.setup()
        site = web.TCPSite(runner, config["listen_host"], config["listen_port"])
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        runtime = Path(config["state_dir"]) / "runtime.json"
        if runtime.is_symlink():
            raise ValueError("unsafe runtime file")
        info = {"pid": os.getpid(), "port": port, "started_at": time.time(), "version": __version__}
        fd = os.open(runtime, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(info, handle)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, gateway.stop_event.set)
        print(f"AgentGuard listening on loopback port {port}; mode={config['mode']}; model identity unverified", flush=True)
        await gateway.stop_event.wait()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(gateway.store.update_status, gateway.status())
    finally:
        await runner.cleanup()
        runtime = Path(config["state_dir"]) / "runtime.json"
        with contextlib.suppress(OSError, ValueError):
            if json.loads(runtime.read_text()).get("pid") == os.getpid():
                runtime.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="RelayAudit AgentGuard continuous passive gateway; identity unverified")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--config", default="guard.local.json")
    init.add_argument("--upstream", default=DEFAULT["upstream"]["base_url"])
    init.add_argument("--model", help="Initially allow this exact response label")
    init.add_argument("--port", type=int, default=8765)
    for name in ("start", "stop", "status", "report", "doctor", "backup", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="guard.local.json")
        if name == "start":
            command.add_argument("--background", action="store_true")
        if name in {"report", "backup"}:
            command.add_argument("--out", required=True)
        if name == "doctor":
            command.add_argument("--mock", action="store_true", help="Test a separate loopback-only synthetic streaming chain")
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--out", required=True, help="New configuration path; never overwrites an existing file")
    attach = commands.add_parser("attach-codex")
    attach.add_argument("--config", default="guard.local.json")
    attach.add_argument("--codex-home", required=True)
    attach.add_argument("--model", required=True)
    attach.add_argument("--profile", default="relayguard")
    claude = commands.add_parser("attach-claude")
    claude.add_argument("--config", default="guard.local.json")
    claude.add_argument("--settings-dir", required=True)
    claude.add_argument("--model", required=True)
    detach = commands.add_parser("detach")
    detach.add_argument("--manifest", required=True)
    notify = commands.add_parser("notify")
    notify.add_argument("--state-dir", required=True)
    notify.add_argument("--channel", default="terminal")
    notify.add_argument("--system-notification", action="store_true")
    demo = commands.add_parser("demo")
    demo.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            raw = {"upstream": {"base_url": args.upstream}, "listen_port": args.port}
            if args.model:
                raw["model_mappings"] = {args.model: [args.model]}
            path = write_new(args.config, raw)
            _print({"config": str(path), "mode": "passive", "extra_model_requests": 0,
                    "next_step": "Set RELAY_GUARD_TOKEN and RELAY_UPSTREAM_KEY explicitly, then guard start.",
                    "identity_status": "unverified"})
            return 0
        if args.command == "restore":
            backup = json.loads(Path(args.backup).read_text())
            encoded = json.dumps(backup["config"], sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(encoded).hexdigest() != backup["sha256"]:
                raise ValueError("backup digest mismatch")
            _print({"restored_to": str(write_new(args.out, backup["config"]))})
            return 0
        if args.command in {"notify", "detach"}:
            from . import adapters
            result = adapters.detach(args.manifest) if args.command == "detach" else adapters.notify(args.state_dir, args.channel, args.system_notification)
            _print(result)
            return 0
        if args.command == "demo":
            from .harness import run_demo
            _print(asyncio.run(run_demo(args.out)))
            return 0
        config = load(args.config)
        if args.command == "start":
            if args.background:
                # Check all credentials before spawning; do not save their values.
                for field in (config["local_token_env"], config["upstream"]["key_env"]):
                    if not os.environ.get(field):
                        raise ValueError("required environment variable missing")
                state = Path(config["state_dir"])
                state.mkdir(parents=True, mode=0o700, exist_ok=True)
                log = state / "gateway-terminal.log"
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "ab", buffering=0) as stderr:
                    process = subprocess.Popen([sys.executable, "-m", "relay_audit", "guard", "start", "--config", str(Path(args.config).resolve())],
                                               stdout=stderr, stderr=stderr, start_new_session=True)
                time.sleep(.5)
                if process.poll() is not None:
                    raise ValueError("background gateway failed to start; inspect local terminal log")
                _print({"pid": process.pid, "terminal_log": str(log), "identity_status": "unverified"})
            else:
                asyncio.run(serve(config))
            return 0
        if args.command == "stop":
            _print(_control(config, "/__guard/stop", "POST"))
            return 0
        if args.command == "status":
            try:
                _print({**_control(config, "/__guard/status"), "gateway_reachable": True})
            except (URLError, OSError, ValueError):
                _print({**Store(config["state_dir"]).status(), "gateway_reachable": False,
                        "monitoring_state": "stopped_or_unreachable", "identity_status": "unverified"})
            return 0
        if args.command == "report":
            out = Path(args.out)
            if out.exists():
                raise ValueError("report destination must be new")
            _print({"report": str(Store(config["state_dir"]).report(out)), "identity_status": "unverified"})
            return 0
        if args.command == "backup":
            out = Path(args.out)
            content = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"schema_version": 1, "config": config, "sha256": hashlib.sha256(content).hexdigest()}, handle, indent=2)
            _print({"backup": str(out.resolve()), "contains_keys": False})
            return 0
        if args.command == "cleanup":
            _print(Store(config["state_dir"]).cleanup(config["retention_days"]))
            return 0
        if args.command == "doctor":
            with socket.socket(socket.AF_INET6 if ":" in config["listen_host"] else socket.AF_INET) as probe_socket:
                try:
                    probe_socket.bind((config["listen_host"], config["listen_port"]))
                    port_available = True
                except OSError:
                    port_available = False
            result = {"version": __version__, "configuration": "valid", "loopback_only": True,
                    "port_available": port_available,
                    "supported_protocols": ["responses", "chat_completions", "anthropic_messages"],
                    "transport": "JSON / HTTP SSE; WebSocket unsupported",
                    "credential_env_present": {field: bool(os.environ.get(field)) for field in (config["local_token_env"], config["upstream"]["key_env"])},
                    "codex": shutil.which("codex"), "claude": shutil.which("claude"),
                    "upstream_test": "NOT_RUN", "stream_test": "NOT_RUN", "extra_model_requests": 0, "identity_status": "unverified"}
            if args.mock:
                from .harness import run_demo
                with tempfile.TemporaryDirectory() as directory:
                    summary = asyncio.run(run_demo(Path(directory) / "demo"))
                result["stream_test"] = "PASS" if summary["continued_after_alert"] else "FAIL"
                result["stream_test_source"] = "synthetic_unverified_loopback"
            _print(result)
            return 0
        from . import adapters
        if args.command == "attach-codex":
            result = adapters.attach_codex(codex_home=args.codex_home, guard_url=_base(config), model=args.model,
                                          state_dir=config["state_dir"], local_token_env=config["local_token_env"], profile=args.profile)
        else:
            result = adapters.attach_claude(settings_dir=args.settings_dir, guard_url=_base(config), model=args.model, state_dir=config["state_dir"])
        _print(result)
        return 0
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
        print(f"AgentGuard stopped safely ({type(exc).__name__}); check local configuration and environment. No exception payload saved.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
