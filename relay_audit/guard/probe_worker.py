"""Explicitly enabled, isolated synthetic review with persisted request budgets."""

import asyncio
import json
import os
import time
import uuid
from urllib.parse import urlsplit

import aiohttp

from ..probes import PROBE_VERSION, build, canonical, grade
from ..transport import MAX_RESPONSE_BYTES, TOOL, extract, payload


class ProbeWorker:
    def __init__(self, config, store, notifier=None):
        self.config, self.store, self.notifier = config, store, notifier
        self.probe = config.get("probe", {})
        self.enabled = (config.get("mode", "passive") == "verified_probe"
                        and self.probe.get("enabled") is True
                        and int(self.probe.get("hourly_limit", 0)) > 0
                        and int(self.probe.get("daily_limit", 0)) > 0)
        self.concurrency = max(1, min(int(self.probe.get("concurrency", 1)), 8))
        self.queue = asyncio.Queue(maxsize=32)
        self._tasks = []
        self._start_lock = asyncio.Lock()
        self._startup_blocked = False
        self._session = None
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.extra_requests = 0
        self.inflight = 0
        self.budget_state = "disabled" if not self.enabled else "available"
        self.calibration_state = "uncalibrated"
        self._budget = {"budget_used_hour": 0, "budget_used_day": 0}

    async def start(self):
        if not self.enabled or self._tasks or self._startup_blocked:
            return
        async with self._start_lock:
            if self._tasks or self._startup_blocked:
                return
            hourly, daily = int(self.probe.get("hourly_limit", 0)), int(self.probe.get("daily_limit", 0))
            try:
                # count=0 only reads persisted usage; startup never reserves or
                # sends a model request, and must not display a reset budget.
                self._budget = await asyncio.wait_for(
                    asyncio.to_thread(self.store.reserve_probe, hourly, daily, 0), timeout=1)
            except Exception:
                self._startup_blocked = True
                self.budget_state = "monitoring_degraded"
                self._budget = {"budget_used_hour": None, "budget_used_day": None,
                                "hourly_limit": hourly, "daily_limit": daily}
                try:
                    await asyncio.wait_for(self._event("monitoring_degraded",
                                                       {"reason": "probe_persisted_budget_unavailable"}, {}), timeout=1)
                except Exception:
                    pass
                return
            self.budget_state = ("exhausted" if self._budget["budget_used_hour"] >= hourly
                                 or self._budget["budget_used_day"] >= daily else "available")
            self._session = aiohttp.ClientSession(trust_env=False, auto_decompress=True,
                                                 timeout=aiohttp.ClientTimeout(total=max(1, min(int(self.probe.get("timeout_seconds", 20)), 60))))
            self._tasks = [asyncio.create_task(self._worker(), name="guard-probe") for _ in range(self.concurrency)]

    async def trigger(self, event):
        if not self.enabled or self._startup_blocked or event.get("event_type") in {"monitoring_degraded", "probe_review", "behavioral_drift_suspected", "recovered"}:
            return False
        if event.get("evidence", {}).get("source") == "probe":
            return False
        fingerprint = str(event.get("fingerprint", event.get("event_type", "probe")))
        try:
            await self.start()
            if self._startup_blocked:
                return False
            claimed = await asyncio.to_thread(self.store.claim_probe_trigger, fingerprint,
                                              max(0, int(self.probe.get("cooldown_seconds", 300))))
            if not claimed:
                return False
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            await self._event("monitoring_degraded", {"reason": "probe_queue_full"}, event)
        except Exception:
            await self._event("monitoring_degraded", {"reason": "probe_budget_or_queue_unavailable"}, event)
        return False

    @staticmethod
    def _endpoint(raw):
        if not isinstance(raw, dict):
            raise ValueError("Explicit probe endpoint required")
        ep = dict(raw)
        protocol = ep.get("protocol")
        if protocol not in {"responses", "chat_completions", "anthropic_messages"}:
            raise ValueError("Unsupported probe protocol")
        parts = urlsplit(ep.get("url", ""))
        local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Unsafe probe URL")
        if parts.scheme != "https" and not (parts.scheme == "http" and local and ep.get("allow_local_http") is True):
            raise ValueError("HTTPS required for probes")
        if not isinstance(ep.get("model"), str) or not ep["model"]:
            raise ValueError("Probe model required")
        if not isinstance(ep.get("parameters", {}), dict):
            raise ValueError("Probe parameters must be an object")
        if set(ep.get("parameters", {})) - {"temperature", "top_p", "seed", "reasoning_effort", "thinking", "reasoning"}:
            raise ValueError("Unsupported probe parameters")
        ep.setdefault("parameters", {})
        ep.setdefault("output_limit", 512)
        ep.setdefault("limit_field", "max_tokens")
        if type(ep["output_limit"]) is not int or not 16 <= ep["output_limit"] <= 32768:
            raise ValueError("Invalid probe output cap")
        key_env = ep.get("key_env")
        if not isinstance(key_env, str) or not key_env.isidentifier():
            raise ValueError("Probe key environment variable required")
        key = os.environ.get(key_env, "")
        if not key or not key.isascii() or any(char in key for char in "\r\n"):
            raise ValueError("Probe key missing or invalid")
        return ep, key

    @staticmethod
    def _request(ep, task):
        if ep["protocol"] != "responses":
            return payload(ep, task)
        request = {"model": ep["model"], "input": [{"role": "user", "content": task.prompt}],
                   "max_output_tokens": ep["output_limit"], **ep["parameters"]}
        if task.tool:
            request["tools"] = [{"type": "function", **TOOL}]
            request["tool_choice"] = {"type": "function", "name": TOOL["name"]}
        return request

    @staticmethod
    def _grade(ep, task, data):
        if ep["protocol"] != "responses":
            result = extract(ep, data, task)
            if result.grading_error:
                return False
            return grade(task, result.answer)[0]
        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
            raise ValueError("Invalid Responses probe envelope")
        output = data["output"]
        if task.tool:
            calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
            if len(calls) != 1 or calls[0].get("name") != TOOL["name"]:
                return False
            answer = calls[0].get("arguments")
        else:
            texts = [block.get("text") for item in output if isinstance(item, dict) and item.get("type") == "message"
                     for block in item.get("content", []) if isinstance(block, dict) and block.get("type") == "output_text"]
            if any(not isinstance(text, str) for text in texts):
                raise ValueError("Invalid Responses output text")
            answer = "".join(texts)
        return grade(task, answer)[0]

    async def _call(self, ep, key, task):
        async with self._semaphore:
            budget = await asyncio.to_thread(self.store.reserve_probe,
                                             int(self.probe.get("hourly_limit", 0)), int(self.probe.get("daily_limit", 0)))
            self._budget = budget
            if not budget["available"]:
                self.budget_state = "exhausted"
                return None
            self.extra_requests += 1
            self.inflight += 1
            start = time.monotonic()
            status, passed, reason = None, None, "probe_transport_error"
            headers = {"Content-Type": "application/json"}
            if ep["protocol"] == "anthropic_messages":
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            else:
                headers["Authorization"] = "Bearer " + key
            try:
                async with self._session.post(ep["url"], json=self._request(ep, task), headers=headers,
                                              allow_redirects=False) as response:
                    status = response.status
                    if 200 <= status < 300:
                        raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                        # read(n) can return before EOF; collect boundedly until completion.
                        while not response.content.at_eof() and len(raw) <= MAX_RESPONSE_BYTES:
                            raw += await response.content.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                        if len(raw) <= MAX_RESPONSE_BYTES:
                            passed = self._grade(ep, task, json.loads(raw))
                            reason = "matched" if passed else "answer_mismatch"
                        else:
                            reason = "probe_response_too_large"
                    else:
                        reason = "probe_http_error"
            except asyncio.CancelledError:
                raise
            except Exception:
                reason = "probe_invalid_or_unavailable_response"
            finally:
                self.inflight -= 1
                await asyncio.to_thread(self.store.append_request, {
                    "request_id": "probe-" + uuid.uuid4().hex, "session_id": "independent-probe", "source": "probe",
                    "upstream_alias": "probe-reference" if ep.get("trusted") else "probe-target",
                    "protocol": ep["protocol"], "requested_model": ep["model"], "http_status": status,
                    "termination": reason, "total_ms": round((time.monotonic() - start) * 1000, 3),
                })
            return passed

    @staticmethod
    def _conditions(ep):
        return {key: ep.get(key) for key in ("protocol", "snapshot_id", "parameters", "output_limit", "limit_field")}

    async def _review(self, event):
        target, key = self._endpoint(self.probe.get("target"))
        self.store.set_secrets([key, target["url"]])
        count = max(2, min(int(self.probe.get("min_samples", 3)), 100))
        families = self.probe.get("families", ["modular", "ledger", "json_schema"])
        if not isinstance(families, list) or not families:
            raise ValueError("Probe families required")
        tasks = build(20261011, max(1, (count + len(families) - 1) // len(families)), families)[:count]
        reference_raw = self.probe.get("reference")
        matched = False
        reference = None
        if reference_raw:
            reference, reference_key = self._endpoint(reference_raw)
            self.store.set_secrets([reference_key, reference["url"]])
            matched = bool(reference.get("trusted") is True and target.get("snapshot_id")
                           and self._conditions(reference) == self._conditions(target))
        evidence = {"source": "probe", "identity_status": "unverified", "ground_truth_status": "synthetic_unverified",
                    "conditions_match": matched, "reference_trusted": bool(reference and reference.get("trusted")),
                    "calibration_state": "uncalibrated", "target_samples": 0, "target_passes": 0,
                    "probe_version": PROBE_VERSION, "seed": 20261011, "families": families,
                    "samples_requested": count, "protocol": target["protocol"]}
        if matched:
            calibration_count = max(2 * count, int(self.probe.get("calibration_samples", 6)))
            calibration = [await self._call(reference, reference_key, tasks[index % len(tasks)])
                           for index in range(calibration_count)]
            if any(item is None for item in calibration):
                evidence["reason"] = "reference_calibration_unavailable_or_budget_exhausted"
                evidence["budget_state"] = self.budget_state
                await self._event("probe_review", evidence, event)
                return
            midpoint = len(calibration) // 2
            first = sum(calibration[:midpoint]) / midpoint
            second = sum(calibration[midpoint:]) / (len(calibration) - midpoint)
            stable = abs(first - second) <= 0.2 and min(first, second) >= 0.8
            evidence.update({"calibration_samples": len(calibration), "calibration_passes": sum(calibration),
                             "calibration_state": "descriptive_reference_self_check" if stable else "unstable"})
            self.calibration_state = evidence["calibration_state"]
            if not stable:
                await self._event("probe_review", {**evidence, "reason": "reference_self_variation_not_stable"}, event)
                return
            reference_results = [await self._call(reference, reference_key, task) for task in tasks]
            target_results = [await self._call(target, key, task) for task in tasks]
            pairs = [(left, right) for left, right in zip(reference_results, target_results) if left is not None and right is not None]
            evidence.update({"reference_samples": len(pairs), "target_samples": len(pairs),
                             "reference_passes": sum(left for left, right in pairs),
                             "target_passes": sum(right for left, right in pairs), "budget_state": self.budget_state})
            drop = sum(left - right for left, right in pairs) / len(pairs) if pairs else 0
            evidence["drop"] = round(drop, 4)
            evidence["minimum_drop"] = float(self.probe.get("minimum_drop", 0.35))
            if len(pairs) >= count and drop >= evidence["minimum_drop"]:
                evidence["observed_fact"] = "Condition-matched synthetic review showed a descriptive capability difference."
                evidence["possible_explanations"] = ["Sampling, gateway processing or capability configuration; actual model weights remain unverified."]
                await self._event("behavioral_drift_suspected", evidence, event)
            else:
                await self._event("probe_review", {**evidence, "reason": "no_supported_drift_or_insufficient_pairs"}, event)
        else:
            outcomes = [await self._call(target, key, task) for task in tasks]
            evidence.update({"target_samples": sum(item is not None for item in outcomes),
                             "target_passes": sum(item is True for item in outcomes),
                             "reason": "trusted_matched_reference_missing; identity_unverified", "budget_state": self.budget_state})
            await self._event("probe_review", evidence, event)

    async def _event(self, event_type, evidence, source):
        try:
            event = await asyncio.to_thread(self.store.incident, event_type, evidence,
                                            source.get("session_id", ""), "probe:" + str(source.get("fingerprint", "review")))
            if self.notifier is not None:
                await self.notifier.notify(event)
        except Exception:
            self.budget_state = "monitoring_degraded"

    async def _worker(self):
        while True:
            event = await self.queue.get()
            try:
                await self._review(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._event("monitoring_degraded", {"reason": "probe_configuration_or_worker_unavailable"}, event)
            finally:
                self.queue.task_done()

    def snapshot(self):
        return {"enabled": self.enabled, "extra_requests": self.extra_requests, "inflight": self.inflight,
                "budget_state": self.budget_state, "calibration_state": self.calibration_state, **self._budget}

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._session is not None:
            await self._session.close()
            self._session = None
