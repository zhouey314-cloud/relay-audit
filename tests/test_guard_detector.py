"""Local synthetic contract tests; none establish real model identity."""

import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from relay_audit.guard.detector import Detector
from relay_audit.guard.notifier import Notifier
from relay_audit.guard.probe_worker import ProbeWorker
from relay_audit.guard.store import Store
from relay_audit.probes import build, canonical


def observation(**changed):
    row = {"request_id": "request-1", "session_id": "session-1", "upstream_alias": "synthetic-relay",
           "protocol": "responses", "source": "passive", "requested_model": "model-A",
           "response_models": ["model-A"], "http_status": 200, "stream": True,
           "termination": "completed", "total_ms": 10, "usage": {"input_tokens": 3, "output_tokens": 5},
           "conditions": {"reasoning_effort": "medium", "thinking_enabled": True, "output_limit": 128,
                          "tools_present": False, "input_size_bucket": "<1KiB", "cache_status": "miss", "stream": True}}
    row.update(changed)
    return row


class StoreTests(unittest.TestCase):
    def test_metadata_allowlist_redaction_and_file_permissions(self):
        secret = "SYNTHETIC_GUARD_KEY_NEVER_REAL_12345"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, secrets=[secret])
            row = observation(requested_model="M" * 195 + secret, Authorization=secret,
                              prompt="SYNTHETIC_PRIVATE_CONVERSATION", response="PRIVATE_RESPONSE",
                              usage={"input_tokens": 3, "body": "PRIVATE_RESPONSE"})
            store.append_request(row)
            incident = store.incident("declared_model_mismatch", {**row, "reason": "synthetic fixture"}, "session-1", "same")
            stored = "".join(path.read_text() for path in Path(directory).iterdir() if path.suffix in {".jsonl", ".md"})
            for forbidden in (secret, "SYNTHETIC_PRIVATE_CONVERSATION", "PRIVATE_RESPONSE", "Authorization"):
                self.assertFalse(forbidden in stored, "Forbidden content reached metadata storage")
            self.assertNotIn("SYNTH", store.read(kind="requests")[0]["requested_model"])
            self.assertEqual(store.read(kind="requests")[0]["usage"], {"input_tokens": 3})
            self.assertEqual(incident["identity_status"], "unverified")
            for path in Path(directory).iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_budget_and_cooldown_survive_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Store(directory)
            self.assertTrue(first.reserve_probe(2, 3)["available"])
            self.assertTrue(first.reserve_probe(2, 3)["available"])
            self.assertTrue(first.claim_probe_trigger("same", 300))
            restarted = Store(directory)
            self.assertFalse(restarted.reserve_probe(2, 3)["available"])
            self.assertEqual(restarted.reserve_probe(2, 3)["budget_used_day"], 2)
            self.assertFalse(restarted.claim_probe_trigger("same", 300))

    def test_separate_store_instances_cannot_race_past_shared_probe_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Store(directory), Store(directory)
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit((first if index % 2 else second).reserve_probe, 3, 3) for index in range(20)]
                results = [future.result() for future in futures]
            self.assertEqual(sum(row["available"] for row in results), 3)

    def test_request_report_cleanup_and_persisted_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.update_status({"baseline_state": "learning", "identity_status": "verified",
                                 "monitoring_state": "degraded", "monitoring_degraded": ["queue_overflow"],
                                 "notification": {"failures": 1}, "queue_pending": 2})
            self.assertEqual(Store(directory).status()["identity_status"], "unverified")
            self.assertEqual(store.status()["notification"], {"failures": 1})
            self.assertEqual(store.status()["queue_pending"], 2)
            store.incident("monitoring_degraded", {"reason": "synthetic fixture"})
            report = Path(directory) / "export.md"
            store.report(report)
            self.assertIn("unverified", report.read_text())
            with self.assertRaises(FileExistsError):
                store.report(report)
            self.assertEqual(store.cleanup(14)["removed_records"], 0)


class DetectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.config = {"mode": "passive", "model_mappings": {"model-A": ["model-A", "snapshot-A"]},
                       "detector": {"min_samples": 3, "window": 3, "cooldown_seconds": 60,
                                    "error_rate_delta": 0.25, "latency_multiplier": 2}}
        self.messages = []
        self.notifier = Notifier({"terminal": True, "system": False}, self.store, self.messages.append)
        self.detector = Detector(self.config, self.store, self.notifier)

    async def asyncTearDown(self):
        await self.notifier.close()
        self.directory.cleanup()

    async def test_midstream_mismatch_immediately_notifies_deduplicates_and_records_final_once(self):
        class ProbeRecorder:
            def __init__(self): self.events = []
            async def trigger(self, event): self.events.append(event)
            def snapshot(self): return {"enabled": True, "extra_requests": 0}
        recorder = ProbeRecorder()
        self.detector.probe_worker = recorder
        self.detector.cooldown = 0  # Same request remains deduplicated with cooldown disabled.
        stream = observation(response_models=["model-B"], phase="stream_label")
        await self.detector.observe(stream)
        await self.notifier.queue.join()
        self.assertEqual(len(self.store.read()), 1)
        self.assertEqual(self.store.read(kind="requests"), [])
        self.assertEqual(recorder.events, [])
        self.assertIn("声明模型不一致；真实模型身份未验证", self.messages[0])
        await self.detector.observe(observation(response_models=["model-B"], phase="complete"))
        self.assertEqual(len(self.store.read()), 1)
        self.assertEqual(len(self.store.read(kind="requests")), 1)
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(len(self.messages), 1)

    async def test_allowed_alias_model_change_missing_label_and_hidden_equivalence_do_not_verify_identity(self):
        for row in (observation(response_models=["snapshot-A"]), observation(requested_model="model-C", response_models=["model-C"]),
                    observation(response_models=[]), observation()):
            await self.detector.observe(row)
        self.assertEqual(self.store.read(), [])
        self.assertEqual(self.detector.snapshot()["identity_status"], "unverified")
        self.assertEqual(self.detector.snapshot()["baseline_display"], "数据不足")

    async def test_multiple_keys_and_urls_remain_redacted_after_detector_collects_environment_secrets(self):
        local = "SYNTHETIC_LOCAL_KEY_123456"
        upstream = "SYNTHETIC_UPSTREAM_KEY_654321"
        reference = "SYNTHETIC_REFERENCE_KEY_987654"
        target_url = "https://target.example.invalid/v1/responses"
        reference_url = "https://reference.example.invalid/v1/responses"
        self.store.set_secrets([local])
        config = {**self.config, "upstream": {"key_env": "SYNTHETIC_UPSTREAM_ENV", "base_url": target_url},
                  "probe": {"reference": {"key_env": "SYNTHETIC_REFERENCE_ENV", "url": reference_url}}}
        with patch.dict(os.environ, {"SYNTHETIC_UPSTREAM_ENV": upstream, "SYNTHETIC_REFERENCE_ENV": reference}):
            detector = Detector(config, self.store)
            await detector.observe(observation(response_models=[local, upstream, reference, target_url, reference_url]))
        stored = self.store.report() + json.dumps(self.store.read(kind="requests"))
        for forbidden in (local, upstream, reference, target_url, reference_url):
            self.assertFalse(forbidden in stored, "A collected known secret or URL was persisted")

    async def test_service_change_requires_comparable_windows_and_recovers(self):
        for index in range(3):
            await self.detector.observe(observation(request_id=f"baseline-{index}"))
        await self.detector.observe(observation(total_ms=500))
        self.assertFalse(any(row["event_type"] == "service_degradation" for row in self.store.read()))
        for index in range(2):
            await self.detector.observe(observation(request_id=f"slow-{index}", total_ms=500))
        event = next(row for row in self.store.read() if row["event_type"] == "service_degradation")
        self.assertEqual(event["evidence"]["baseline_samples"], 3)
        self.assertEqual(event["evidence"]["before"]["median_ms"], 10)
        self.assertEqual(event["evidence"]["current"]["median_ms"], 500)
        for index in range(3):
            await self.detector.observe(observation(request_id=f"recovery-{index}"))
        self.assertTrue(any(row["event_type"] == "recovered" for row in self.store.read()))

    async def test_model_session_reasoning_and_cache_conditions_do_not_share_baseline(self):
        for index in range(3):
            await self.detector.observe(observation(request_id=f"base-{index}"))
        for changed in ({"session_id": "other-session"}, {"requested_model": "model-C", "response_models": ["model-C"]},
                        {"conditions": {**observation()["conditions"], "reasoning_effort": "high"}},
                        {"conditions": {**observation()["conditions"], "cache_status": "hit"}}):
            for index in range(3):
                await self.detector.observe(observation(total_ms=500, **changed))
        self.assertFalse(any(row["event_type"] == "service_degradation" for row in self.store.read()))
        self.assertEqual(len(self.detector.snapshot()["groups"]), 5)

    async def test_client_cancel_budget_end_and_tool_use_are_distinct_from_protocol_failure(self):
        for ending in ("client_cancelled", "output_budget_exhausted", "tool_use"):
            await self.detector.observe(observation(termination=ending, cancel_source="client" if ending == "client_cancelled" else None))
        self.assertEqual(self.store.read(), [])
        await self.detector.observe(observation(termination="upstream_truncated"))
        self.assertEqual(self.store.read()[0]["event_type"], "protocol_or_capability_error")

    async def test_probe_source_never_contaminates_or_recursively_triggers_live_monitor(self):
        await self.detector.observe(observation(source="probe", response_models=["different-model"]))
        self.assertEqual(self.store.read(), [])
        self.assertEqual(self.detector.snapshot()["requests_observed"], 0)

    async def test_slow_storage_runs_off_the_forwarding_event_loop(self):
        original = self.store.append_request
        def slow(row):
            time.sleep(.2)
            return original(row)
        with patch.object(self.store, "append_request", side_effect=slow):
            start = time.monotonic()
            task = asyncio.create_task(self.detector.observe(observation()))
            await asyncio.sleep(.02)
            self.assertLess(time.monotonic() - start, .15)
            await task

    async def test_notifier_failure_is_recorded_without_sensitive_exception_or_blocking_observation(self):
        def failed(message): raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        self.notifier.sink = failed
        await self.detector.observe(observation(response_models=["model-B"]))
        await self.notifier.queue.join()
        self.assertEqual(self.notifier.failures, 1)
        self.assertTrue(any(row["event_type"] == "monitoring_degraded" for row in self.store.read()))
        self.assertNotIn("SYNTHETIC_PRIVATE_EXCEPTION", self.store.report())

    async def test_active_incident_memory_has_a_fixed_capacity(self):
        now = time.monotonic()
        self.detector._active = {str(index): {"at": now, "event": None} for index in range(2048)}
        event = {"event_type": "monitoring_degraded", "evidence": {}, "fingerprint": "new"}
        with patch.object(self.store, "incident", return_value=event):
            await self.detector._emit("monitoring_degraded", {"reason": "synthetic"}, {}, "new", trigger=False)
        self.assertEqual(len(self.detector._active), 2048)
        self.assertNotIn("0", self.detector._active)
        self.assertIn("new", self.detector._active)


class ProbeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.seen = []
        self.active = 0
        self.maximum_active = 0
        self.expected = {task.prompt: task.expected for task in build(20261011, 1, ["modular", "ledger", "json_schema"])}
        async def handler(request):
            body = await request.json()
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.seen.append((request.path, body))
            await asyncio.sleep(.005)
            protocol = request.path.rsplit("/", 1)[-1]
            prompt = body["input"][0]["content"] if protocol == "responses" else body["messages"][0]["content"]
            answer = canonical(self.expected[prompt] if request.path.startswith("/reference") else {"incorrect": True})
            if protocol == "responses":
                data = {"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]}
            elif protocol == "anthropic_messages":
                data = {"content": [{"type": "text", "text": answer}]}
            else:
                data = {"choices": [{"message": {"content": answer}}]}
            self.active -= 1
            return web.json_response(data)
        application = web.Application()
        application.router.add_post("/{role}/{protocol}", handler)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.workers = []
        self.env = patch.dict(os.environ, {"SYNTHETIC_PROBE_KEY_ENV": "SYNTHETIC_PROBE_KEY_NEVER_REAL"})
        self.env.start()

    async def asyncTearDown(self):
        for worker in self.workers:
            await worker.close()
        await self.runner.cleanup()
        self.env.stop()
        self.directory.cleanup()

    def config(self, protocol="responses", reference=True, **changed):
        endpoint = {"protocol": protocol, "model": "same-model-label", "snapshot_id": "synthetic-matched-snapshot",
                    "key_env": "SYNTHETIC_PROBE_KEY_ENV", "allow_local_http": True, "parameters": {}, "output_limit": 128}
        value = {"mode": "verified_probe", "probe": {"enabled": True, "hourly_limit": 100, "daily_limit": 100,
                 "concurrency": 1, "cooldown_seconds": 300, "min_samples": 3, "calibration_samples": 6,
                 "minimum_drop": .35, "families": ["modular", "ledger", "json_schema"],
                 "target": {**endpoint, "url": self.base + "/target/" + protocol},
                 "reference": {**endpoint, "url": self.base + "/reference/" + protocol, "trusted": True} if reference else None}}
        value["probe"].update(changed)
        return value

    def worker(self, config):
        worker = ProbeWorker(config, self.store)
        self.workers.append(worker)
        return worker

    async def test_passive_mode_has_strictly_zero_extra_requests_even_with_configured_probe(self):
        config = self.config()
        config["mode"] = "passive"
        worker = self.worker(config)
        await worker.start()
        self.assertFalse(await worker.trigger({"event_type": "service_degradation", "fingerprint": "passive"}))
        self.assertEqual(self.seen, [])
        self.assertEqual(worker.snapshot()["extra_requests"], 0)

    async def test_three_protocols_require_matched_reference_self_calibration_before_suspected_drift(self):
        for protocol in ("responses", "chat_completions", "anthropic_messages"):
            worker = self.worker(self.config(protocol))
            self.assertTrue(await worker.trigger({"event_type": "service_degradation", "fingerprint": protocol}))
            await asyncio.wait_for(worker.queue.join(), timeout=5)
            event = self.store.read()[-1]
            self.assertEqual(event["event_type"], "behavioral_drift_suspected")
            self.assertEqual(event["identity_status"], "unverified")
            self.assertEqual(event["evidence"]["ground_truth_status"], "synthetic_unverified")
            self.assertEqual(event["evidence"]["calibration_samples"], 6)
            self.assertEqual(event["evidence"]["target_samples"], 3)
            self.assertEqual(worker.extra_requests, 12)
            self.assertEqual(self.maximum_active, 1)

    async def test_missing_reference_is_uncalibrated_not_identity_or_behavioral_drift(self):
        worker = self.worker(self.config(reference=False))
        await worker.trigger({"event_type": "service_degradation", "fingerprint": "no-reference"})
        await asyncio.wait_for(worker.queue.join(), timeout=5)
        event = self.store.read()[-1]
        self.assertEqual(event["event_type"], "probe_review")
        self.assertEqual(event["evidence"]["calibration_state"], "uncalibrated")

    async def test_identical_observable_probe_behavior_keeps_identity_unverified_and_does_not_report_drift(self):
        config = self.config()
        config["probe"]["target"]["url"] = config["probe"]["reference"]["url"]
        worker = self.worker(config)
        await worker.trigger({"event_type": "service_degradation", "fingerprint": "identical-observable-behavior"})
        await asyncio.wait_for(worker.queue.join(), timeout=5)
        event = self.store.read()[-1]
        self.assertEqual(event["event_type"], "probe_review")
        self.assertEqual(event["evidence"]["target_passes"], 3)
        self.assertEqual(event["evidence"]["drop"], 0)
        self.assertEqual(event["identity_status"], "unverified")

    async def test_budget_cooldown_concurrency_and_restart_never_exceed_hard_limit(self):
        config = self.config(reference=False, hourly_limit=2, daily_limit=2, cooldown_seconds=300, concurrency=2)
        worker = self.worker(config)
        event = {"event_type": "service_degradation", "fingerprint": "budget-event"}
        self.assertTrue(await worker.trigger(event))
        self.assertFalse(await worker.trigger(event))
        await asyncio.wait_for(worker.queue.join(), timeout=5)
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(worker.budget_state, "exhausted")
        restarted = self.worker(config)
        await restarted.trigger({**event, "fingerprint": "new-event"})
        await asyncio.wait_for(restarted.queue.join(), timeout=5)
        self.assertEqual(len(self.seen), 2)
        self.assertLessEqual(self.maximum_active, 2)
        self.assertTrue(all(row["source"] == "probe" for row in self.store.read(kind="requests")))

    async def test_startup_immediately_reports_persisted_budget_usage_and_exhaustion(self):
        self.assertTrue(self.store.reserve_probe(2, 2)["available"])
        self.assertTrue(self.store.reserve_probe(2, 2)["available"])
        worker = self.worker(self.config(reference=False, hourly_limit=2, daily_limit=2))
        await worker.start()
        snapshot = worker.snapshot()
        self.assertEqual(snapshot["budget_used_hour"], 2)
        self.assertEqual(snapshot["budget_used_day"], 2)
        self.assertEqual(snapshot["budget_state"], "exhausted")
        self.assertEqual(snapshot["extra_requests"], 0)
        self.assertEqual(self.seen, [])

    async def test_unavailable_persisted_budget_fails_closed_without_workers_or_model_requests(self):
        worker = self.worker(self.config(reference=False))
        with patch.object(self.store, "reserve_probe", side_effect=OSError("SYNTHETIC_PRIVATE_BUDGET_ERROR")):
            await worker.start()
        self.assertEqual(worker.snapshot()["budget_state"], "monitoring_degraded")
        self.assertIsNone(worker.snapshot()["budget_used_hour"])
        self.assertEqual(worker._tasks, [])
        self.assertIsNone(worker._session)
        self.assertFalse(await worker.trigger({"event_type": "service_degradation", "fingerprint": "blocked"}))
        self.assertEqual(worker.queue.qsize(), 0)
        self.assertEqual(self.seen, [])
        self.assertNotIn("SYNTHETIC_PRIVATE_BUDGET_ERROR", self.store.report())

    async def test_persisted_budget_read_timeout_fails_closed(self):
        worker = self.worker(self.config(reference=False))
        def slow_budget(*args):
            time.sleep(1.1)
            return {"budget_used_hour": 0, "budget_used_day": 0}
        with patch.object(self.store, "reserve_probe", side_effect=slow_budget):
            await worker.start()
        self.assertEqual(worker.snapshot()["budget_state"], "monitoring_degraded")
        self.assertEqual(worker._tasks, [])
        self.assertEqual(self.seen, [])

    async def test_condition_mismatch_and_recursive_probe_events_cannot_report_drift(self):
        config = self.config()
        config["probe"]["reference"]["parameters"] = {"temperature": .5}
        worker = self.worker(config)
        self.assertFalse(await worker.trigger({"event_type": "behavioral_drift_suspected", "fingerprint": "recursive"}))
        self.assertTrue(await worker.trigger({"event_type": "service_degradation", "fingerprint": "mismatch"}))
        await asyncio.wait_for(worker.queue.join(), timeout=5)
        event = self.store.read()[-1]
        self.assertEqual(event["event_type"], "probe_review")
        self.assertFalse(event["evidence"]["conditions_match"])
        self.assertEqual(len(self.seen), 3)


class NotifierIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_synchronous_sink_does_not_block_event_loop(self):
        delivered = []
        def slow(text):
            time.sleep(.2)
            delivered.append(text)
        notifier = Notifier({"terminal": True}, sink=slow)
        try:
            await notifier.notify({"event_type": "declared_model_mismatch"})
            start = time.monotonic()
            await asyncio.sleep(.02)
            self.assertLess(time.monotonic() - start, .15)
            await notifier.queue.join()
            self.assertEqual(len(delivered), 1)
        finally:
            await notifier.close()

    async def test_synchronous_sink_returning_awaitable_is_awaited(self):
        delivered = []
        async def finish(text):
            delivered.append(text)
        def sink(text):
            return finish(text)
        notifier = Notifier({"terminal": True}, sink=sink)
        try:
            await notifier.notify({"event_type": "declared_model_mismatch"})
            await notifier.queue.join()
            self.assertEqual(len(delivered), 1)
            self.assertEqual(notifier.failures, 0)
        finally:
            await notifier.close()

    async def test_system_notification_timeout_kills_and_reaps_child(self):
        class Process:
            returncode = None
            killed = False
            waits = 0
            async def wait(self):
                self.waits += 1
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9
        process = Process()
        async def timeout(awaitable, timeout):
            await awaitable
            raise asyncio.TimeoutError
        notifier = Notifier({"terminal": False, "system": True})
        spawn = AsyncMock(return_value=process)
        try:
            with patch("relay_audit.guard.notifier.asyncio.create_subprocess_exec", new=spawn), \
                 patch("relay_audit.guard.notifier.asyncio.wait_for", side_effect=timeout):
                await notifier.notify({"event_type": "declared_model_mismatch"})
                await notifier.queue.join()
            self.assertTrue(process.killed)
            self.assertEqual(process.waits, 2)
            self.assertEqual(notifier.failures, 1)
            arguments = spawn.call_args.args
            self.assertEqual(arguments[:2], ("osascript", "-e"))
            self.assertIn("on run argv", arguments[2])
            self.assertNotIn("声明模型不一致", arguments[2])
            self.assertIn("声明模型不一致", arguments[3])
        finally:
            await notifier.close()


if __name__ == "__main__":
    unittest.main()
