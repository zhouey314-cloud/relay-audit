"""Local, metadata-only incident storage. Provider labels are untrusted."""

import hashlib
import hmac
import json
import math
import os
import secrets as secret_module
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

RULE_VERSION = "guard-descriptive-v1"
IDENTITY = "unverified"
REQUEST_FIELDS = {
    "request_id", "session_id", "upstream_alias", "protocol", "source", "requested_model",
    "response_models", "http_status", "stream", "termination", "cancel_source", "parse_error",
    "ttfb_ms", "first_output_ms", "total_ms", "usage", "conditions", "phase", "observed_at_utc",
    "finish_reason", "usage_source", "model_label_observable", "protocol_errors", "observation_parse_errors",
}
CONDITION_FIELDS = {
    "reasoning_effort", "thinking_enabled", "output_limit", "tools_present", "input_size_bucket",
    "cache_status", "stream",
}
USAGE_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
    "cached_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "input_tokens_details", "prompt_tokens_details", "output_tokens_details", "completion_tokens_details",
    "reasoning_tokens",
    "cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    "cache_write_tokens", "audio_tokens", "image_tokens", "text_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens",
}
EVIDENCE_FIELDS = REQUEST_FIELDS | CONDITION_FIELDS | USAGE_FIELDS | {
    "reason", "observed_fact", "possible_explanations", "next_step", "baseline_samples", "current_samples",
    "before", "current", "error_rate", "median_ms", "rule_version", "identity_status", "baseline_state",
    "request_ids", "sample_count", "reference_samples", "target_samples", "reference_passes",
    "target_passes", "calibration_samples", "calibration_passes", "calibration_state", "conditions_match",
    "drop", "minimum_drop", "ground_truth_status", "budget_state", "budget_used_hour", "budget_used_day",
    "mode", "requests_forwarded", "requests_observed", "queue_dropped", "incidents", "active_incidents",
    "groups", "samples", "state", "notifier_failures", "probe", "available", "supported", "reference_trusted",
    "extra_requests", "hourly_limit", "daily_limit", "inflight", "enabled", "retention_days", "error_count",
    "fingerprint", "last_observed_at_utc", "status", "recovered", "response_metadata_only",
    "monitoring_state", "monitoring_degraded", "notification", "queue_pending", "uptime_seconds",
    "delivered", "failures", "dropped", "queue_size", "removed_records",
    "baseline_display", "baseline_version", "collection_started_at_utc", "collection_ended_at_utc",
    "parser_queue_pending",
    "inflight_requests", "max_inflight_requests", "capacity_rejected",
    "probe_version", "seed", "families", "probe_id", "passed", "samples_requested",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, state_dir, secrets=()):
        self.state_dir = Path(state_dir)
        if self.state_dir.is_symlink():
            raise ValueError("State directory must not be a symlink")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self._lock = threading.RLock()
        self._secrets = tuple(value for value in secrets if isinstance(value, str) and value)
        self._active = {}
        self._status = {"identity_status": IDENTITY, "baseline_state": "baseline_unknown"}
        key_path = self.state_dir / ".association-key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            if key_path.is_symlink():
                raise ValueError("Association key must not be a symlink")
            self._key = key_path.read_bytes()
        else:
            self._key = secret_module.token_bytes(32)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._key)
        if len(self._key) != 32:
            raise ValueError("Invalid local association key")

    def set_secrets(self, values):
        self._secrets = tuple(set(self._secrets) | {value for value in values if isinstance(value, str) and value})

    def _write_json(self, filename, value):
        path = self.state_dir / filename
        if path.is_symlink():
            raise ValueError("State file must not be a symlink")
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, allow_nan=False)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _read_json(self, filename, default):
        path = self.state_dir / filename
        if not path.exists():
            return default
        if path.is_symlink():
            raise ValueError("State file must not be a symlink")
        return json.loads(path.read_text())

    @contextmanager
    def _probe_lock(self):
        # All workers and Store instances sharing a directory reserve under the
        # same operating-system lock, including after a process restart.
        with self._lock:
            fd = os.open(self.state_dir / ".probe-lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "r+b") as handle:
                try:
                    import fcntl
                except ImportError:
                    import msvcrt
                    handle.write(b"0")
                    handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def reserve_probe(self, hourly_limit, daily_limit, count=1):
        """Reserve before I/O. Stored reservations survive process restart."""
        import time
        with self._probe_lock():
            now = time.time()
            prior = self._read_json("probe-budget.json", [])
            if not isinstance(prior, list) or any(type(item) not in (int, float) or not math.isfinite(item) for item in prior):
                raise ValueError("Invalid persisted probe budget")
            daily = [item for item in prior if item > now - 86400]
            hourly = [item for item in daily if item > now - 3600]
            allowed = (hourly_limit > 0 and daily_limit > 0 and count > 0
                       and len(hourly) + count <= hourly_limit and len(daily) + count <= daily_limit)
            if allowed:
                daily.extend([now] * count)
                self._write_json("probe-budget.json", daily)
            return {"available": allowed, "budget_used_hour": len(hourly) + (count if allowed else 0),
                    "budget_used_day": len(daily), "hourly_limit": hourly_limit, "daily_limit": daily_limit}

    def claim_probe_trigger(self, fingerprint, cooldown_seconds):
        import time
        with self._probe_lock():
            now = time.time()
            key = self.association_digest(fingerprint)
            prior = self._read_json("probe-cooldown.json", {})
            if not isinstance(prior, dict):
                raise ValueError("Invalid persisted probe cooldown")
            if now - prior.get(key, 0) < cooldown_seconds:
                return False
            prior = {item: stamp for item, stamp in prior.items() if now - stamp < max(86400, cooldown_seconds)}
            prior[key] = now
            self._write_json("probe-cooldown.json", prior)
            return True

    def association_digest(self, value):
        return hmac.new(self._key, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    def _text(self, value, limit=200):
        for secret in sorted(set(self._secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return "".join(character for character in value if character >= " " or character == "\n")[:limit]

    def sanitize(self, value, allowed=EVIDENCE_FIELDS, depth=0):
        if depth > 6:
            return None
        if isinstance(value, str):
            return self._text(value)
        if value is None or type(value) is bool:
            return value
        if type(value) in (int, float):
            return value if math.isfinite(value) and abs(value) <= 10**15 else None
        if isinstance(value, list):
            return [self.sanitize(item, allowed, depth + 1) for item in value[:64]]
        if isinstance(value, dict):
            return {key: self.sanitize(item, allowed, depth + 1) for key, item in value.items()
                    if key in allowed}
        return None

    def sanitize_request(self, row):
        safe = self.sanitize(row, REQUEST_FIELDS)
        if isinstance(row.get("conditions"), dict):
            safe["conditions"] = self.sanitize(row["conditions"], CONDITION_FIELDS)
        if isinstance(row.get("usage"), dict):
            safe["usage"] = self.sanitize(row["usage"], USAGE_FIELDS)
        safe["observed_at_utc"] = utc_now()
        return safe

    def _append(self, filename, row):
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        with self._lock:
            fd = os.open(self.state_dir / filename,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                os.chmod(handle.fileno(), 0o600)
                handle.write(encoded)

    def append_request(self, row):
        safe = self.sanitize_request(row)
        self._append("requests.jsonl", safe)
        return safe

    def incident(self, event_type, evidence, session_id="", fingerprint=""):
        if event_type not in {
            "declared_model_mismatch", "protocol_or_capability_error", "service_degradation",
            "behavioral_drift_suspected", "monitoring_degraded", "recovered", "probe_review",
        }:
            raise ValueError("Unknown incident type")
        with self._lock:
            fingerprint = self.association_digest(fingerprint or event_type + str(session_id))
            prior = self._active.get(fingerprint)
            event = {"id": secret_module.token_hex(12), "event_type": event_type,
                     "observed_at_utc": utc_now(), "session_id": self._text(str(session_id), 96),
                     "fingerprint": fingerprint, "occurrences": 1 if prior is None else prior["occurrences"] + 1,
                     "identity_status": IDENTITY, "rule_version": RULE_VERSION,
                     "evidence": self.sanitize(evidence)}
            self._append("events.jsonl", event)
            self._append("incidents.jsonl", event)
            if event_type != "recovered":
                self._active[fingerprint] = event
                if len(self._active) > 1024:
                    self._active.pop(next(iter(self._active)))
            else:
                self._active.pop(fingerprint, None)
            destination = self.state_dir / ("incident-" + event["id"] + ".md")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._markdown([event]))
            return event

    def update_status(self, snapshot):
        safe = self.sanitize(snapshot)
        safe["identity_status"] = IDENTITY
        safe["observed_at_utc"] = utc_now()
        with self._lock:
            self._status = safe
            fd, name = tempfile.mkstemp(prefix=".status-", dir=self.state_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(safe, handle, ensure_ascii=False, allow_nan=False)
                os.replace(name, self.state_dir / "status.json")
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def status(self):
        with self._lock:
            path = self.state_dir / "status.json"
            if path.exists() and not path.is_symlink():
                try:
                    value = json.loads(path.read_text())
                    if isinstance(value, dict):
                        return {**value, "identity_status": IDENTITY}
                except (ValueError, OSError):
                    pass
            return {**self._status, "active_incidents": len(self._active), "identity_status": IDENTITY}

    def read(self, limit=20, kind="incidents"):
        if kind not in {"incidents", "events", "requests"}:
            raise ValueError("Unknown store record type")
        path = self.state_dir / (kind + ".jsonl")
        if not path.exists():
            return []
        if path.is_symlink():
            raise ValueError("Record file must not be a symlink")
        from collections import deque
        rows = deque(maxlen=max(1, min(int(limit), 10000)))
        with self._lock, path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except (ValueError, RecursionError):
                    continue
        return list(rows)

    def _markdown(self, events):
        lines = ["# AgentGuard evidence report", "", "Model identity: **unverified**.",
                 "Descriptive metadata only. Real model identity calibration has not been completed.", ""]
        if not events:
            lines += ["No incidents recorded. This does not certify a model or upstream.", ""]
        for event in events:
            lines += ["## " + event["event_type"], "", "Time: " + event["observed_at_utc"],
                      "Rule version: " + RULE_VERSION, "", "```json",
                      json.dumps(event, ensure_ascii=False, indent=2), "```", "",
                      "Reproduce with the same configured model, protocol and conditions; inspect the affected request IDs.",
                      "Possible explanations include endpoint routing, protocol handling, sampling and request conditions.",
                      "Next step: inspect metadata and, only if explicitly enabled and budgeted, run independent synthetic review.", ""]
        return "\n".join(lines)

    def report(self, destination=None):
        text = self._markdown(self.read(10000))
        if destination is None:
            return text
        path = Path(destination)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return str(path)

    def cleanup(self, retention_days=14):
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))
        removed = 0
        with self._lock:
            for kind in ("requests", "events", "incidents"):
                path = self.state_dir / (kind + ".jsonl")
                if not path.exists():
                    continue
                if path.is_symlink():
                    raise ValueError("Record file must not be a symlink")
                retained = []
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                            observed = datetime.fromisoformat(row["observed_at_utc"])
                            keep = observed >= cutoff
                        except (ValueError, KeyError, TypeError):
                            keep = False
                        if keep:
                            retained.append(line)
                        else:
                            removed += 1
                fd, name = tempfile.mkstemp(prefix=".cleanup-", dir=self.state_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.writelines(retained)
                os.replace(name, path)
            for path in self.state_dir.glob("incident-*.md"):
                if not path.is_symlink() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    path.unlink()
        return {"removed_records": removed, "retention_days": max(1, int(retention_days))}
