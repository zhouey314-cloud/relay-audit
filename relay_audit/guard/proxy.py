"""A fixed-route loopback gateway with an independent, bounded observer queue."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from multidict import CIMultiDict

from .config import validate
from .detector import Detector
from .notifier import Notifier
from .observer import Observer, observe_json
from .probe_worker import ProbeWorker
from .store import Store

ROUTES = {
    ("POST", "/v1/responses"): "responses",
    ("POST", "/v1/chat/completions"): "chat_completions",
    ("POST", "/v1/messages"): "anthropic_messages",
    ("POST", "/v1/messages/count_tokens"): "token_count",
    ("GET", "/v1/models"): "models",
}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "content-length"}


class _PrivateHTTPLogger(logging.Logger):
    def exception(self, msg, *args, **kwargs):
        # aiohttp parser exceptions may embed raw HTTP payloads. Never print them.
        self.error("Gateway HTTP handling failed; diagnostic payload suppressed.")

    def error(self, msg, *args, **kwargs):
        super().error("Gateway HTTP handling failed; diagnostic payload suppressed.")


class _DiscardUpgrade:
    def feed_data(self, data):
        return False, b""

    def feed_eof(self):
        pass


def headers_without_hops(headers, request=False):
    # Repeated Connection fields may each nominate headers for this one hop.
    blocked = HOP | {word.strip().lower() for name, value in headers.items()
                     if name.lower() == "connection" for word in value.split(",")}
    if request:
        blocked |= {"authorization", "x-api-key", "cookie", "host"}
    else:
        blocked |= {"set-cookie"}
    return CIMultiDict((key, val) for key, val in headers.items() if key.lower() not in blocked)


class Gateway:
    def __init__(self, config, *, detector=None, notifier=None, store=None):
        self.config = validate(config)
        self.local_token = os.environ.get(self.config["local_token_env"], "")
        self.upstream_key = os.environ.get(self.config["upstream"]["key_env"], "")
        if not self.local_token or not self.upstream_key:
            raise ValueError("explicit local and upstream credential environment variables required")
        if any(not key.isascii() or any(char in key for char in "\r\n") for key in (self.local_token, self.upstream_key)):
            raise ValueError("credential values must be valid ASCII header tokens")
        self.store = store or Store(self.config["state_dir"], (self.local_token, self.upstream_key))
        self.store.set_secrets((self.local_token, self.upstream_key))
        self.notifier = notifier or Notifier(self.config, self.store)
        self.probes = ProbeWorker(self.config, self.store, self.notifier)
        self.detector = detector or Detector(self.config, self.store, self.notifier, self.probes)
        self.queue = asyncio.Queue(maxsize=self.config["observation"]["queue_size"])
        self.parser_queue = asyncio.Queue(maxsize=self.config["observation"]["queue_size"])
        self.forwarded = self.observed = self.dropped = 0
        self.degraded = set()
        self.stop_event = asyncio.Event()
        self._worker_task = None
        self._parser_task = None
        self._fallback_tasks = set()
        self.client = None
        self.started_at = time.time()

    async def start(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=self.config["request_timeout_seconds"]),
                                    auto_decompress=False, trust_env=False)
        await self.notifier.start()
        await self.probes.start()
        self._worker_task = asyncio.create_task(self._worker(), name="guard-detector")
        self._parser_task = asyncio.create_task(self._parser_worker(), name="guard-parser")
        if not self.config["observation"]["enabled"]:
            self._mark_degraded("observation_disabled")
        # Disk work never runs on the forwarding event loop.
        try:
            await asyncio.wait_for(asyncio.to_thread(self.store.cleanup, self.config["retention_days"]), 1)
        except Exception:
            self._mark_degraded("retention_cleanup_failure")

    async def close(self, app):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.flush(), 2)
        for task in (self._parser_task, self._worker_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.probes.close()
        await self.notifier.close()
        if self.client:
            await self.client.close()
        for task in self._fallback_tasks:
            task.cancel()
        if self._fallback_tasks:
            await asyncio.gather(*self._fallback_tasks, return_exceptions=True)

    def _mark_degraded(self, reason):
        # Fixed internal reason codes only. Deduplicate and bound background tasks.
        if reason in self.degraded or len(self.degraded) >= 32:
            return
        self.degraded.add(reason)

        async def record():
            try:
                event = await asyncio.to_thread(self.store.incident, "monitoring_degraded",
                                               {"reason": reason, "identity_status": "unverified"}, "", reason)
                await self.notifier.notify(event)
            except Exception:
                # Always independent of SSE/model/tool bytes. No exception text.
                await asyncio.to_thread(print, "[AgentGuard] 监测降级；身份未验证；检查本地状态。", file=sys.stderr, flush=True)

        task = asyncio.create_task(record(), name="guard-degraded")
        self._fallback_tasks.add(task)
        task.add_done_callback(self._fallback_tasks.discard)

    def submit(self, row):
        if not self.config["observation"]["enabled"]:
            return
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            self._mark_degraded("observation_queue_full")

    async def flush(self):
        """Local acceptance/shutdown barrier; never used while forwarding bytes."""
        await self.parser_queue.join()
        await self.queue.join()

    def _parse_enqueue(self, context, kind, data=None, arrived_ms=None):
        try:
            self.parser_queue.put_nowait((context, kind, data, arrived_ms))
        except asyncio.QueueFull:
            context["lost"] = True
            self.dropped += 1
            self._mark_degraded("parser_queue_full")
            if kind == "finish":
                self.submit({**context["row"], "termination": "unobservable", "parse_error": False})

    async def _parser_worker(self):
        while True:
            context, kind, data, arrived = await self.parser_queue.get()
            row = context["row"]
            try:
                if kind == "chunk" and not context["lost"]:
                    context["last_arrival"] = arrived
                    if context["sse"]:
                        if context["observer"] is None:
                            context["observer"] = await asyncio.to_thread(Observer, row["protocol"], row,
                                                                         self.config["observation"]["max_event_bytes"])
                        observer = context["observer"]
                        await asyncio.to_thread(observer.feed, data)
                        if context["visible_at"] is None and observer.first_visible_output_ms is not None:
                            context["visible_at"] = arrived
                        for event in observer.drain_events():
                            if event["type"] == "model_label":
                                self.submit({**row, "phase": "stream_label", "response_models": [event["model"]]})
                            elif event["type"] == "monitoring_degraded":
                                self._mark_degraded(event["reason"])
                    elif len(context["body"]) + len(data) <= self.config["observation"]["max_event_bytes"]:
                        context["body"].extend(data)
                    else:
                        context["body"].clear()
                        context["lost"] = True
                        self._mark_degraded("json_observation_limit")
                elif kind == "finish":
                    transport_reason = row.get("termination")
                    if context["lost"]:
                        row.update({"termination": "unobservable", "parse_error": False})
                    elif context["sse"]:
                        observer = context["observer"]
                        if observer is None:
                            observer = await asyncio.to_thread(Observer, row["protocol"], row,
                                                               self.config["observation"]["max_event_bytes"])
                        result = await asyncio.to_thread(observer.finish, cancelled=context["cancelled"])
                        result["first_visible_output_ms"] = context["visible_at"]
                        self._parse_result(row, result)
                    else:
                        result = await asyncio.to_thread(observe_json, row["protocol"], bytes(context["body"]), row,
                                                         self.config["observation"]["max_event_bytes"])
                        if result.get("first_visible_output_ms") is not None:
                            result["first_visible_output_ms"] = context["last_arrival"]
                        self._parse_result(row, result)
                        if row["stream"]:
                            row["parse_error"] = True
                    if context["cancelled"]:
                        row.update({"termination": "client_cancelled", "cancel_source": "downstream_disconnect"})
                    elif transport_reason in {"upstream_timeout", "upstream_transport_error"}:
                        row["termination"] = transport_reason
                    self.submit(dict(row))
            except asyncio.CancelledError:
                raise
            except Exception:
                context["lost"] = True
                self._mark_degraded("observer_failure")
                if kind == "finish":
                    self.submit({**row, "termination": "unobservable", "parse_error": False})
            finally:
                self.parser_queue.task_done()

    async def _worker(self):
        while True:
            row = await self.queue.get()
            try:
                await asyncio.wait_for(self.detector.observe(row), self.config["observation"]["detector_timeout_seconds"])
                if getattr(self.detector, "failures", 0):
                    self._mark_degraded("detector_or_storage_failure")
                if row.get("phase") == "complete":
                    self.observed += 1
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._mark_degraded("detector_timeout")
            except Exception:
                self._mark_degraded("detector_or_storage_failure")
            finally:
                self.queue.task_done()

    def status(self):
        try:
            detector = self.detector.snapshot()
        except Exception:
            detector = {"baseline_state": "数据不足", "identity_status": "unverified"}
        notices = self.notifier.snapshot()
        reasons = sorted(self.degraded)
        if notices.get("failures") or notices.get("dropped"):
            reasons.append("notification_failed_or_dropped")
        if detector.get("error_count"):
            reasons.append("detector_or_storage_failure")
        probe = self.probes.snapshot()
        if probe.get("budget_state") == "monitoring_degraded":
            reasons.append("probe_review_unavailable")
        return {**detector, "identity_status": "unverified", "mode": self.config["mode"],
                "requests_forwarded": self.forwarded, "requests_observed": self.observed,
                "queue_dropped": self.dropped, "queue_pending": self.queue.qsize(),
                "parser_queue_pending": self.parser_queue.qsize(),
                "monitoring_state": "degraded" if reasons else "observing",
                "monitoring_degraded": reasons, "notification": notices,
                "probe": probe, "uptime_seconds": round(time.time() - self.started_at, 3)}

    def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if not token or not token.isascii() or not hmac.compare_digest(token, self.local_token):
            raise web.HTTPUnauthorized(text="Local gateway authentication required.")

    async def control_status(self, request):
        self.authenticate(request)
        return web.json_response(self.status())

    async def control_stop(self, request):
        self.authenticate(request)
        self.stop_event.set()
        return web.json_response({"stopping": True})

    async def control_incidents(self, request):
        self.authenticate(request)
        try:
            events = await asyncio.wait_for(asyncio.to_thread(self.store.read, 20), 1)
        except Exception:
            self._mark_degraded("incident_read_failure")
            raise web.HTTPServiceUnavailable(text="Monitoring report unavailable.")
        return web.json_response({"identity_status": "unverified", "incidents": events})

    def _metadata(self, request, protocol, body):
        try:
            data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                data = {}
        except (ValueError, UnicodeError, RecursionError):
            data = {}
        session = (request.headers.get("X-Session-Id") or request.headers.get("Session-Id")
                   or request.headers.get("Session_Id") or request.headers.get("X-Codex-Session-Id"))
        # A client without a stable session identifier gets a single-request group.
        # This is safer than mixing unrelated users and conversations into a baseline.
        request_id = uuid.uuid4().hex
        if not session and isinstance(data.get("metadata"), dict):
            session = data["metadata"].get("session_id")
        session_id = self.store.association_digest(session or request_id)
        reasoning = data.get("reasoning", {})
        effort = reasoning.get("effort", data.get("reasoning_effort")) if isinstance(reasoning, dict) else data.get("reasoning_effort")
        if not isinstance(effort, str) or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            effort = None
        limit = next((data.get(k) for k in ("max_output_tokens", "max_completion_tokens", "max_tokens") if type(data.get(k)) is int), None)
        size = len(body)
        thinking = data.get("thinking")
        thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
        thinking_enabled = thinking is True or (isinstance(thinking_type, str) and thinking_type in {"enabled", "adaptive"})
        conditions = {"reasoning_effort": effort, "thinking_enabled": thinking_enabled,
                      "output_limit": limit, "tools_present": bool(data.get("tools")),
                      "input_size_bucket": "<1KiB" if size < 1024 else "1-16KiB" if size < 16384 else "16-256KiB" if size < 262144 else ">=256KiB",
                      "cache_status": "unknown", "stream": data.get("stream") is True}
        model = data.get("model")
        return {"request_id": request_id, "session_id": session_id, "upstream_alias": self.config["upstream"]["alias"],
                "protocol": protocol, "source": "passive", "requested_model": model if isinstance(model, str) else None,
                "response_models": [], "stream": conditions["stream"], "conditions": conditions, "phase": "complete"}

    def _parse_result(self, row, result):
        row.update({"response_models": result.get("model_labels", []),
                    "usage": result.get("usage_provider_reported", {}),
                    "usage_source": "upstream_reported", "finish_reason": result.get("finish_reason"),
                    "model_label_observable": bool(result.get("model_labels")),
                    "protocol_errors": result.get("protocol_errors", []),
                    "observation_parse_errors": result.get("parse_errors", 0),
                    "first_output_ms": result.get("first_visible_output_ms"),
                    "termination": result.get("termination", "unobservable"),
                    "parse_error": bool(result.get("protocol_errors"))})
        usage = row["usage"]
        cached = usage.get("cache_read_input_tokens", usage.get("cached_tokens"))
        for key in ("input_tokens_details", "prompt_tokens_details"):
            if isinstance(usage.get(key), dict):
                cached = usage[key].get("cached_tokens", cached)
        row["conditions"]["cache_status"] = "hit" if type(cached) is int and cached > 0 else "miss" if cached == 0 else "unknown"
        for reason in result.get("monitoring_degraded", []):
            self._mark_degraded(reason)

    async def forward(self, request):
        self.authenticate(request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            self._mark_degraded("websocket_not_supported")
            # aiohttp treats Upgrade payload as a future protocol tail. Consume it
            # with its public parser hook before explicitly rejecting and closing.
            request.protocol.set_parser(_DiscardUpgrade())
            response = web.Response(status=426, text="WebSocket is unsupported; configure the Agent provider for HTTP/SSE.")
            response.force_close()
            return response
        protocol = ROUTES.get((request.method, request.path))
        if protocol is None:
            raise web.HTTPNotFound(text="Unsupported fixed gateway route.")
        body = await request.read()
        if request.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            self._mark_degraded("compressed_request_metadata_unobservable")
        row = self._metadata(request, protocol, body)
        start = time.monotonic()
        context = None
        response = None
        complete = cancelled = False
        row.update({"http_status": None, "termination": "gateway_error", "ttfb_ms": None,
                    "first_output_ms": None, "total_ms": None, "cancel_source": None})
        upstream = self.config["upstream"]["base_url"]
        suffix = request.path[3:] if upstream.endswith("/v1") else request.path
        url = upstream + suffix
        # Query parameters remain request data; they cannot choose a proxy destination.
        if request.query_string:
            url += "?" + request.query_string
        headers = headers_without_hops(request.headers, request=True)
        if protocol in {"anthropic_messages", "token_count"}:
            headers["x-api-key"] = self.upstream_key
            if "anthropic-version" not in headers:
                headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = "Bearer " + self.upstream_key
        self.forwarded += 1
        try:
            async with self.client.request(request.method, url, data=body if request.method == "POST" else None,
                                           headers=headers, allow_redirects=False) as upstream_response:
                row["http_status"] = upstream_response.status
                is_sse = "text/event-stream" in upstream_response.headers.get("Content-Type", "").lower()
                observing = self.config["observation"]["enabled"] and protocol in {"responses", "chat_completions", "anthropic_messages"} and upstream_response.status < 400
                compressed = upstream_response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}
                if compressed and observing:
                    observing = False
                    self._mark_degraded("compressed_response_not_observable")
                    row["termination"] = "unobservable"
                if observing:
                    context = {"row": row, "sse": is_sse, "observer": None, "body": bytearray(),
                               "lost": False, "cancelled": False, "visible_at": None, "last_arrival": None}
                response = web.StreamResponse(status=upstream_response.status,
                                              headers=headers_without_hops(upstream_response.headers))
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_any():
                    if row["ttfb_ms"] is None:
                        row["ttfb_ms"] = round((time.monotonic() - start) * 1000, 3)
                    # Transfer first. Observation errors must not intercept model bytes.
                    await response.write(chunk)
                    if context is not None and not context["lost"]:
                        # Limit individual retained copies as well as the global queue.
                        maximum = self.config["observation"]["max_event_bytes"]
                        arrived_ms = round((time.monotonic() - start) * 1000, 3)
                        for offset in range(0, len(chunk), maximum):
                            self._parse_enqueue(context, "chunk", chunk[offset:offset + maximum], arrived_ms)
                            if context["lost"]:
                                break
                complete = True
                if context is None and row["termination"] != "unobservable":
                    row["termination"] = "rate_limited" if upstream_response.status == 429 else "upstream_http_error" if upstream_response.status >= 400 else "redirect_not_followed" if upstream_response.status in {301,302,303,307,308} else "completed"
                await response.write_eof()
                return response
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            cancelled = True
            row["termination"] = "client_cancelled"
            row["cancel_source"] = "downstream_disconnect"
            if isinstance(sys.exc_info()[1], asyncio.CancelledError):
                raise
            return response or web.Response(status=499, text="Client cancelled.")
        except (asyncio.TimeoutError, ClientError, OSError):
            row["termination"] = "upstream_timeout" if isinstance(sys.exc_info()[1], asyncio.TimeoutError) else "upstream_transport_error"
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.Response(status=504 if row["termination"] == "upstream_timeout" else 502, text="Configured upstream unavailable.")
        finally:
            row["total_ms"] = round((time.monotonic() - start) * 1000, 3)
            if context is not None:
                context["cancelled"] = cancelled
                self._parse_enqueue(context, "finish")
            else:
                self.submit(row)


def create_app(config, *, detector=None, notifier=None, store=None):
    gateway = Gateway(config, detector=detector, notifier=notifier, store=store)
    app = web.Application(client_max_size=32 * 1024 * 1024,
                          handler_args={"handler_cancellation": True, "auto_decompress": False,
                                        "logger": _PrivateHTTPLogger("relay_audit.guard.http")})
    app["gateway"] = gateway
    app.router.add_get("/__guard/status", gateway.control_status)
    app.router.add_post("/__guard/stop", gateway.control_stop)
    app.router.add_get("/__guard/incidents", gateway.control_incidents)
    # Whitelist validation lives in forward, so Upgrade gets a clear rejection.
    app.router.add_route("*", "/{path:.*}", gateway.forward)
    app.on_startup.append(gateway.start)
    app.on_cleanup.append(gateway.close)
    return app
