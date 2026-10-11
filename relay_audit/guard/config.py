"""Strict local gateway configuration. Configuration never contains API keys."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

DEFAULT = {
    "schema_version": 1,
    "listen_host": "127.0.0.1", "listen_port": 8765,
    "upstream": {"alias": "relay", "base_url": "https://relay.example.invalid/v1",
                 "key_env": "RELAY_UPSTREAM_KEY", "allow_local_http": False},
    "local_token_env": "RELAY_GUARD_TOKEN", "state_dir": "guard-state",
    "mode": "passive", "model_mappings": {},
    "request_timeout_seconds": 300, "retention_days": 14,
    "observation": {"enabled": True, "queue_size": 256, "max_event_bytes": 65536,
                    "detector_timeout_seconds": 2},
    "detector": {"min_samples": 20, "window": 20, "error_rate_delta": 0.25,
                 "latency_multiplier": 2.0, "cooldown_seconds": 60},
    "notifications": {"terminal": True, "system": False, "queue_size": 64},
    "probe": {"enabled": False, "hourly_limit": 0, "daily_limit": 0,
              "concurrency": 1, "cooldown_seconds": 300, "min_samples": 3,
              "calibration_samples": 6, "minimum_drop": 0.35, "timeout_seconds": 20,
              "families": ["modular", "ledger", "json_schema"], "target": None, "reference": None},
}


def _merge(base, supplied):
    result = copy.deepcopy(base)
    for key, value in supplied.items():
        if key not in base:
            raise ValueError("unknown configuration field")
        if isinstance(base[key], dict) and key != "model_mappings":
            if not isinstance(value, dict):
                raise ValueError("invalid configuration object")
            result[key] = _merge(base[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def endpoint(url, allow_local_http=False):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid upstream endpoint")
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint cannot contain credentials, query or fragment")
    if parsed.scheme != "https":
        if not (allow_local_http and parsed.scheme == "http" and
                parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("HTTPS required except explicit loopback mock")
    if ".." in parsed.path.split("/") or "\\" in url or any(ord(c) < 32 for c in url):
        raise ValueError("invalid endpoint path")
    _ = parsed.port
    return url.rstrip("/")


def _number(value, low, high, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
        raise ValueError("invalid numeric configuration")


def validate(raw):
    if not isinstance(raw, dict):
        raise ValueError("configuration must be an object")
    value = _merge(DEFAULT, raw)
    if value["schema_version"] != 1 or value["listen_host"] not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("only loopback binding supported")
    _number(value["listen_port"], 0, 65535, True)
    for field in (value["local_token_env"], value["upstream"]["key_env"]):
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise ValueError("credentials must name environment variables")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["upstream"]["alias"]):
        raise ValueError("invalid upstream alias")
    if type(value["upstream"]["allow_local_http"]) is not bool:
        raise ValueError("invalid HTTP policy")
    value["upstream"]["base_url"] = endpoint(value["upstream"]["base_url"], value["upstream"]["allow_local_http"])
    if value["mode"] not in {"passive", "verified_probe"}:
        raise ValueError("invalid mode")
    if not isinstance(value["state_dir"], str) or not value["state_dir"]:
        raise ValueError("invalid local state directory")
    mappings = value["model_mappings"]
    if not isinstance(mappings, dict) or len(mappings) > 1000:
        raise ValueError("invalid model mapping")
    for model, allowed in mappings.items():
        if not isinstance(model, str) or not 1 <= len(model) <= 200 or not isinstance(allowed, list) or not allowed:
            raise ValueError("invalid model mapping")
        if len(allowed) > 32 or any(not isinstance(label, str) or not 1 <= len(label) <= 200 for label in allowed):
            raise ValueError("invalid response label")
    _number(value["request_timeout_seconds"], 1, 3600)
    _number(value["retention_days"], 1, 3650, True)
    for key, low, high in (("queue_size", 1, 100000), ("max_event_bytes", 1024, 4*1024*1024)):
        _number(value["observation"][key], low, high, True)
    _number(value["observation"]["detector_timeout_seconds"], .05, 60)
    for flag in (value["observation"]["enabled"], value["notifications"]["terminal"], value["notifications"]["system"], value["probe"]["enabled"]):
        if type(flag) is not bool:
            raise ValueError("invalid boolean configuration")
    _number(value["notifications"]["queue_size"], 1, 10000, True)
    for key in ("min_samples", "window"):
        _number(value["detector"][key], 3, 10000, True)
    _number(value["detector"]["error_rate_delta"], .05, 1)
    _number(value["detector"]["latency_multiplier"], 1.1, 100)
    _number(value["detector"]["cooldown_seconds"], 0, 86400)
    probe = value["probe"]
    for key, low, high in (("hourly_limit", 0, 10000), ("daily_limit", 0, 100000),
                          ("concurrency", 1, 4), ("min_samples", 3, 100), ("calibration_samples", 3, 100)):
        _number(probe[key], low, high, True)
    _number(probe["cooldown_seconds"], 0, 86400)
    _number(probe["minimum_drop"], .1, 1)
    _number(probe["timeout_seconds"], 1, 120)
    if not isinstance(probe["families"], list) or not probe["families"]:
        raise ValueError("probe families required")
    if probe["enabled"] and (value["mode"] != "verified_probe" or not probe["hourly_limit"] or not probe["daily_limit"] or not probe["target"]):
        raise ValueError("enabled probes require explicit mode, target and positive budgets")
    for target in (probe["target"], probe["reference"]):
        if target is None:
            continue
        if not isinstance(target, dict) or not {"url", "protocol", "model", "key_env", "snapshot_id"} <= target.keys():
            raise ValueError("explicit probe route and snapshot required")
        allowed_fields = {"url", "protocol", "model", "key_env", "snapshot_id", "parameters",
                          "output_limit", "limit_field", "trusted", "allow_local_http"}
        if set(target) - allowed_fields:
            raise ValueError("unsupported probe endpoint field; credentials must use environment variables")
        endpoint(target["url"], bool(target.get("allow_local_http", False)))
        if target["protocol"] not in {"responses", "chat_completions", "anthropic_messages"}:
            raise ValueError("invalid probe protocol")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target["key_env"]):
            raise ValueError("invalid probe credential variable")
        if not isinstance(target["model"], str) or not 1 <= len(target["model"]) <= 200:
            raise ValueError("invalid probe model")
        if not isinstance(target["snapshot_id"], str) or not 1 <= len(target["snapshot_id"]) <= 200:
            raise ValueError("explicit snapshot required")
        if "trusted" in target and type(target["trusted"]) is not bool:
            raise ValueError("invalid reference trust policy")
        if "allow_local_http" in target and type(target["allow_local_http"]) is not bool:
            raise ValueError("invalid probe HTTP policy")
        _number(target.get("output_limit", 512), 16, 32768, True)
        if target.get("limit_field", "max_tokens") not in {"max_tokens", "max_completion_tokens", "max_output_tokens"}:
            raise ValueError("invalid probe limit field")
        parameters = target.get("parameters", {})
        if not isinstance(parameters, dict) or set(parameters) - {"temperature", "top_p", "seed", "reasoning_effort", "thinking", "reasoning"}:
            raise ValueError("invalid probe parameters")
        for key in ("temperature", "top_p"):
            if key in parameters:
                _number(parameters[key], 0, 2 if key == "temperature" else 1)
        if "seed" in parameters:
            _number(parameters["seed"], 0, 2**32-1, True)
        efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if "reasoning_effort" in parameters and (not isinstance(parameters["reasoning_effort"], str) or parameters["reasoning_effort"] not in efforts):
            raise ValueError("invalid reasoning effort")
        if "reasoning" in parameters:
            reasoning = parameters["reasoning"]
            if not isinstance(reasoning, dict) or set(reasoning) - {"effort"} or not isinstance(reasoning.get("effort"), str) or reasoning["effort"] not in efforts:
                raise ValueError("invalid reasoning object")
        if "thinking" in parameters:
            thinking = parameters["thinking"]
            if not isinstance(thinking, dict) or set(thinking) - {"type", "budget_tokens"} or thinking.get("type") not in {"enabled", "disabled", "adaptive"}:
                raise ValueError("invalid thinking object")
            if "budget_tokens" in thinking:
                _number(thinking["budget_tokens"], 1, 32768, True)
    return value


def load(path):
    path = Path(path).expanduser().resolve()
    value = validate(json.loads(path.read_text(encoding="utf-8")))
    state = Path(value["state_dir"]).expanduser()
    value["state_dir"] = str((path.parent / state).resolve() if not state.is_absolute() else state.resolve())
    return value


def write_new(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(validate(value), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path
