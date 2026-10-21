"""Security and failure-path regressions using only synthetic loopback services."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from aiohttp import ClientError, ClientSession, web
from multidict import CIMultiDict

from relay_audit.guard.cli import _control
from relay_audit.guard.config import validate
from relay_audit.guard.proxy import Gateway, create_app
from relay_audit.guard.store import Store

LOCAL_KEY = "SYNTHETIC_HARDENING_LOCAL_TOKEN"
UPSTREAM_KEY = "SYNTHETIC_HARDENING_UPSTREAM_TOKEN"
PRIVATE = "SYNTHETIC_PRIVATE_PAYLOAD_NEVER_LOG"


@contextmanager
def control_server(seen, redirect=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            if redirect is not None:
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
            else:
                raw = b'{"local_guard":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class LocalControlHardeningTests(unittest.TestCase):
    def configuration(self, port, directory):
        return {"listen_host": "127.0.0.1", "listen_port": port,
                "local_token_env": "HARDENING_LOCAL_TOKEN", "state_dir": directory}

    def test_local_control_ignores_implicit_http_proxy(self):
        guard_seen, proxy_seen = [], []
        with tempfile.TemporaryDirectory() as directory, control_server(guard_seen) as guard_port, control_server(proxy_seen) as proxy_port:
            fake_proxy = "http://127.0.0.1:" + str(proxy_port)
            with patch.dict(os.environ, {"HARDENING_LOCAL_TOKEN": LOCAL_KEY,
                                         "HTTP_PROXY": fake_proxy, "http_proxy": fake_proxy,
                                         "NO_PROXY": "", "no_proxy": ""}):
                result = _control(self.configuration(guard_port, directory), "/__guard/status")
            self.assertEqual(result, {"local_guard": True})
            self.assertEqual(proxy_seen, [])
            self.assertEqual(len(guard_seen), 1)
            self.assertEqual(guard_seen[0]["authorization"], "Bearer " + LOCAL_KEY)

    def test_local_control_does_not_follow_redirect_or_forward_token(self):
        guard_seen, sink_seen = [], []
        with tempfile.TemporaryDirectory() as directory, control_server(sink_seen) as sink_port:
            with control_server(guard_seen, "http://127.0.0.1:" + str(sink_port) + "/sink") as guard_port, \
                 patch.dict(os.environ, {"HARDENING_LOCAL_TOKEN": LOCAL_KEY}):
                with self.assertRaises(URLError):
                    _control(self.configuration(guard_port, directory), "/__guard/status")
            self.assertEqual(len(guard_seen), 1)
            self.assertEqual(sink_seen, [])


class ConfigurationHardeningTests(unittest.TestCase):
    def endpoint(self, **changed):
        endpoint = {"url": "https://synthetic.example.invalid/v1/responses", "protocol": "responses",
                    "model": "model-A", "snapshot_id": "synthetic-snapshot", "key_env": "HARDENING_UPSTREAM_TOKEN"}
        endpoint.update(changed)
        return endpoint

    def test_nested_probe_credentials_and_invalid_parameter_shapes_are_rejected(self):
        endpoints = [
            self.endpoint(parameters={"api_key": UPSTREAM_KEY}),
            self.endpoint(parameters={"Authorization": "Bearer " + UPSTREAM_KEY}),
            self.endpoint(parameters={"thinking": {"type": "enabled", "api_key": UPSTREAM_KEY}}),
            self.endpoint(parameters={"reasoning": {"effort": {"private": PRIVATE}}}),
            self.endpoint(output_limit=999999), self.endpoint(allow_local_http="yes"),
        ]
        for endpoint in endpoints:
            with self.subTest(fields=sorted(endpoint)), self.assertRaises((ValueError, TypeError)):
                validate({"probe": {"target": endpoint}})

    def test_invalid_header_credentials_fail_before_state_or_http_session_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            for field, value in (("HARDENING_LOCAL_TOKEN", "非ASCII"),
                                 ("HARDENING_LOCAL_TOKEN", "bad\nheader"),
                                 ("HARDENING_UPSTREAM_TOKEN", "非ASCII"),
                                 ("HARDENING_UPSTREAM_TOKEN", "bad\rheader")):
                state = Path(directory) / field
                env = {"HARDENING_LOCAL_TOKEN": LOCAL_KEY, "HARDENING_UPSTREAM_TOKEN": UPSTREAM_KEY, field: value}
                with self.subTest(field=field), patch.dict(os.environ, env), self.assertRaises(ValueError):
                    Gateway({"state_dir": str(state), "local_token_env": "HARDENING_LOCAL_TOKEN",
                             "upstream": {"key_env": "HARDENING_UPSTREAM_TOKEN"}})
                self.assertFalse(state.exists())


class GatewayFailureHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runners = []
        self.release = asyncio.Event()
        self.received = []
        self.received_headers = []
        self.env = patch.dict(os.environ, {"HARDENING_LOCAL_TOKEN": LOCAL_KEY, "HARDENING_UPSTREAM_TOKEN": UPSTREAM_KEY})
        self.env.start()
        application = web.Application()
        application.router.add_post("/v1/responses", self.upstream)
        self.upstream_url = await self.serve(application)
        self.client = ClientSession()

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.env.stop()
        self.directory.cleanup()

    async def serve(self, application):
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def upstream(self, request):
        body = await request.read()
        self.received.append(body)
        self.received_headers.append(CIMultiDict(request.headers))
        if request.headers.get("X-Synthetic-Scenario") == "midstream_timeout":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            raw = b'event: response.created\ndata: {"type":"response.created","response":{"model":"model-A","status":"in_progress"}}\n\n'
            await response.write(raw)
            await self.release.wait()
            return response
        headers = None
        if request.headers.get("X-Synthetic-Scenario") == "repeated_connection":
            headers = CIMultiDict([
                ("Connection", "X-First-Reply"), ("Connection", "X-Second-Reply"),
                ("X-First-Reply", PRIVATE), ("X-Second-Reply", PRIVATE),
                ("X-Business-Repeat", "first"), ("X-Business-Repeat", "second"),
            ])
        return web.Response(body=json.dumps({"id": "synthetic-response", "status": "completed", "model": "model-A",
                                             "output": [{"type": "message", "content": [{"type": "output_text", "text": PRIVATE}]}]}).encode(),
                            content_type="application/json", headers=headers)

    async def gateway(self, store=None, **changed):
        state = Path(self.directory.name) / ("state-" + str(len(self.runners)))
        config = {"listen_port": 0, "state_dir": str(state), "local_token_env": "HARDENING_LOCAL_TOKEN",
                  "upstream": {"base_url": self.upstream_url + "/v1", "key_env": "HARDENING_UPSTREAM_TOKEN", "allow_local_http": True},
                  "notifications": {"terminal": False, "system": False}, **changed}
        application = create_app(config, store=store)
        url = await self.serve(application)
        return url, application["gateway"]

    async def test_cleanup_failure_keeps_gateway_forwarding_and_monitoring_degradation_visible(self):
        store = Store(Path(self.directory.name) / "cleanup-failure")
        with patch.object(store, "cleanup", side_effect=OSError("SYNTHETIC_PRIVATE_STORAGE_EXCEPTION")):
            url, gateway = await self.gateway(store=store)
        body = b'{"model":"model-A","input":"synthetic","stream":false}'
        async with self.client.post(url + "/v1/responses", data=body, headers={"Authorization": "Bearer " + LOCAL_KEY}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["model"], "model-A")
        await gateway.flush()
        self.assertEqual(self.received, [body])
        self.assertEqual(gateway.status()["monitoring_state"], "degraded")
        self.assertIn("retention_cleanup_failure", gateway.status()["monitoring_degraded"])
        self.assertNotIn("SYNTHETIC_PRIVATE_STORAGE_EXCEPTION", store.report())

    async def test_all_repeated_request_connection_headers_are_removed(self):
        url, gateway = await self.gateway()
        body = b'{"model":"model-A","input":"synthetic","stream":false}'
        headers = CIMultiDict([
            ("Authorization", "Bearer " + LOCAL_KEY),
            ("Connection", "X-First-Hop"), ("Connection", "x-second-hop, X-Third-Hop"),
            ("X-First-Hop", PRIVATE), ("X-Second-Hop", PRIVATE), ("X-Third-Hop", PRIVATE),
            ("X-Business-Repeat", "first"), ("X-Business-Repeat", "second"),
        ])
        async with self.client.post(url + "/v1/responses", data=body, headers=headers) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "completed")
        await gateway.flush()
        self.assertEqual(self.received, [body])
        captured = self.received_headers[0]
        for name in ("X-First-Hop", "X-Second-Hop", "X-Third-Hop"):
            self.assertNotIn(name, captured)
        self.assertEqual(captured.getall("X-Business-Repeat"), ["first", "second"])
        self.assertEqual(captured["Authorization"], "Bearer " + UPSTREAM_KEY)
        self.assertEqual(gateway.forwarded, 1)

    async def test_all_repeated_response_connection_headers_are_removed(self):
        url, gateway = await self.gateway()
        body = b'{"model":"model-A","input":"synthetic","stream":false}'
        async with self.client.post(url + "/v1/responses", data=body,
                                    headers={"Authorization": "Bearer " + LOCAL_KEY,
                                             "X-Synthetic-Scenario": "repeated_connection"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["output"][0]["content"][0]["text"], PRIVATE)
            for name in ("X-First-Reply", "X-Second-Reply"):
                self.assertNotIn(name, response.headers)
            self.assertEqual(response.headers.getall("X-Business-Repeat"), ["first", "second"])
        await gateway.flush()
        self.assertEqual(self.received, [body])
        self.assertEqual(gateway.forwarded, 1)

    async def test_midstream_timeout_metadata_preserves_timeout_instead_of_generic_truncation(self):
        url, gateway = await self.gateway(request_timeout_seconds=1)
        body = b'{"model":"model-A","input":"synthetic","stream":true}'
        async with self.client.post(url + "/v1/responses", data=body,
                                    headers={"Authorization": "Bearer " + LOCAL_KEY,
                                             "X-Synthetic-Scenario": "midstream_timeout"}) as response:
            self.assertEqual(response.status, 200)
            with self.assertRaises(ClientError):
                await response.read()
        await asyncio.wait_for(gateway.flush(), timeout=3)
        rows = gateway.store.read(kind="requests")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["termination"], "upstream_timeout")
        self.assertEqual(gateway.status()["requests_forwarded"], 1)
        self.assertFalse(any(row["termination"] == "upstream_truncated" for row in rows))

    async def _wait_for_request_row(self, gateway):
        async def stored():
            while True:
                rows = gateway.store.read(kind="requests")
                if rows:
                    return rows[0]
                await asyncio.sleep(.01)
        return await asyncio.wait_for(stored(), 2)

    async def _transport_failure_with_full_parser_queue(self, cancel):
        url, gateway = await self.gateway(request_timeout_seconds=1,
                                         observation={"queue_size": 1})
        # A suspended parser deterministically models an observer stalled in disk/CPU
        # work. The forwarding path must remain independent of this queue.
        gateway._parser_task.cancel()
        await asyncio.gather(gateway._parser_task, return_exceptions=True)
        try:
            response = await self.client.post(
                url + "/v1/responses",
                data=b'{"model":"model-A","input":"synthetic","stream":true}',
                headers={"Authorization": "Bearer " + LOCAL_KEY,
                         "X-Synthetic-Scenario": "midstream_timeout"})
            self.assertEqual(response.status, 200)
            self.assertTrue(await response.content.readany())
            if cancel:
                response.close()
            else:
                with self.assertRaises(ClientError):
                    await response.read()
            row = await self._wait_for_request_row(gateway)
            self.assertEqual(row["termination"], "client_cancelled" if cancel else "upstream_timeout")
            self.assertEqual(row["cancel_source"], "downstream_disconnect" if cancel else None)
            self.assertFalse(row["parse_error"])
            self.assertIn("parser_queue_full", gateway.status()["monitoring_degraded"])
            self.assertEqual(len(self.received), 1)
            self.assertEqual(gateway.status()["probe"]["extra_requests"], 0)
        finally:
            gateway._parser_task = asyncio.create_task(gateway._parser_worker())
            self.release.set()
            await asyncio.wait_for(gateway.flush(), 3)

    async def test_parser_finish_overflow_preserves_upstream_timeout(self):
        await self._transport_failure_with_full_parser_queue(cancel=False)

    async def test_parser_finish_overflow_preserves_client_disconnect(self):
        await self._transport_failure_with_full_parser_queue(cancel=True)

    async def test_observer_finish_failure_preserves_upstream_timeout(self):
        url, gateway = await self.gateway(request_timeout_seconds=1)
        with patch("relay_audit.guard.proxy.Observer.finish", side_effect=RuntimeError(PRIVATE + UPSTREAM_KEY)):
            async with self.client.post(
                    url + "/v1/responses", data=b'{"model":"model-A","stream":true}',
                    headers={"Authorization": "Bearer " + LOCAL_KEY,
                             "X-Synthetic-Scenario": "midstream_timeout"}) as response:
                with self.assertRaises(ClientError):
                    await response.read()
            await asyncio.wait_for(gateway.flush(), 3)
        row = (await self._wait_for_request_row(gateway))
        self.assertEqual(row["termination"], "upstream_timeout")
        self.assertIn("observer_failure", gateway.status()["monitoring_degraded"])
        stored = gateway.store.report() + json.dumps(gateway.store.read(kind="requests"))
        self.assertFalse(PRIVATE in stored or UPSTREAM_KEY in stored)

    async def test_invalid_downstream_content_lengths_never_reach_upstream_or_logs(self):
        url, gateway = await self.gateway()
        port = int(url.rsplit(":", 1)[1])
        malformed = [b"Content-Length: invalid", b"Content-Length: -1",
                     b"Content-Length: 1\r\nContent-Length: 2"]
        with patch("logging.Logger._log") as logged:
            for length in malformed:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    writer.write(b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n" + length +
                                 b"\r\nAuthorization: Bearer " + LOCAL_KEY.encode() +
                                 b"\r\nConnection: close\r\n\r\n" + PRIVATE.encode())
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), 2)
                    self.assertEqual(response.split(b"\r\n", 1)[0].split()[1], b"400", "Malformed framing was accepted")
                finally:
                    writer.close()
                    await writer.wait_closed()
        self.assertEqual(self.received, [])
        self.assertEqual(gateway.forwarded, 0)
        diagnostic = str(logged.call_args_list)
        self.assertFalse(PRIVATE in diagnostic or LOCAL_KEY in diagnostic or UPSTREAM_KEY in diagnostic)

    async def test_invalid_upstream_content_length_is_single_safe_transport_failure(self):
        calls = []

        async def malformed_reply(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                calls.append(1)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: invalid\r\n" +
                             b"X-Synthetic-Private: " + PRIVATE.encode() + b"\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(malformed_reply, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            url, gateway = await self.gateway(upstream={"base_url": f"http://127.0.0.1:{port}/v1",
                                                        "key_env": "HARDENING_UPSTREAM_TOKEN", "allow_local_http": True})
            with patch("logging.Logger._log") as logged:
                async with self.client.post(url + "/v1/responses", data=b'{}',
                                            headers={"Authorization": "Bearer " + LOCAL_KEY}) as response:
                    self.assertEqual(response.status, 502)
                    self.assertEqual(await response.text(), "Configured upstream unavailable.")
                await gateway.flush()
            self.assertEqual(calls, [1])
            rows = gateway.store.read(kind="requests")
            self.assertEqual(rows[0]["termination"], "upstream_transport_error")
            diagnostic = str(logged.call_args_list) + gateway.store.report() + json.dumps(rows)
            self.assertFalse(PRIVATE in diagnostic or LOCAL_KEY in diagnostic or UPSTREAM_KEY in diagnostic)
        finally:
            server.close()
            await server.wait_closed()

    async def test_downstream_write_backpressure_precedes_observation_and_preserves_entity(self):
        url, gateway = await self.gateway()
        blocked, resume = asyncio.Event(), asyncio.Event()
        original_write = web.StreamResponse.write

        async def stalled_write(response, data):
            if response._req is not None and response._req.app.get("gateway") is gateway:
                blocked.set()
                await resume.wait()
            return await original_write(response, data)

        with patch.object(web.StreamResponse, "write", stalled_write):
            response = await self.client.post(url + "/v1/responses", data=b'{"model":"model-A"}',
                                              headers={"Authorization": "Bearer " + LOCAL_KEY})
            read = asyncio.create_task(response.read())
            try:
                await asyncio.wait_for(blocked.wait(), 1)
                await asyncio.sleep(.05)
                self.assertFalse(read.done(), "Blocked downstream write was bypassed")
                self.assertEqual(gateway.observed, 0)
                self.assertEqual(gateway.parser_queue.qsize(), 0)
            finally:
                resume.set()
            entity = await asyncio.wait_for(read, 2)
        self.assertEqual(json.loads(entity)["output"][0]["content"][0]["text"], PRIVATE)
        await gateway.flush()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(gateway.observed, 1)

    async def test_concurrent_passive_requests_keep_exact_count_and_entities(self):
        url, gateway = await self.gateway()
        body = b'{"model":"model-A","input":"synthetic","stream":false}'

        async def receive():
            async with self.client.post(url + "/v1/responses", data=body,
                                        headers={"Authorization": "Bearer " + LOCAL_KEY}) as response:
                self.assertEqual(response.status, 200)
                return await response.read()

        entities = await asyncio.gather(*(receive() for _ in range(16)))
        await gateway.flush()
        self.assertTrue(all(entity == entities[0] for entity in entities))
        self.assertEqual(self.received, [body] * 16)
        self.assertEqual(gateway.forwarded, 16)
        self.assertEqual(gateway.observed, 16)
        self.assertEqual(gateway.dropped, 0)
        self.assertEqual(gateway.status()["probe"]["extra_requests"], 0)

    async def test_malformed_reasoning_effort_is_forwarded_unchanged_and_never_logged(self):
        url, gateway = await self.gateway()
        body = json.dumps({"model": "model-A", "input": PRIVATE,
                           "reasoning": {"effort": {"Authorization": UPSTREAM_KEY, "private": PRIVATE}},
                           "thinking": {"type": [PRIVATE, UPSTREAM_KEY]},
                           "stream": False}).encode()
        async with self.client.post(url + "/v1/responses", data=body,
                                    headers={"Authorization": "Bearer " + LOCAL_KEY}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "completed")
        await gateway.flush()
        self.assertEqual(self.received, [body])
        rows = gateway.store.read(kind="requests")
        self.assertIsNone(rows[0]["conditions"]["reasoning_effort"])
        self.assertFalse(rows[0]["conditions"]["thinking_enabled"])
        stored = gateway.store.report() + json.dumps(rows)
        for value in (PRIVATE, LOCAL_KEY, UPSTREAM_KEY, "Authorization"):
            self.assertFalse(value in stored, "Private content reached metadata storage")


if __name__ == "__main__":
    unittest.main()
