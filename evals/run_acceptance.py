#!/usr/bin/env python3
"""Repeatable, loopback-only release gates; no external model API calls.

Example: python evals/run_acceptance.py --out work/acceptance-new --requests 100
Use --clients explicitly to also run installed Codex and Claude clients against
the existing isolated, fake-key launchers. Mock success never verifies identity.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import statistics
import sys
import time
import tracemalloc

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import aiohttp
from aiohttp import web

from relay_audit.guard.harness import check_local_agent, run_demo
from relay_audit.guard.proxy import create_app

WARMUPS = 10
MODEL = "synthetic-benchmark-model"
LOCAL_ENV = "RELAY_ACCEPTANCE_BENCHMARK_LOCAL_TOKEN"
UPSTREAM_ENV = "RELAY_ACCEPTANCE_BENCHMARK_UPSTREAM_KEY"
LOCAL_TOKEN = "fake-loopback-benchmark-local-token"
UPSTREAM_KEY = "fake-loopback-benchmark-upstream-key"


class GateError(Exception):
    """Only fixed gate names, never provider or exception body text."""


def save_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(checks: dict, name: str, condition: bool) -> None:
    checks[name] = bool(condition)
    if not condition:
        raise GateError(name)


@contextmanager
def fake_environment():
    previous = {name: os.environ.get(name) for name in (LOCAL_ENV, UPSTREAM_ENV)}
    os.environ[LOCAL_ENV] = LOCAL_TOKEN
    os.environ[UPSTREAM_ENV] = UPSTREAM_KEY
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def serve(app: web.Application):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
    except BaseException:
        sock.close()
        await runner.cleanup()
        raise
    return runner, f"http://127.0.0.1:{port}"


def fixtures():
    """Fixed synthetic JSON and SSE entities, IDs, usage and chunk schedule."""
    text = "SYNTHETIC_BENCHMARK_OUTPUT 你好🙂 " * 64
    usage = {"prompt_tokens": 10, "completion_tokens": 64, "total_tokens": 74,
             "prompt_tokens_details": {"cached_tokens": 3}}
    completed = {"id": "chatcmpl_synthetic_benchmark", "object": "chat.completion",
                 "created": 0, "model": MODEL,
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                              "finish_reason": "stop"}], "usage": usage}
    chunk = {"id": completed["id"], "object": "chat.completion.chunk", "created": 0, "model": MODEL}

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def event(value):
        return b"data: " + encoded(value) + b"\r\n\r\n"

    sse = [event({**chunk, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})]
    for start in range(0, len(text), 256):
        sse.append(event({**chunk, "choices": [{"index": 0, "delta": {"content": text[start:start + 256]},
                                               "finish_reason": None}]}))
    sse.extend([event({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                event({**chunk, "choices": [], "usage": usage}), b"data: [DONE]\r\n\r\n"])
    return {"chat_json": [encoded(completed)], "chat_sse": sse}


def benchmark_upstream(payloads: dict):
    app = web.Application()
    state = {"requests": 0}

    async def reply(request):
        state["requests"] += 1
        if request.headers.get("Authorization") != "Bearer " + UPSTREAM_KEY:
            return web.Response(status=401)
        body = await request.json()
        pieces = payloads["chat_sse" if body.get("stream") else "chat_json"]
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream" if body.get("stream") else "application/json"})
        await response.prepare(request)
        for piece in pieces:
            await response.write(piece)
        await response.write_eof()
        return response

    app.router.add_post("/v1/chat/completions", reply)
    return app, state


def distribution(values):
    ordered = sorted(values)
    return {"median": statistics.median(ordered), "p95": ordered[math.ceil(.95 * len(ordered)) - 1],
            "min": ordered[0], "max": ordered[-1]}


async def receive(session, url, body: bytes, token: str, expected_digest: str, expected_bytes: int):
    started = time.perf_counter()
    first = None
    digest = hashlib.sha256()
    size = chunks = 0
    async with session.post(url, data=body, headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"}) as response:
        if response.status != 200:
            raise GateError("benchmark_http_200")
        async for part in response.content.iter_any():
            if first is None:
                first = time.perf_counter()
            digest.update(part)
            size += len(part)
            chunks += 1
    ended = time.perf_counter()
    if first is None or size != expected_bytes or digest.hexdigest() != expected_digest:
        raise GateError("benchmark_complete_unchanged_entity")
    return {"ttfb_ms": (first - started) * 1000, "total_ms": (ended - started) * 1000,
            "response_bytes": size, "received_chunks": chunks}


async def drained(gateway):
    if gateway is not None:
        await asyncio.wait_for(gateway.flush(), 30)
        await asyncio.wait_for(gateway.notifier.queue.join(), 30)


async def measure_mode(out: Path, upstream_url: str, upstream_state: dict, workload: str,
                       pieces: list[bytes], mode: str, requests: int, concurrency: int = 1):
    state_dir = out / "benchmark-state" / workload / mode
    state_dir.mkdir(parents=True, exist_ok=False)
    guard_runner = gateway = None
    expected = b"".join(pieces)
    expected_digest = hashlib.sha256(expected).hexdigest()
    body = json.dumps({"model": MODEL, "stream": workload == "chat_sse",
                       "messages": [{"role": "user", "content": "synthetic benchmark request"}],
                       "max_tokens": 128}, separators=(",", ":")).encode()
    before = upstream_state["requests"]
    with fake_environment():
        try:
            url = upstream_url + "/v1/chat/completions"
            token = UPSTREAM_KEY
            if mode != "direct":
                guard = create_app({"listen_port": 0, "state_dir": str(state_dir), "mode": "passive",
                                    "local_token_env": LOCAL_ENV,
                                    "upstream": {"base_url": upstream_url + "/v1", "key_env": UPSTREAM_ENV,
                                                 "alias": "synthetic-benchmark", "allow_local_http": True},
                                    "observation": {"enabled": mode == "observation_enabled", "queue_size": 4096},
                                    "notifications": {"terminal": False, "system": False}})
                gateway = guard["gateway"]
                guard_runner, guard_url = await serve(guard)
                url = guard_url + "/v1/chat/completions"
                token = LOCAL_TOKEN
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
                                             connector=aiohttp.TCPConnector(limit=concurrency)) as session:
                for _ in range(WARMUPS):
                    await receive(session, url, body, token, expected_digest, len(expected))
                await drained(gateway)
                warm_upstream = upstream_state["requests"] - before
                tracemalloc.start()
                cpu_started = time.process_time()
                wall_started = time.perf_counter()
                try:
                    # Batches bound both client concurrency and retained task objects.
                    samples = []
                    for offset in range(0, requests, concurrency):
                        samples.extend(await asyncio.gather(*(
                            receive(session, url, body, token, expected_digest, len(expected))
                            for _ in range(min(concurrency, requests - offset)))))
                    # Include all parser/detector/notification CPU, rather than measuring
                    # only work that happens to finish before the final network byte.
                    await drained(gateway)
                    cpu_seconds = time.process_time() - cpu_started
                    wall_seconds = time.perf_counter() - wall_started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            status = gateway.status() if gateway is not None else None
            upstream_requests = upstream_state["requests"] - before
            total = WARMUPS + requests
            checks = {}
            require(checks, "warmup_upstream_equals_agent", warm_upstream == WARMUPS)
            require(checks, "passive_upstream_equals_agent", upstream_requests == total)
            require(checks, "extra_model_requests_zero", upstream_requests - total == 0)
            if status is not None:
                require(checks, "requests_forwarded_equals_agent", status["requests_forwarded"] == total)
                require(checks, "no_queue_drops", status["queue_dropped"] == 0)
                require(checks, "queues_drained", status["queue_pending"] == status["parser_queue_pending"] == 0)
                require(checks, "probe_extra_requests_zero", status["probe"].get("extra_requests") == 0)
                if mode == "observation_enabled":
                    require(checks, "all_requests_observed", status["requests_observed"] == total)
                    require(checks, "monitoring_observing", status["monitoring_state"] == "observing")
                else:
                    require(checks, "observation_disabled_explicit", "observation_disabled" in status["monitoring_degraded"])
            return {"workload": workload, "mode": mode, "status": "PASS", "checks": checks,
                    "warmup_requests": WARMUPS, "measured_requests": requests, "concurrency": concurrency,
                    "completed_requests": len(samples), "error_requests": 0, "error_rate": 0,
                    "throughput_requests_per_second_including_queue_drain": requests / wall_seconds,
                    "agent_requests_including_warmup": total, "upstream_requests": upstream_requests,
                    "extra_model_requests": upstream_requests - total, "queue_dropped": status["queue_dropped"] if status else 0,
                    "request_payload_bytes": len(body), "response_payload_bytes": len(expected),
                    "response_sha256": expected_digest, "upstream_write_chunks_per_response": len(pieces),
                    "received_chunks": distribution([s["received_chunks"] for s in samples]),
                    "ttfb_ms": distribution([s["ttfb_ms"] for s in samples]),
                    "total_ms": distribution([s["total_ms"] for s in samples]),
                    "process_cpu_seconds_including_queue_drain": cpu_seconds,
                    "measured_batch_wall_seconds_including_queue_drain": wall_seconds,
                    "tracemalloc_peak_bytes": peak,
                    "samples": samples, "gateway_status": status}
        finally:
            if guard_runner is not None:
                await guard_runner.cleanup()


async def benchmark(out: Path, requests: int, concurrency: int = 1):
    payloads = fixtures()
    upstream, state = benchmark_upstream(payloads)
    runner, url = await serve(upstream)
    result = {"schema_version": 1, "evidence_status": "synthetic_unverified", "identity_status": "unverified",
              "real_api": "NOT_RUN", "real_api_verified": False,
              "environment": {"python": platform.python_version(), "system": platform.system(),
                              "release": platform.release(), "machine": platform.machine(), "aiohttp": aiohttp.__version__},
              "conditions": {"upstream": "same_process_ipv4_loopback", "protocol": "chat_completions",
                             "fixed_synthetic_entities": True, "artificial_delay_seconds": 0,
                             "warmup_per_mode_workload": WARMUPS, "measured_requests_per_mode_workload": requests,
                             "concurrency": concurrency, "scheduling": "bounded batches; next batch after previous receives EOF",
                             "errors": "any HTTP, incomplete entity, timeout or digest failure aborts the gate; no retries",
                             "fresh_mode_state_and_fake_credentials": True,
                             "observation_queue_capacity": 4096,
                             "observation_max_event_bytes": 65536,
                             "detector_timeout_seconds": 2,
                             "mode_order": ["direct", "observation_disabled", "observation_enabled"],
                             "ttfb_definition": "request start to first nonempty received HTTP entity chunk; not first visible text",
                             "total_definition": "request start through complete HTTP entity reception and EOF",
                             "p95_method": "nearest rank ceil(0.95 * count)",
                             "cpu_scope": "whole Python process: client, loopback upstream, proxy, parser, detector and notification queue drain",
                             "memory_scope": "Python tracemalloc allocations during measured requests and queue drain; not RSS",
                             "tracemalloc_enabled_for_all_modes": True,
                             "latency_gate": "descriptive_only_no_environment_independent_threshold"}, "modes": []}
    try:
        for workload, pieces in payloads.items():
            for mode in result["conditions"]["mode_order"]:
                print(f"Benchmark {workload} / {mode}: {requests} requests, concurrency {concurrency}", flush=True)
                result["modes"].append(await measure_mode(out, url, state, workload, pieces, mode, requests, concurrency))
                save_json(out / "benchmark.json", result)
        result["upstream_requests_total"] = state["requests"]
        result["expected_upstream_requests_total"] = 6 * (WARMUPS + requests)
        result["status"] = "PASS"
        result["comparisons"] = []
        for workload in payloads:
            modes = {item["mode"]: item for item in result["modes"] if item["workload"] == workload}
            for name in ("observation_disabled", "observation_enabled"):
                result["comparisons"].append({"workload": workload, "mode": name,
                    "ttfb_median_delta_ms_vs_direct": modes[name]["ttfb_ms"]["median"] - modes["direct"]["ttfb_ms"]["median"],
                    "ttfb_p95_delta_ms_vs_direct": modes[name]["ttfb_ms"]["p95"] - modes["direct"]["ttfb_ms"]["p95"],
                    "total_median_delta_ms_vs_direct": modes[name]["total_ms"]["median"] - modes["direct"]["total_ms"]["median"],
                    "total_p95_delta_ms_vs_direct": modes[name]["total_ms"]["p95"] - modes["direct"]["total_ms"]["p95"]})
        return result
    except BaseException:
        result["status"] = "FAIL"
        raise
    finally:
        save_json(out / "benchmark.json", result)
        await runner.cleanup()


async def unit_tests(out: Path):
    argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    process = await asyncio.create_subprocess_exec(*argv, cwd=REPO, stdin=asyncio.subprocess.DEVNULL,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    with (out / "tests.log").open("wb") as log:
        while data := await process.stdout.read(65536):
            log.write(data)
    await process.wait()
    text = (out / "tests.log").read_text(encoding="utf-8", errors="replace")
    counts = re.findall(r"^Ran (\d+) tests? in ([\d.]+)s", text, re.MULTILINE)
    count, seconds = (int(counts[-1][0]), float(counts[-1][1])) if counts else (0, None)
    report_lines = re.findall(r"^(?:OK|FAILED)(?: \((.*)\))?$", text, re.MULTILINE)
    details = {key: int(value) for key, value in re.findall(
        r"\b(failures|errors|skipped|expected failures|unexpected successes)=(\d+)",
        report_lines[-1] if report_lines else "")}
    result = {"status": "PASS" if process.returncode == 0 and count > 0 else "FAIL",
              "exit_code": process.returncode, "tests_run": count, "runner_seconds": seconds,
              "failures": details.get("failures", 0), "errors": details.get("errors", 0),
              "skipped": details.get("skipped", 0), "expected_failures": details.get("expected failures", 0),
              "unexpected_successes": details.get("unexpected successes", 0),
              "command": "python -m unittest discover -s tests -v", "log": "tests.log"}
    save_json(out / "tests.json", result)
    return result


async def acceptance(args):
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    summary = {"schema_version": 1, "status": "RUNNING", "evidence_status": "synthetic_unverified",
               "identity_status": "unverified", "real_api": "NOT_RUN", "real_api_verified": False,
               "identity_calibration": "NOT_RUN", "clients": {"status": "NOT_RUN", "reason": "explicit_flag_required"},
               "checks": {}}
    stage = "traditional_tests"
    try:
        print("Running complete unittest release gate; output is saved in tests.log", flush=True)
        summary["tests"] = await unit_tests(out)
        require(summary["checks"], "complete_test_suite_pass", summary["tests"]["status"] == "PASS")
        print(f"Complete unittest gate: {summary['tests']['tests_run']} tests passed", flush=True)
        stage = "synthetic_demo"
        demo = await run_demo(out / "demo")
        summary["demo"] = {"summary": "demo/summary.json", "report": "demo/report.md", "checks": {}}
        checks = summary["demo"]["checks"]
        require(checks, "three_agent_requests", demo["agent_requests"] == 3)
        require(checks, "passive_three_upstream_requests", demo["upstream_requests"] == 3)
        require(checks, "extra_model_requests_zero", demo["extra_model_requests"] == 0 and
                demo["status"]["probe"].get("extra_requests") == 0)
        require(checks, "automatic_label_incident", "declared_model_mismatch" in demo["incident_types"])
        require(checks, "automatic_notification", demo["notification"].get("delivered", 0) > 0)
        require(checks, "hook_received_incident", demo["hook_receipt"].get("new_incidents", 0) > 0)
        require(checks, "report_created", (out / "demo" / "report.md").is_file() and (out / "demo" / "report.md").stat().st_size > 0)
        require(checks, "continued_after_alert", demo["continued_after_alert"] and all(o["http_status"] == 200 for o in demo["observations"]))
        require(checks, "demo_queue_drops_zero", demo["status"]["queue_dropped"] == 0)
        stage = "loopback_benchmark"
        measured = await benchmark(out, args.requests, args.concurrency)
        summary["benchmark"] = {"status": measured["status"], "artifact": "benchmark.json", "modes": len(measured["modes"]),
                                "requests_per_mode_workload": args.requests, "upstream_requests": measured["upstream_requests_total"]}
        require(summary["checks"], "six_benchmark_modes_pass", measured["status"] == "PASS" and len(measured["modes"]) == 6)
        require(summary["checks"], "benchmark_passive_exact_request_count",
                measured["upstream_requests_total"] == measured["expected_upstream_requests_total"])
        if args.clients:
            stage = "isolated_installed_clients"
            summary["clients"] = {"status": "RUNNING", "results": []}
            for agent in ("codex", "claude"):
                print(f"Running isolated installed {agent} client against loopback mock", flush=True)
                client = await check_local_agent(out / agent, agent)
                summary["clients"]["results"].append({"agent": agent, "status": client["status"], "artifact": agent + "/summary.json"})
                require(summary["checks"], agent + "_isolated_client_pass", client["status"] == "PASS")
            summary["clients"]["status"] = "PASS"
        summary["status"] = "PASS"
        print("Release harness PASS; synthetic mechanics only, real API and identity calibration NOT_RUN", flush=True)
        return 0
    except Exception as error:
        summary["status"] = "FAIL"
        summary["failed_stage"] = stage
        summary["failed_gate"] = str(error) if isinstance(error, GateError) else "unexpected_" + type(error).__name__
        print(f"Release harness FAIL at {stage}; see saved results", file=sys.stderr, flush=True)
        return 1
    finally:
        save_json(out / "summary.json", summary)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new output directory; existing paths are refused")
    parser.add_argument("--requests", type=int, default=100, help="measured requests per workload/mode (default: 100)")
    parser.add_argument("--concurrency", type=int, default=1, help="bounded benchmark concurrency (default: 1; max: 128)")
    parser.add_argument("--clients", action="store_true", help="explicitly run installed Codex and Claude clients against loopback mock")
    args = parser.parse_args()
    if not 1 <= args.requests <= 10000:
        parser.error("--requests must be between 1 and 10000")
    if not 1 <= args.concurrency <= 128:
        parser.error("--concurrency must be between 1 and 128")
    if args.out.expanduser().exists():
        parser.error("--out must be a new directory")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(acceptance(parse_args())))
