import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import public_config
from .probes import PROBE_VERSION, build, canonical, grade
from .report import html_report, markdown, summarize
from .transport import HTTPClient, MockClient, payload


def redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def plan(config):
    probes = build(config["seed"], config["variants"], config.get("families"))
    count = len(probes) * len(config["endpoints"])
    if count > config["max_requests"]:
        raise ValueError(f"Plan needs {count} requests, exceeding max_requests={config['max_requests']}")
    return {
        "planned_requests": count, "probe_count_per_endpoint": len(probes),
        "input_characters_all_requests": sum(len(task.prompt) for task in probes) * len(config["endpoints"]),
        "sum_requested_output_caps": len(probes) * sum(ep["output_limit"] for ep in config["endpoints"]),
        "billing_note": "Character counts are not token counts. Output caps are not a monetary spending cap. "
                        "Provider input, reasoning, caching and pricing rules affect the bill.",
    }


def write_private(path, contents):
    # Exclusive creation: do not follow or overwrite existing files or symlinks.
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(contents)


def run(config, out, save_responses=False, client=None, progress=None):
    planned = plan(config)  # Cost limit checked before reading keys or creating output.
    probes = build(config["seed"], config["variants"], config.get("families"))
    synthetic = all(ep["protocol"] == "mock" for ep in config["endpoints"])
    if any(ep["protocol"] == "mock" for ep in config["endpoints"]) and not synthetic:
        raise ValueError("Do not mix mock and real endpoints in one report")
    keys = {}
    for ep in config["endpoints"]:
        if ep["protocol"] != "mock":
            key = os.environ.get(ep["key_env"], "")
            if not key or any(character in key for character in "\r\n") or not key.isascii():
                raise ValueError("A configured API key environment variable is missing or invalid")
            keys[ep["name"]] = key
    secrets = list(keys.values()) + [ep.get("url", "") for ep in config["endpoints"]]
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    manifest = {"tool_version": __version__, "probe_version": PROBE_VERSION,
                "started_at_utc": started, "synthetic": synthetic,
                "config": public_config(config), "plan": planned,
                "ordering": "seeded_task_shuffle_rotating_endpoints",
                "response_storage": "local_redacted_answers" if save_responses else "hashes_only"}
    write_private(out / "manifest.json", json.dumps(redact(manifest, secrets), ensure_ascii=False, indent=2))
    client = client or (MockClient() if synthetic else HTTPClient())
    rng = random.Random(config["seed"])
    rng.shuffle(probes)
    endpoints = list(config["endpoints"])
    rng.shuffle(endpoints)
    records = []
    status = "complete"
    deadline = time.monotonic() + config["max_wall_seconds"]
    with (out / "results.jsonl").open("x", encoding="utf-8") as journal:
        os.chmod(out / "results.jsonl", 0o600)
        try:
            for task_index, probe in enumerate(probes):
                offset = task_index % len(endpoints)
                for ep in endpoints[offset:] + endpoints[:offset]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = "wall_budget_exhausted"
                        break
                    result = client.call(ep, probe, keys.get(ep["name"], ""),
                                         min(config["timeout_seconds"], remaining))
                    if result.error:
                        passed, reason = None, result.error
                    elif result.grading_error:
                        passed, reason = False, result.grading_error
                    else:
                        passed, reason = grade(probe, result.answer)
                    row = {
                        "endpoint": ep["name"], "probe_id": probe.id, "family": probe.family,
                        "category": probe.category, "probe_digest": probe.digest,
                        "request_digest": hashlib.sha256(canonical(payload(ep, probe)).encode()).hexdigest(),
                        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                        "passed": passed, "grade_reason": reason, "error": result.error,
                        "status_code": result.status_code, "latency_ms": result.latency_ms,
                        "reported_model": result.reported_model, "finish_reason": result.finish_reason,
                        "usage_provider_reported": result.usage,
                        "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest()
                                         if result.answer is not None else None,
                    }
                    if save_responses:
                        row["answer"] = result.answer
                    row = redact(row, secrets)
                    # Redact complete metadata first: clipping an echoed key or URL
                    # can otherwise leave an unmatched secret prefix on disk.
                    for field, limit in (("reported_model", 200), ("finish_reason", 80)):
                        if isinstance(row[field], str):
                            row[field] = row[field][:limit]
                    records.append(row)
                    journal.write(canonical(row) + "\n")
                    journal.flush()
                    if progress:
                        progress(len(records), planned["planned_requests"], ep["name"], reason)
                if status != "complete":
                    break
        except KeyboardInterrupt:
            status = "interrupted"
    summary = summarize(config, records, status, synthetic)
    summary = redact(summary, secrets)
    write_private(out / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    write_private(out / "report.md", markdown(summary))
    write_private(out / "report.html", html_report(summary))
    return summary


def demo_config():
    return {
        "seed": 20261011, "variants": 3, "max_requests": 120, "timeout_seconds": 30,
        "max_wall_seconds": 300,
        "endpoints": [
            {"name": "reference_demo", "role": "reference", "protocol": "mock",
             "model": "claimed-model-demo", "output_limit": 512, "limit_field": "max_tokens",
             "parameters": {}, "mock_mode": "clean"},
            {"name": "relay_demo", "role": "target", "protocol": "mock",
             "model": "claimed-model-demo", "output_limit": 512, "limit_field": "max_tokens",
             "parameters": {}, "mock_mode": "degraded"},
        ],
    }
