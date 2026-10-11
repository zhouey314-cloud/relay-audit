import asyncio
import contextlib
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientPayloadError, ClientSession, web

from relay_audit.guard.config import validate
from relay_audit.guard.notifier import Notifier
from relay_audit.guard.observer import Observer
from relay_audit.guard.proxy import create_app
from relay_audit.guard.store import Store


LOCAL_KEY = "SYNTHETIC-LOCAL-GUARD-TOKEN"
UPSTREAM_KEY = "SYNTHETIC-UPSTREAM-API-TOKEN"
PRIVATE = "SYNTHETIC-PRIVATE-PROMPT-ANSWER-REASONING-TOOL-DATA"
ROUTES = {"responses": "/v1/responses", "chat_completions": "/v1/chat/completions",
          "anthropic_messages": "/v1/messages"}


def event(value, name=None):
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (("event: " + name + "\r\n" if name else "") + "data: " + data + "\r\n\r\n").encode()


def fixture(protocol, stream, *, tool=False, budget=False, label="model-A"):
    usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
             "input_tokens_details": {"cached_tokens": 8}, "secret": PRIVATE}
    if protocol == "responses":
        output = ([{"type": "function_call", "id": "fixed-fn", "call_id": "fixed-call", "name": "lookup", "arguments": json.dumps({"private": PRIVATE, "city": "杭州"})}]
                  if tool else [{"type": "message", "content": [{"type": "output_text", "text": PRIVATE + " 你好🙂"}]}])
        status = "incomplete" if budget else "completed"
        body = {"id": "fixed-response", "model": label, "status": status, "output": output,
                "usage": usage, "incomplete_details": {"reason": "max_output_tokens"} if budget else None}
        events = [event({"type": "response.created", "response": {"model": label}}, "response.created")]
        if tool:
            events += [event({"type": "response.output_item.added", "item": {**output[0], "arguments": ""}}, "response.output_item.added")]
            events += [event({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": fragment}, "response.function_call_arguments.delta") for fragment in ('{"private":', '"' + PRIVATE + '","city":"杭', '州"}')]
        else:
            events += [event({"type": "response.reasoning_text.delta", "delta": PRIVATE}, "response.reasoning_text.delta"),
                       event({"type": "response.output_text.delta", "delta": PRIVATE + " 你好🙂"}, "response.output_text.delta")]
        events += [event({"type": "response.future_extension", "secret": PRIVATE}, "response.future_extension"),
                   event({"type": "response." + status, "response": body}, "response." + status)]
    elif protocol == "chat_completions":
        reason = "length" if budget else "tool_calls" if tool else "stop"
        message = {"role": "assistant", "content": None if tool else PRIVATE + " 你好🙂"}
        if tool:
            message["tool_calls"] = [{"type": "function", "id": "fixed-call", "function": {"name": "lookup", "arguments": json.dumps({"private": PRIVATE, "city": "杭州"})}}]
        usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                 "prompt_tokens_details": {"cached_tokens": 8}, "secret": PRIVATE}
        body = {"id": "fixed-chat", "model": label, "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": usage}
        events = [event({"model": label, "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]})]
        if tool:
            events += [event({"model": label, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": fragment, **({"name": "lookup"} if index == 0 else {})}}]}}]}) for index, fragment in enumerate(('{"private":', '"' + PRIVATE + '","city":"杭', '州"}'))]
        else:
            events.append(event({"model": label, "choices": [{"delta": {"content": PRIVATE + " 你好🙂"}}]}))
        events += [event({"model": label, "choices": [{"delta": {}, "finish_reason": reason}]}),
                   event({"choices": [], "usage": usage}), event("[DONE]")]
    else:
        reason = "max_tokens" if budget else "tool_use" if tool else "end_turn"
        block = ({"type": "tool_use", "id": "fixed-call", "name": "lookup", "input": {"private": PRIVATE, "city": "杭州"}}
                 if tool else {"type": "text", "text": PRIVATE + " 你好🙂"})
        usage = {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 8, "cache_creation_input_tokens": 2,
                 "cache_creation": {"ephemeral_5m_input_tokens": 2}, "secret": PRIVATE}
        body = {"id": "fixed-message", "type": "message", "model": label, "role": "assistant", "content": [block], "stop_reason": reason, "usage": usage}
        events = [event({"type": "message_start", "message": {"model": label, "usage": usage}}, "message_start"),
                  event({"type": "ping"}, "ping"),
                  event({"type": "content_block_start", "index": 0, "content_block": {**block, "input": {}} if tool else {"type": "text", "text": ""}}, "content_block_start")]
        if tool:
            events += [event({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": fragment}}, "content_block_delta") for fragment in ('{"private":', '"' + PRIVATE + '","city":"杭', '州"}')]
        else:
            events += [event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": PRIVATE}}, "content_block_delta"),
                       event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": PRIVATE + " 你好🙂"}}, "content_block_delta")]
        events += [event({"type": "content_block_stop", "index": 0}, "content_block_stop"),
                   event({"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 3}}, "message_delta"),
                   event({"type": "message_stop"}, "message_stop")]
    return (b"".join(events), "text/event-stream") if stream else ((json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode(), "application/json")


class CaptureDetector:
    def __init__(self, delay=0, blocker=None, fail=False):
        self.rows = []
        self.delay, self.blocker, self.fail = delay, blocker, fail
        self.completed = asyncio.Event()

    async def observe(self, row):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.blocker is not None:
            await self.blocker.wait()
        if self.fail:
            raise RuntimeError(PRIVATE + UPSTREAM_KEY)
        self.rows.append(row)
        if row.get("phase") == "complete":
            self.completed.set()

    def snapshot(self):
        return {"identity_status": "unverified", "baseline_state": "baseline_unknown"}


class GuardProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runners = []
        self.gateways = []
        self.env = patch.dict(os.environ, {"PROXY_TEST_LOCAL_KEY": LOCAL_KEY, "PROXY_TEST_UPSTREAM_KEY": UPSTREAM_KEY})
        self.env.start()
        self.received = []
        self.sink_count = 0
        self.release = asyncio.Event()
        self.stream_opened = asyncio.Event()
        self.stream_finished = asyncio.Event()
        self.upstream_disconnected = asyncio.Event()
        upstream = web.Application(handler_args={"auto_decompress": False})
        upstream.router.add_route("*", "/{path:.*}", self.upstream_handle)
        self.upstream_url = await self.serve(upstream)
        self.client = ClientSession(auto_decompress=False)

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.env.stop()
        self.temp.cleanup()

    async def serve(self, app):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def new_gateway(self, detector=None, store=None, notifier=None, overrides=None):
        state_dir = str(Path(self.temp.name) / ("state-" + str(len(self.gateways))))
        config = {"listen_port": 0, "state_dir": state_dir, "local_token_env": "PROXY_TEST_LOCAL_KEY",
                  "upstream": {"base_url": self.upstream_url + "/fixed/v1", "key_env": "PROXY_TEST_UPSTREAM_KEY", "allow_local_http": True},
                  "notifications": {"terminal": False, "system": False}}
        for key, value in (overrides or {}).items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
        config = validate(config)
        app = create_app(config, detector=detector, store=store, notifier=notifier)
        gateway = app["gateway"]
        self.gateways.append(gateway)
        return await self.serve(app), gateway

    async def upstream_handle(self, request):
        if request.path == "/sink":
            self.sink_count += 1
            return web.Response(text="sink")
        raw = await request.read()
        self.received.append({"path": request.path, "query": request.query_string, "body": raw,
                              "headers": dict(request.headers)})
        if request.path.endswith("/models"):
            return web.Response(body=b'{"object":"list","data":[{"id":"model-A"}]}\n', content_type="application/json")
        if request.path.endswith("/messages/count_tokens"):
            return web.Response(body=b'{"input_tokens":12}\n', content_type="application/json")
        scenario = request.headers.get("X-Test-Scenario", "normal")
        if scenario == "redirect":
            return web.Response(status=307, headers={"Location": self.upstream_url + "/sink"})
        if scenario == "limited":
            return web.Response(status=429, body=json.dumps({"error": {"message": PRIVATE}}).encode(), content_type="application/json")
        decoded = gzip.decompress(raw) if request.headers.get("Content-Encoding") == "gzip" else raw
        body = json.loads(decoded) if decoded else {}
        protocol = next((name for name, route in ROUTES.items() if request.path.endswith(route[3:])), "responses")
        stream = body.get("stream") is True
        wire, content_type = fixture(protocol, stream, tool=bool(body.get("test_tool")),
                                     budget=scenario == "budget", label="model-B" if scenario == "mismatch" else body.get("model", "model-A"))
        if scenario == "compressed":
            return web.Response(body=gzip.compress(wire, mtime=0), headers={"Content-Encoding": "gzip", "Content-Type": content_type})
        if not stream:
            return web.Response(body=wire, content_type=content_type,
                                headers={"Connection": "X-Upstream-Hop", "X-Upstream-Hop": "remove", "X-Business": "preserve"})
        response = web.StreamResponse(headers={"Content-Type": content_type, "Cache-Control": "no-cache"})
        await response.prepare(request)
        try:
            if scenario in {"held", "disconnect", "midstream_mismatch"}:
                first = event({"type": "response.created", "response": {"model": "model-A"}}, "response.created") + event({"type": "response.output_text.delta", "delta": "early 你好🙂"}, "response.output_text.delta")
                if scenario == "midstream_mismatch":
                    first += event({"type": "response.in_progress", "response": {"model": "model-B"}}, "response.in_progress")
                await response.write(first)
                self.stream_opened.set()
                if scenario == "disconnect":
                    request.transport.close()
                    return response
                await self.release.wait()
                wire = event({"type": "response.completed", "response": {"status": "completed", "model": "model-B" if scenario == "midstream_mismatch" else "model-A"}}, "response.completed")
            cursor = 0
            sizes = (1, 2, 7, 19, 53)
            number = 0
            while cursor < len(wire):
                size = sizes[number % len(sizes)]
                await response.write(wire[cursor:cursor + size])
                cursor += size
                number += 1
            await response.write_eof()
            self.stream_finished.set()
        except (ConnectionResetError, asyncio.CancelledError):
            self.upstream_disconnected.set()
        return response

    async def post(self, base, protocol="responses", *, stream=False, scenario="normal", body=None, headers=None):
        body = body or {"model": "model-A", "stream": stream, "input": PRIVATE, "messages": [{"role": "user", "content": PRIVATE}]}
        request_headers = {"Authorization": "Bearer " + LOCAL_KEY, "X-Test-Scenario": scenario,
                           "X-Session-Id": "synthetic-session", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        return await self.client.post(base + ROUTES[protocol], data=json.dumps(body, ensure_ascii=False, indent=1).encode(),
                                      headers=request_headers, allow_redirects=False)

    async def flush(self, gateway):
        await asyncio.sleep(0)
        await asyncio.wait_for(gateway.flush(), 3)
        await asyncio.wait_for(gateway.notifier.queue.join(), 2)

    def assert_private_not_stored(self):
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                for secret in (PRIVATE, LOCAL_KEY, UPSTREAM_KEY):
                    self.assertFalse(secret.encode() in raw, "Sensitive synthetic data persisted")

    async def test_json_and_sse_entity_bytes_exact_for_all_protocols(self):
        base, gateway = await self.new_gateway()
        for protocol in ROUTES:
            for stream in (False, True):
                with self.subTest(protocol=protocol, stream=stream):
                    expected, _ = fixture(protocol, stream)
                    response = await self.post(base, protocol, stream=stream)
                    actual = await response.read()
                    self.assertTrue(actual == expected, "Gateway changed entity bytes")
                    self.assertEqual(response.status, 200)
                    response.release()
        await self.flush(gateway)
        self.assertEqual(len(self.received), 6)
        self.assertEqual(gateway.status()["probe"]["extra_requests"], 0)
        self.assert_private_not_stored()

    async def test_stream_arrives_before_upstream_completes(self):
        base, gateway = await self.new_gateway()
        response = await self.post(base, stream=True, scenario="held")
        early = await asyncio.wait_for(response.content.readany(), 1)
        self.assertTrue(b"early" in early)
        self.assertFalse(self.stream_finished.is_set())
        self.release.set()
        tail = await asyncio.wait_for(response.read(), 1)
        self.assertTrue(b"response.completed" in tail)
        await self.flush(gateway)
        rows = gateway.store.read(10, "requests")
        self.assertIsNotNone(rows[-1]["first_output_ms"])

    async def test_midstream_label_change_automatically_notifies_before_response_finishes(self):
        alerts = asyncio.Queue()

        async def sink(text):
            await alerts.put(text)

        notifier = Notifier({"notifications": {"terminal": True, "system": False}}, sink=sink)
        base, gateway = await self.new_gateway(notifier=notifier)
        response = await self.post(base, stream=True, scenario="midstream_mismatch")
        first = await response.content.readany()
        try:
            warning = await asyncio.wait_for(alerts.get(), 2)
            self.assertIn("声明模型不一致", warning)
            self.assertFalse(self.stream_finished.is_set())
            self.assertFalse(warning.encode() in first)
        finally:
            self.release.set()
        tail = await response.read()
        self.assertTrue(b"response.completed" in tail)
        self.assertFalse(warning.encode() in tail)
        await self.flush(gateway)
        self.assertTrue(any(row["event_type"] == "declared_model_mismatch" for row in gateway.store.read()))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(gateway.status()["identity_status"], "unverified")
        self.assert_private_not_stored()

    async def test_multiround_tools_and_cache_metadata_preserve_business_entities(self):
        detector = CaptureDetector()
        base, gateway = await self.new_gateway(detector=detector)
        for protocol in ROUTES:
            tool_request = {"model": "model-A", "stream": True, "test_tool": True, "tools": [{"name": "lookup"}], "input": PRIVATE}
            response = await self.post(base, protocol, body=tool_request)
            self.assertTrue(await response.read() == fixture(protocol, True, tool=True)[0], "Tool stream changed")
            continuation = {"model": "model-A", "stream": True, "input": [{"type": "function_call_output", "call_id": "fixed-call", "output": PRIVATE}],
                            "messages": [{"role": "tool", "tool_call_id": "fixed-call", "content": PRIVATE}]}
            response = await self.post(base, protocol, body=continuation)
            self.assertTrue(await response.read() == fixture(protocol, True)[0], "Continuation stream changed")
            self.assertTrue(json.loads(self.received[-1]["body"]) == continuation, "Tool result request changed")
        await self.flush(gateway)
        complete = [row for row in detector.rows if row["phase"] == "complete"]
        self.assertEqual(len(complete), 6)
        self.assertEqual(sum(row["termination"] == "tool_use" for row in complete), 3)
        self.assertTrue(all(row["conditions"]["cache_status"] == "hit" for row in complete))
        self.assertEqual(gateway.status()["probe"]["extra_requests"], 0)

    async def test_fixed_routes_and_authentication_do_not_forward_client_credentials(self):
        base, gateway = await self.new_gateway()
        bad = await self.client.post(base + "/v1/responses", json={"model": "A"})
        self.assertEqual(bad.status, 401)
        await bad.read()
        bad = await self.client.post(base + "/arbitrary-route", headers={"Authorization": "Bearer " + LOCAL_KEY})
        self.assertEqual(bad.status, 404)
        await bad.read()
        self.assertEqual(len(self.received), 0)
        for protocol in ROUTES:
            request_body = {"model": "model-A", "stream": False, "url": "https://unrelated.example.invalid/", "reasoning": {"effort": "high"}, "max_output_tokens": 30,
                            "messages": [{"role": "user", "content": PRIVATE}], "tools": [{"name": "lookup"}]}
            response = await self.post(base, protocol, body=request_body,
                                       headers={"Cookie": PRIVATE, "x-api-key": LOCAL_KEY, "Connection": "X-Client-Hop", "X-Client-Hop": "remove"})
            await response.read()
            captured = self.received[-1]
            self.assertEqual(captured["path"], "/fixed" + ROUTES[protocol])
            self.assertTrue(json.loads(captured["body"]) == request_body, "Business request changed")
            self.assertNotIn("Cookie", captured["headers"])
            self.assertNotIn("X-Client-Hop", captured["headers"])
            self.assertNotIn("X-Upstream-Hop", response.headers)
            self.assertEqual(response.headers.get("X-Business"), "preserve")
            upstream_auth = captured["headers"].get("x-api-key") if protocol == "anthropic_messages" else captured["headers"].get("Authorization")
            self.assertTrue(upstream_auth == (UPSTREAM_KEY if protocol == "anthropic_messages" else "Bearer " + UPSTREAM_KEY), "Upstream authentication is incorrect")
        await self.flush(gateway)
        self.assert_private_not_stored()

    async def test_redirect_is_returned_without_forwarding_credentials_to_sink(self):
        base, gateway = await self.new_gateway()
        response = await self.post(base, scenario="redirect")
        self.assertEqual(response.status, 307)
        await response.read()
        self.assertEqual(self.sink_count, 0)
        self.assertEqual(len(self.received), 1)
        await self.flush(gateway)

    async def test_auxiliary_routes_are_passthrough_and_websocket_is_explicitly_unsupported(self):
        base, gateway = await self.new_gateway()
        response = await self.client.get(base + "/v1/models", headers={"Authorization": "Bearer " + LOCAL_KEY})
        self.assertEqual(await response.read(), b'{"object":"list","data":[{"id":"model-A"}]}\n')
        response = await self.client.post(base + "/v1/messages/count_tokens", data=b'{"model":"model-A","messages":[]}',
                                          headers={"x-api-key": LOCAL_KEY, "Content-Type": "application/json"})
        self.assertEqual(await response.read(), b'{"input_tokens":12}\n')
        response = await self.client.post(base + "/v1/responses", data=b'{}',
                                          headers={"Authorization": "Bearer " + LOCAL_KEY, "Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(response.status, 426)
        await response.read()
        response = await self.client.get(base + "/v1/responses",
                                         headers={"Authorization": "Bearer " + LOCAL_KEY, "Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(response.status, 426)
        await response.read()
        self.assertEqual(len(self.received), 2)
        await self.flush(gateway)
        response = await self.client.get(base + "/__guard/status", headers={"Authorization": "Bearer " + LOCAL_KEY})
        status = await response.json()
        self.assertIn("websocket_not_supported", status["monitoring_degraded"])
        self.assertEqual(status["identity_status"], "unverified")

    async def test_compressed_response_entity_and_headers_remain_consistent(self):
        base, gateway = await self.new_gateway()
        response = await self.post(base, scenario="compressed")
        actual = await response.read()
        expected = gzip.compress(fixture("responses", False)[0], mtime=0)
        self.assertTrue(actual == expected, "Compressed response entity changed")
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertTrue(gzip.decompress(actual) == fixture("responses", False)[0])
        await self.flush(gateway)
        self.assertEqual(gateway.status()["monitoring_state"], "degraded")

    async def test_compressed_request_entity_is_forwarded_without_decode_header_mismatch(self):
        base, gateway = await self.new_gateway()
        raw = json.dumps({"model": "model-A", "stream": False, "input": PRIVATE}).encode()
        compressed = gzip.compress(raw, mtime=0)
        response = await self.client.post(base + "/v1/responses", data=compressed,
                                          headers={"Authorization": "Bearer " + LOCAL_KEY, "Content-Type": "application/json", "Content-Encoding": "gzip"})
        await response.read()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.received[-1]["body"] == compressed, "Compressed request entity changed")
        self.assertEqual(self.received[-1]["headers"].get("Content-Encoding"), "gzip")
        await self.flush(gateway)

    async def test_rate_limit_and_output_budget_are_separate_and_not_retried(self):
        detector = CaptureDetector()
        base, gateway = await self.new_gateway(detector=detector)
        response = await self.post(base, scenario="limited")
        self.assertEqual(response.status, 429)
        await response.read()
        for protocol in ROUTES:
            response = await self.post(base, protocol, stream=True, scenario="budget")
            await response.read()
        await self.flush(gateway)
        complete = [row for row in detector.rows if row["phase"] == "complete"]
        self.assertEqual([row["termination"] for row in complete], ["rate_limited", "output_budget", "output_budget", "output_budget"])
        self.assertEqual(len(self.received), 4)
        self.assertEqual(gateway.forwarded, 4)

    async def test_passive_n_agent_requests_are_exactly_n_upstream_requests(self):
        base, gateway = await self.new_gateway()
        for number in range(5):
            response = await self.post(base, stream=bool(number % 2))
            await response.read()
        await self.flush(gateway)
        self.assertEqual(len(self.received), 5)
        self.assertEqual(gateway.forwarded, 5)
        self.assertEqual(gateway.status()["probe"]["extra_requests"], 0)

    async def test_client_cancel_is_not_a_server_protocol_incident(self):
        detector = CaptureDetector()
        base, gateway = await self.new_gateway(detector=detector)
        response = await self.post(base, stream=True, scenario="held")
        await response.content.readany()
        response.close()
        await asyncio.wait_for(detector.completed.wait(), 1)
        self.release.set()
        await self.flush(gateway)
        rows = [row for row in detector.rows if row["phase"] == "complete"]
        self.assertEqual(rows[-1]["termination"], "client_cancelled")
        self.assertEqual(rows[-1]["cancel_source"], "downstream_disconnect")

    async def test_upstream_disconnect_is_not_fabricated_into_completed_output(self):
        detector = CaptureDetector()
        base, gateway = await self.new_gateway(detector=detector)
        response = await self.post(base, stream=True, scenario="disconnect")
        with contextlib.suppress(ClientPayloadError):
            await response.read()
        await self.flush(gateway)
        rows = [row for row in detector.rows if row["phase"] == "complete"]
        self.assertIn(rows[-1]["termination"], {"upstream_truncated", "upstream_transport_error"})
        self.assertIsNone(rows[-1]["cancel_source"])

    async def test_observation_queue_overflow_keeps_entities_readable_and_degrades_status(self):
        blocker = asyncio.Event()
        detector = CaptureDetector(blocker=blocker)
        base, gateway = await self.new_gateway(detector=detector, overrides={"observation": {"queue_size": 1, "detector_timeout_seconds": 1}})
        responses = await asyncio.gather(*(self.post(base, stream=True) for _ in range(8)))
        bodies = await asyncio.gather(*(response.read() for response in responses))
        self.assertTrue(all(body == fixture("responses", True)[0] for body in bodies), "Queue failure changed entities")
        blocker.set()
        await self.flush(gateway)
        self.assertGreater(gateway.dropped, 0)
        self.assertEqual(gateway.status()["monitoring_state"], "degraded")
        self.assertIn("observation_queue_full", gateway.status()["monitoring_degraded"])
        self.assertEqual(len(self.received), 8)

    async def test_slow_or_failing_detector_cannot_block_forwarding(self):
        for detector in (CaptureDetector(delay=5), CaptureDetector(fail=True)):
            base, gateway = await self.new_gateway(detector=detector, overrides={"observation": {"detector_timeout_seconds": .05}})
            response = await asyncio.wait_for(self.post(base, stream=True), 1)
            self.assertTrue(await asyncio.wait_for(response.read(), 1) == fixture("responses", True)[0], "Detector failure changed entities")
            await self.flush(gateway)
            self.assertEqual(gateway.status()["monitoring_state"], "degraded")
        self.assert_private_not_stored()

    async def test_observer_exception_keeps_stream_readable_and_private(self):
        class BrokenObserver:
            def __init__(self, *args):
                pass

            def feed(self, chunk):
                raise RuntimeError(PRIVATE + UPSTREAM_KEY)

        base, gateway = await self.new_gateway()
        with patch("relay_audit.guard.proxy.Observer", BrokenObserver):
            response = await self.post(base, stream=True)
            self.assertTrue(await response.read() == fixture("responses", True)[0], "Observer failure changed entity")
        await self.flush(gateway)
        self.assertIn("observer_failure", gateway.status()["monitoring_degraded"])
        self.assert_private_not_stored()

    async def test_slow_observer_is_outside_the_stream_forwarding_path(self):
        started = threading.Event()
        allow_parse = threading.Event()
        finished = threading.Event()
        original = Observer.feed

        def slow_feed(observer, chunk):
            started.set()
            allow_parse.wait(1)
            time.sleep(.2)
            original(observer, chunk)
            finished.set()

        base, gateway = await self.new_gateway()
        with patch("relay_audit.guard.observer.Observer.feed", slow_feed):
            try:
                response = await asyncio.wait_for(self.post(base, stream=True), .5)
                body = await asyncio.wait_for(response.read(), .5)
                self.assertTrue(body == fixture("responses", True)[0], "Slow observer changed response")
                self.assertTrue(await asyncio.to_thread(started.wait, .5), "Observer worker did not start")
                self.assertFalse(finished.is_set(), "Forwarding waited for observer parsing")
            finally:
                allow_parse.set()
            await self.flush(gateway)

    async def test_parser_queue_overflow_is_unobservable_not_upstream_truncation(self):
        started = threading.Event()
        allow_parse = threading.Event()
        original = Observer.feed

        def held_feed(observer, chunk):
            started.set()
            allow_parse.wait(2)
            original(observer, chunk)

        detector = CaptureDetector()
        base, gateway = await self.new_gateway(detector=detector, overrides={"observation": {"queue_size": 1}})
        with patch("relay_audit.guard.observer.Observer.feed", held_feed):
            first = await self.post(base, stream=True, scenario="held")
            await first.content.readany()
            self.assertTrue(await asyncio.to_thread(started.wait, 1), "Parser did not start")
            try:
                second = await self.post(base, stream=True)
                self.assertTrue(await second.read() == fixture("responses", True)[0], "Parser queue overflow changed entity")
                await asyncio.wait_for(detector.completed.wait(), 1)
                row = next(row for row in detector.rows if row["phase"] == "complete")
                self.assertEqual(row["termination"], "unobservable")
                self.assertFalse(row["parse_error"])
                self.assertLessEqual(gateway.parser_queue.qsize(), 1)
                self.assertIn("parser_queue_full", gateway.status()["monitoring_degraded"])
            finally:
                allow_parse.set()
                self.release.set()
                await first.read()
            await self.flush(gateway)
        self.assertTrue(all(row.get("termination") != "upstream_truncated" for row in detector.rows if row["phase"] == "complete"))

    async def test_incident_storage_failure_is_visible_without_breaking_model_response(self):
        class BrokenIncidentStore(Store):
            def incident(self, *args, **kwargs):
                raise OSError(PRIVATE + UPSTREAM_KEY)

        store = BrokenIncidentStore(str(Path(self.temp.name) / "broken-store"))
        base, gateway = await self.new_gateway(store=store)
        response = await self.post(base, scenario="mismatch")
        self.assertTrue(await response.read() == fixture("responses", False, label="model-B")[0])
        await self.flush(gateway)
        self.assertEqual(gateway.status()["monitoring_state"], "degraded")
        self.assert_private_not_stored()

    async def test_notification_failure_is_visible_without_breaking_model_response(self):
        def failing_sink(text):
            raise RuntimeError(PRIVATE + UPSTREAM_KEY)

        notifier = Notifier({"notifications": {"terminal": True, "system": False}}, sink=failing_sink)
        base, gateway = await self.new_gateway(notifier=notifier)
        response = await self.post(base, scenario="mismatch")
        self.assertTrue(await response.read() == fixture("responses", False, label="model-B")[0])
        await self.flush(gateway)
        self.assertGreater(gateway.notifier.failures, 0)
        self.assertEqual(gateway.status()["monitoring_state"], "degraded")
        self.assert_private_not_stored()


if __name__ == "__main__":
    unittest.main()
