import json
import re
from pathlib import Path
from urllib.parse import urlsplit

PARAMETERS = {"temperature", "top_p", "seed", "reasoning_effort", "thinking"}


def integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field} must be an integer in [{low}, {high}]")
    return value


def validate(data, allow_local_http=False):
    data = json.loads(json.dumps(data, allow_nan=False))
    integer(data.setdefault("seed", 20261011), 0, 2**32 - 1, "seed")
    integer(data.setdefault("variants", 3), 1, 100, "variants")
    integer(data.setdefault("max_requests", 120), 1, 10000, "max_requests")
    integer(data.setdefault("timeout_seconds", 30), 1, 60, "timeout_seconds")
    integer(data.setdefault("max_wall_seconds", 300), 1, 3600, "max_wall_seconds")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 8:
        raise ValueError("Provide 1 to 8 endpoints")
    names = set()
    references = 0
    for ep in endpoints:
        allowed = {"name", "role", "protocol", "url", "key_env", "model", "output_limit",
                   "limit_field", "parameters", "mock_mode"}
        if not isinstance(ep, dict) or set(ep) - allowed:
            raise ValueError("Unknown endpoint fields; plaintext keys and custom auth headers are forbidden")
        name = ep.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or name in names:
            raise ValueError("Endpoint names must be unique short aliases")
        names.add(name)
        if ep.setdefault("role", "target") not in {"reference", "target"}:
            raise ValueError("role must be reference or target")
        references += ep["role"] == "reference"
        if ep.get("protocol") not in {"chat_completions", "anthropic_messages", "mock"}:
            raise ValueError("Unsupported protocol")
        if ep["protocol"] != "mock":
            url = ep.get("url", "")
            if not isinstance(url, str):
                raise ValueError("url must be a string")
            parts = urlsplit(url)
            local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
            if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("Use a URL without credentials, query, or fragment")
            if parts.scheme != "https" and not (allow_local_http and local and parts.scheme == "http"):
                raise ValueError("HTTPS required; local HTTP needs --allow-local-http")
            key_env = ep.get("key_env", "")
            if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
                raise ValueError("Specify an environment variable name, never a literal API key")
        if not isinstance(ep.get("model"), str) or not ep["model"]:
            raise ValueError("Specify the claimed model or snapshot for each endpoint")
        integer(ep.setdefault("output_limit", 512), 16, 32768, "output_limit")
        if ep.setdefault("limit_field", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported output limit field")
        params = ep.setdefault("parameters", {})
        if not isinstance(params, dict) or set(params) - PARAMETERS:
            raise ValueError("Unsupported parameters; see config documentation")
    if references > 1:
        raise ValueError("Use at most one user-supplied reference endpoint")
    families = data.get("families")
    if families is not None and (not isinstance(families, list) or not families or
                                 len(set(families)) != len(families)):
        raise ValueError("families must be a nonempty list of unique probe names")
    return data


def load(path, allow_local_http=False):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")), allow_local_http)


def conditions(ep):
    return {key: ep[key] for key in ("protocol", "output_limit", "limit_field", "parameters")}


def public_config(data):
    return {
        "seed": data["seed"], "variants": data["variants"],
        "max_requests": data["max_requests"], "timeout_seconds": data["timeout_seconds"],
        "max_wall_seconds": data["max_wall_seconds"], "families": data.get("families"),
        "endpoints": [{"name": ep["name"], "role": ep["role"], "model": ep["model"],
                       **conditions(ep)} for ep in data["endpoints"]],
    }
