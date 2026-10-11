"""Conservative descriptive rules over metadata, never conversation grading."""

import asyncio
import json
import os
import time
from collections import OrderedDict, deque
from statistics import median

from .store import CONDITION_FIELDS, IDENTITY, RULE_VERSION, utc_now


class Detector:
    def __init__(self, config, store, notifier=None, probe_worker=None):
        self.config, self.store = config, store
        self.notifier, self.probe_worker = notifier, probe_worker
        rules = config.get("detector", {})
        self.minimum = max(2, int(rules.get("min_samples", 20)))
        self.window = max(self.minimum, int(rules.get("window", 20)))
        self.error_delta = float(rules.get("error_rate_delta", 0.25))
        self.latency_multiplier = float(rules.get("latency_multiplier", 2.5))
        self.cooldown = max(0, float(rules.get("cooldown_seconds", 120)))
        self._groups = {}
        self._active = {}
        self._request_events = OrderedDict()
        self._last_status = 0
        self.observed = 0
        self.failures = 0
        self._secrets_from_config(config)

    def _secrets_from_config(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"key_env", "upstream_key_env", "local_token_env", "token_env", "access_token_env"} and isinstance(item, str):
                    self.store.set_secrets([os.environ.get(item, "")])
                elif key in {"url", "base_url"} and isinstance(item, str):
                    self.store.set_secrets([item])
                else:
                    self._secrets_from_config(item)
        elif isinstance(value, list):
            for item in value:
                self._secrets_from_config(item)

    def _group_key(self, row):
        conditions = {key: row.get("conditions", {}).get(key) for key in sorted(CONDITION_FIELDS)}
        conditions["stream"] = row.get("stream", conditions.get("stream"))
        return json.dumps([row.get("session_id", ""), row.get("upstream_alias", ""), row.get("protocol", ""),
                           row.get("requested_model", ""), conditions], sort_keys=True, separators=(",", ":"))

    async def _emit(self, event_type, evidence, row, fingerprint, trigger=True):
        now = time.monotonic()
        request_key = (row.get("request_id"), event_type) if row.get("request_id") else None
        cached = self._request_events.get(request_key) if request_key else None
        if cached is not None:
            if trigger and not cached["triggered"]:
                await self._trigger_probe(cached["event"], row)
                cached["triggered"] = True
            return None
        active = self._active.get(fingerprint)
        if active is not None and now - active["at"] < self.cooldown:
            if active.get("event") is not None:
                if request_key:
                    self._remember_request(request_key, active["event"], trigger)
                if trigger:
                    await self._trigger_probe(active["event"], row)
            return None
        self._active[fingerprint] = {"at": now, "event": None}
        if len(self._active) > 2048:
            self._active.pop(next(iter(self._active)))
        evidence = {**evidence, "rule_version": RULE_VERSION, "identity_status": IDENTITY}
        try:
            event = await asyncio.to_thread(self.store.incident, event_type, evidence,
                                            row.get("session_id", ""), fingerprint)
        except Exception:
            self.failures += 1
            return None
        self._active[fingerprint]["event"] = event
        if request_key:
            self._remember_request(request_key, event, trigger)
        if self.notifier is not None:
            try:
                await self.notifier.notify(event)
            except Exception:
                self.failures += 1
        if trigger:
            await self._trigger_probe(event, row)
        return event

    def _remember_request(self, key, event, triggered):
        self._request_events[key] = {"event": event, "triggered": triggered}
        if len(self._request_events) > 2048:
            self._request_events.popitem(last=False)

    async def _trigger_probe(self, event, row):
        if (self.probe_worker is not None and event.get("event_type") not in {"monitoring_degraded", "recovered"}
                and row.get("source", "agent") in {"agent", "passive", "user"}):
            await self.probe_worker.trigger(event)

    async def _recover(self, fingerprint, row):
        if fingerprint in self._active:
            self._active.pop(fingerprint, None)
            try:
                event = await asyncio.to_thread(self.store.incident, "recovered",
                                                {"reason": "current_comparable_observation_recovered"},
                                                row.get("session_id", ""), fingerprint)
                if self.notifier is not None:
                    await self.notifier.notify(event)
            except Exception:
                self.failures += 1

    async def monitoring_degraded(self, reason, metadata=None):
        row = metadata or {}
        self.failures += 1
        return await self._emit("monitoring_degraded", {"reason": str(reason)}, row,
                                "monitoring:" + str(reason), trigger=False)

    async def observe(self, metadata):
        if not isinstance(metadata, dict):
            await self.monitoring_degraded("invalid_observation")
            return
        # Independent probes never enter the live traffic baseline or recursively trigger probes.
        if metadata.get("source", "agent") not in {"agent", "passive", "user"}:
            return
        row = self.store.sanitize_request(metadata)
        key = self._group_key(row)
        requested = metadata.get("requested_model")
        mappings = self.config.get("model_mappings", {})
        allowed = mappings.get(requested, [requested]) if requested else []
        if isinstance(allowed, str):
            allowed = [allowed]
        labels = metadata.get("response_models", [])
        if isinstance(labels, str):
            labels = [labels]
        mismatched = bool(requested and labels and any(label not in allowed for label in labels))
        mismatch_key = "model:" + key
        if mismatched:
            await self._emit("declared_model_mismatch", {
                "request_id": row.get("request_id"), "requested_model": row.get("requested_model"),
                "response_models": row.get("response_models"),
                "observed_fact": "Response model label is outside the configured allowed mapping.",
                "possible_explanations": ["Label mapping or endpoint routing changed; model weights remain unverified."],
            }, row, mismatch_key, trigger=metadata.get("phase") != "stream_label")
        elif metadata.get("phase") != "stream_label":
            await self._recover(mismatch_key, row)
        if metadata.get("phase") == "stream_label":
            return
        await asyncio.to_thread(self.store.append_request, metadata)
        self.observed += 1
        if not labels:
            row["response_models"] = []  # Missing model metadata is unobservable, never evidence of substitution.
        status = row.get("http_status")
        termination = row.get("termination")
        cancelled = row.get("cancel_source") == "client" or termination == "client_cancelled"
        protocol_failure = bool(row.get("parse_error")) or termination in {
            "upstream_disconnect", "stream_disconnect", "invalid_response", "failed", "error", "unexpected_eof",
            "upstream_truncated", "upstream_transport_error", "upstream_timeout",
        }
        capability_error = status in {400, 404, 422}
        protocol_key = "protocol:" + key
        if (protocol_failure or capability_error) and not cancelled:
            await self._emit("protocol_or_capability_error", {
                "request_id": row.get("request_id"), "http_status": status,
                "termination": termination, "parse_error": row.get("parse_error"),
                "observed_fact": "A protocol or capability failure was observed; this does not identify model weights.",
                "possible_explanations": ["Unsupported parameters, gateway compatibility or an upstream stream failure."],
            }, row, protocol_key)
        elif not cancelled:
            await self._recover(protocol_key, row)
        if key not in self._groups and len(self._groups) >= 512:
            self._groups.pop(next(iter(self._groups)))
        group = self._groups.setdefault(key, {"baseline": [], "current": deque(maxlen=self.window),
                                             "conditions": row.get("conditions", {}), "started": utc_now()})
        group["ended"] = utc_now()
        error = bool(status is None or (isinstance(status, int) and status >= 400) or protocol_failure)
        duration = row.get("total_ms")
        sample = {"error": error, "ms": duration if isinstance(duration, (int, float)) else None,
                  "request_id": row.get("request_id")}
        if not cancelled:
            if len(group["baseline"]) < self.minimum:
                if not mismatched and not protocol_failure and not capability_error:
                    group["baseline"].append(sample)
            else:
                group["current"].append(sample)
                if len(group["current"]) >= self.minimum:
                    before = self._distribution(group["baseline"])
                    current = self._distribution(group["current"])
                    error_changed = current["error_rate"] - before["error_rate"] >= self.error_delta
                    latency_changed = (before["median_ms"] is not None and before["median_ms"] > 0
                                       and current["median_ms"] is not None
                                       and current["median_ms"] >= before["median_ms"] * self.latency_multiplier)
                    service_key = "service:" + key
                    if error_changed or latency_changed:
                        await self._emit("service_degradation", {
                            "before": before, "current": current, "conditions": row.get("conditions", {}),
                            "baseline_samples": len(group["baseline"]), "current_samples": len(group["current"]),
                            "request_ids": [sample["request_id"] for sample in group["current"]],
                            "observed_fact": "Comparable request windows show a descriptive service change.",
                            "possible_explanations": ["Network, queueing, provider availability or request workload may differ."],
                        }, row, service_key)
                    else:
                        await self._recover(service_key, row)
        if time.monotonic() - self._last_status >= 1:
            self._last_status = time.monotonic()
            await asyncio.to_thread(self.store.update_status, self.snapshot())

    @staticmethod
    def _distribution(samples):
        durations = [item["ms"] for item in samples if item["ms"] is not None]
        return {"sample_count": len(samples), "error_rate": sum(item["error"] for item in samples) / len(samples),
                "median_ms": median(durations) if durations else None}

    def snapshot(self):
        groups = [{"fingerprint": self.store.association_digest(key), "baseline_samples": len(value["baseline"]),
                   "current_samples": len(value["current"]),
                   "conditions": value["conditions"], "baseline_version": RULE_VERSION,
                   "collection_started_at_utc": value["started"], "collection_ended_at_utc": value.get("ended"),
                   "state": "learning" if len(value["baseline"]) < self.minimum else "baseline_available"}
                  for key, value in self._groups.items()]
        enough = bool(groups and all(group["baseline_samples"] >= self.minimum and group["current_samples"] >= self.minimum
                                     for group in groups))
        state = "baseline_unknown" if not groups else ("current_no_anomaly" if enough else "learning")
        display = "当前未观察到异常；真实模型身份未验证" if enough else "数据不足"
        if self._active:
            state, display = "observed_anomaly", "已观察到事件；真实模型身份未验证"
        if self.failures:
            state, display = "monitoring_degraded", "监测降级；数据不足"
        return {"identity_status": IDENTITY, "baseline_state": state, "requests_observed": self.observed,
                "baseline_display": display,
                "groups": groups, "active_incidents": len(self._active), "error_count": self.failures,
                "mode": self.config.get("mode", "passive"),
                "probe": self.probe_worker.snapshot() if self.probe_worker is not None else {"enabled": False, "extra_requests": 0}}
