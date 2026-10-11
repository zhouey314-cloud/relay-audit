import copy
import json
import os
import tempfile
import threading
import unittest
from http.client import BadStatusLine, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from relay_audit.config import validate
from relay_audit.probes import build, canonical, grade
from relay_audit.report import html_report, summarize
from relay_audit.runner import demo_config, plan, run
from relay_audit.transport import HTTPClient, Result, extract, payload


class ProbeTests(unittest.TestCase):
    def test_seed_reproduction_and_fresh_variants(self):
        first = build(9, 2)
        second = build(9, 2)
        changed = build(10, 2)
        self.assertEqual([p.digest for p in first], [p.digest for p in second])
        self.assertNotEqual([p.digest for p in first], [p.digest for p in changed])
        self.assertEqual(len(first), len(set(p.digest for p in first)))
        for task in first:
            self.assertEqual(grade(task, canonical(task.expected)), (True, "matched"))

    def test_strict_answers_reject_boolean_numeric_coercion_and_extra_keys(self):
        task = next(p for p in build(9) if p.family == "modular")
        self.assertFalse(grade(task, '{"answer": true}')[0])
        self.assertFalse(grade(task, canonical({"answer": task.expected["answer"], "extra": 1}))[0])
        self.assertFalse(grade(task, '```json\n' + canonical(task.expected) + '\n```')[0])
        self.assertFalse(grade(task, '{"answer": 0, "answer": 1}')[0])
        self.assertFalse(grade(task, '{"answer": NaN}')[0])
        self.assertEqual(grade(task, '[' * 10000 + '0' + ']' * 10000), (False, "invalid_json"))

    def test_context_keys_survive_noise(self):
        for task in build(991, 4):
            if task.family.startswith("context_"):
                for key, value in task.expected.items():
                    self.assertIn(f"{key}={value}", task.prompt)
                self.assertEqual(task.prompt.count("alpha="), 1)

    def test_invalid_families_fail_without_silently_reducing_coverage(self):
        with self.assertRaises(ValueError):
            build(1, families=["unknown"])


class ConfigurationTests(unittest.TestCase):
    def test_budget_checked_before_network_or_output(self):
        config = validate(demo_config())
        config["max_requests"] = 1
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-exist"
            with self.assertRaises(ValueError):
                run(config, output)
            self.assertFalse(output.exists())

    def test_full_urls_plaintext_keys_and_unsafe_http(self):
        config = demo_config()
        ep = config["endpoints"][0]
        ep.update(protocol="chat_completions", url="https://example.invalid/v1/chat/completions", key_env="TEST_KEY")
        for bad_url in ("http://example.invalid/v1", "https://user:password@example.invalid/v1",
                        "https://example.invalid/v1?key=anything"):
            bad = copy.deepcopy(config)
            bad["endpoints"][0]["url"] = bad_url
            with self.assertRaises(ValueError):
                validate(bad)
        ep["api_key"] = "do-not-store"
        with self.assertRaises(ValueError):
            validate(config)

    def test_mismatched_settings_are_inconclusive(self):
        config = validate(demo_config())
        config["endpoints"][1]["parameters"] = {"temperature": 1}
        with tempfile.TemporaryDirectory() as directory:
            summary = run(config, Path(directory) / "run")
        self.assertFalse(summary["comparisons"][0]["declared_conditions_match"])
        self.assertEqual(summary["comparisons"][0]["interpretation"], "inconclusive_conditions_or_samples")


class ReportTests(unittest.TestCase):
    def test_demo_detects_capability_gap_without_certifying_identity(self):
        config = validate(demo_config())
        with tempfile.TemporaryDirectory() as directory:
            summary = run(config, Path(directory) / "demo")
            rows = [json.loads(line) for line in (Path(directory) / "demo/results.jsonl").read_text().splitlines()]
        self.assertTrue(summary["synthetic"])
        self.assertEqual(len(rows), plan(config)["planned_requests"])
        self.assertEqual(summary["endpoint_summaries"]["reference_demo"]["passes"], 27)
        self.assertLess(summary["comparisons"][0]["pass_rate_delta_pp"], 0)
        self.assertEqual(summary["comparisons"][0]["identity_verdict"], "not_verified")
        self.assertEqual(len({row["reported_model"] for row in rows if row["reported_model"] is not None}), 1)

    def test_errors_are_not_model_failures_or_grading_denominator(self):
        config = validate(demo_config())
        common = {"endpoint": "relay_demo", "probe_digest": "same", "category": "reasoning",
                  "latency_ms": 1, "reported_model": None}
        result = summarize(config, [{**common, "passed": None, "error": "http_error"}], "complete", True)
        self.assertEqual(result["endpoint_summaries"]["relay_demo"]["graded"], 0)
        self.assertEqual(result["endpoint_summaries"]["relay_demo"]["errors"], {"http_error": 1})
        self.assertEqual(result["comparisons"][0]["paired_graded"], 0)

    def test_secrets_and_urls_excluded_even_when_provider_echoes_them(self):
        secret = "FAKE_TEST_KEY_NEVER_A_REAL_CREDENTIAL"
        config = demo_config()
        config["endpoints"] = [config["endpoints"][0]]
        ep = config["endpoints"][0]
        ep.update(protocol="chat_completions", url="https://example.invalid/v1/chat/completions", key_env="AUDIT_TEST_KEY")
        config["families"] = ["unicode"]
        config["variants"] = 1

        class EchoClient:
            def call(self, ep, probe, key, timeout):
                return Result(answer=key, reported_model=ep["url"] + " " + key, latency_ms=2)

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AUDIT_TEST_KEY": secret}):
            output = Path(directory) / "run"
            run(validate(config), output, True, EchoClient())
            contents = "".join(path.read_text() for path in output.iterdir())
            self.assertNotIn(secret, contents)
            self.assertNotIn(ep["url"], contents)
            self.assertIn("[REDACTED]", contents)

    def test_html_escapes_external_values(self):
        summary = summarize(validate(demo_config()), [], "<script>alert(1)</script>", True)
        rendered = html_report(summary)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            run(validate(demo_config()), output)
            before = (output / "summary.json").read_bytes()
            with self.assertRaises(FileExistsError):
                run(validate(demo_config()), output)
            self.assertEqual(before, (output / "summary.json").read_bytes())

    def test_wall_budget_retains_partial_evidence(self):
        config = validate(demo_config())
        config["max_wall_seconds"] = 1
        with tempfile.TemporaryDirectory() as directory, patch("relay_audit.runner.time.monotonic", side_effect=[0, 0, 2]):
            summary = run(config, Path(directory) / "partial")
        self.assertEqual(summary["run_status"], "wall_budget_exhausted")
        self.assertEqual(sum(item["attempts"] for item in summary["endpoint_summaries"].values()), 1)


class ProtocolTests(unittest.TestCase):
    def test_native_tool_request_and_reply(self):
        task = next(p for p in build(8) if p.tool)
        ep = validate(demo_config())["endpoints"][0]
        ep["protocol"] = "anthropic_messages"
        request = payload(ep, task)
        self.assertEqual(request["tools"][0]["input_schema"]["required"], ["item", "count"])
        response = {"content": [{"type": "tool_use", "name": "record_result", "input": task.expected}],
                    "model": "unverified", "usage": {"input_tokens": 5, "output_tokens": 8}}
        self.assertTrue(grade(task, extract(ep, response, task).answer)[0])
        response["content"].append(response["content"][0])
        result = extract(ep, response, task)
        self.assertIsNone(result.error)
        self.assertEqual(result.grading_error, "tool_call_count")

    def test_wrong_function_name_is_not_a_pass(self):
        task = next(p for p in build(8) if p.tool)
        ep = validate(demo_config())["endpoints"][0]
        response = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "wrong", "arguments": canonical(task.expected)}}]}}]}
        result = extract(ep, response, task)
        self.assertIsNone(result.error)
        self.assertEqual(result.grading_error, "tool_selection")

    def test_truncation_finish_reason_is_preserved(self):
        task = build(8)[0]
        ep = validate(demo_config())["endpoints"][0]
        response = {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}
        result = extract(ep, response, task)
        self.assertEqual(result.finish_reason, "length")
        self.assertFalse(grade(task, result.answer)[0])


class OfflineResponseRegressionTests(unittest.TestCase):
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return self.body

    def client_for(self, body):
        response = self.Response(body if isinstance(body, bytes) else json.dumps(body).encode())

        class Opener:
            def open(self, *args, **kwargs):
                return response

        client = HTTPClient()
        client.opener = Opener()
        return client

    def config_for(self, protocol):
        config = demo_config()
        config["endpoints"] = [config["endpoints"][0]]
        config["endpoints"][0].update(protocol=protocol, url="https://example.invalid/audit",
                                      key_env="OFFLINE_TEST_KEY")
        config.update(families=["tool_arguments"], variants=1)
        return validate(config)

    def tool_reply(self, protocol, task, name="record_result", count=1, text=""):
        if protocol == "anthropic_messages":
            blocks = [{"type": "tool_use", "name": name, "input": task.expected} for _ in range(count)]
            if text:
                blocks.append({"type": "text", "text": text})
            return {"content": blocks, "model": "unverified", "stop_reason": "tool_use"}
        calls = [{"function": {"name": name, "arguments": canonical(task.expected)}} for _ in range(count)]
        return {"choices": [{"message": {"tool_calls": calls, "content": text or None},
                              "finish_reason": "tool_calls"}], "model": "unverified"}

    def test_tool_behavior_failures_are_graded_end_to_end_in_both_protocols(self):
        cases = [("wrong", 1, "", "tool_selection"),
                 ("record_result", 0, "", "tool_call_count"),
                 ("record_result", 2, "", "tool_call_count"),
                 ("record_result", 1, "Extra ordinary text", "unexpected_text")]
        for protocol in ("chat_completions", "anthropic_messages"):
            config = self.config_for(protocol)
            task = build(config["seed"], 1, config["families"])[0]
            for name, count, text, reason in cases:
                with self.subTest(protocol=protocol, reason=reason, count=count):
                    body = self.tool_reply(protocol, task, name, count, text)
                    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OFFLINE_TEST_KEY": "FAKE"}):
                        output = Path(directory) / "run"
                        summary = run(config, output, client=self.client_for(body))
                        row = json.loads((output / "results.jsonl").read_text())
                    endpoint = summary["endpoint_summaries"]["reference_demo"]
                    self.assertEqual((endpoint["attempts"], endpoint["graded"], endpoint["passes"]), (1, 1, 0))
                    self.assertEqual(endpoint["errors"], {})
                    self.assertFalse(row["passed"])
                    self.assertEqual(row["grade_reason"], reason)
                    self.assertEqual(row["status_code"], 200)

    def test_missing_and_malformed_tool_fields_remain_distinct(self):
        for protocol in ("chat_completions", "anthropic_messages"):
            config = self.config_for(protocol)
            ep = config["endpoints"][0]
            task = build(config["seed"], 1, config["families"])[0]
            if protocol == "anthropic_messages":
                malformed = [{"content": "wrong type"}, {"content": [None]},
                             {"content": [{"type": "tool_use", "name": "record_result", "input": "wrong type"}]}]
            else:
                malformed = [{"choices": [{"message": {"tool_calls": "wrong type"}}]},
                             {"choices": [{"message": {"tool_calls": [None]}}]},
                             {"choices": [{"message": {"tool_calls": [{"function": {"name": "record_result", "arguments": {}}}]}}]}]
                missing = self.client_for({"choices": [{"message": {}}]}).call(ep, task, "FAKE", 2)
                self.assertIsNone(missing.error)
                self.assertEqual(missing.grading_error, "tool_call_count")
            for body in malformed:
                with self.subTest(protocol=protocol, body=body):
                    result = self.client_for(body).call(ep, task, "FAKE", 2)
                    self.assertEqual(result.error, "invalid_response")
                    self.assertIsNone(result.grading_error)
                    self.assertEqual(result.status_code, 200)

    def test_http_parser_errors_are_contained_without_exception_text(self):
        config = self.config_for("chat_completions")
        ep = config["endpoints"][0]
        task = build(config["seed"], 1, config["families"])[0]
        secret = "FAKE-CREDENTIAL-MUST-NOT-ENTER-RESULTS"

        class Opener:
            def open(self, *args, **kwargs):
                raise BadStatusLine(secret)

        client = HTTPClient()
        client.opener = Opener()
        result = client.call(ep, task, "FAKE", 2)
        self.assertEqual(result.error, "invalid_response")
        self.assertNotIn(secret, str(result))

        class PartialResponse(self.Response):
            def read(self, *args):
                raise IncompleteRead(secret.encode(), 100)

        class PartialOpener:
            def open(self, *args, **kwargs):
                return PartialResponse(b"")

        client.opener = PartialOpener()
        result = client.call(ep, task, "FAKE", 2)
        self.assertEqual(result.error, "invalid_response")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(secret, str(result))

    def test_deep_protocol_json_is_invalid_response_with_observed_status(self):
        config = self.config_for("chat_completions")
        ep = config["endpoints"][0]
        task = build(config["seed"], 1, config["families"])[0]
        raw = ("[" * 10000 + "0" + "]" * 10000).encode()
        result = self.client_for(raw).call(ep, task, "FAKE", 2)
        self.assertEqual(result.error, "invalid_response")
        self.assertEqual(result.status_code, 200)


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, request, self.headers.get("Authorization"), self.headers.get("x-api-key")))
                if self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", "/sink")
                    self.end_headers()
                    return
                if self.path == "/limited":
                    self.send_response(429)
                    self.end_headers()
                    self.wfile.write(b"DO_NOT_LOG_ERROR_BODY")
                    return
                if self.path == "/messages":
                    body = {"content": [{"type": "text", "text": '{"answer": 42}'}], "model": "label"}
                else:
                    body = {"choices": [{"message": {"content": '{"answer": 42}'}, "finish_reason": "stop"}],
                            "model": "label", "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def ep(self, path, protocol="chat_completions"):
        config = demo_config()
        config["endpoints"] = [config["endpoints"][0]]
        config["endpoints"][0].update(protocol=protocol, url=self.url + path, key_env="LOCAL_TEST_KEY")
        return validate(config, allow_local_http=True)["endpoints"][0]

    def test_chat_and_native_protocol_make_exact_http_requests(self):
        task = build(8)[0]
        for path, protocol in (("/chat", "chat_completions"), ("/messages", "anthropic_messages")):
            result = HTTPClient().call(self.ep(path, protocol), task, "FAKE-LOCAL-KEY", 2)
            self.assertEqual(result.answer, '{"answer": 42}')
            self.assertIsNone(result.error)
        self.assertEqual(self.seen[0][2], "Bearer FAKE-LOCAL-KEY")
        self.assertEqual(self.seen[1][3], "FAKE-LOCAL-KEY")

    def test_redirect_does_not_forward_credentials(self):
        result = HTTPClient().call(self.ep("/redirect"), build(8)[0], "FAKE-LOCAL-KEY", 2)
        self.assertEqual(result.error, "redirect_blocked")
        self.assertEqual(len(self.seen), 1)

    def test_429_is_not_retried_and_error_body_not_retained(self):
        result = HTTPClient().call(self.ep("/limited"), build(8)[0], "FAKE-LOCAL-KEY", 2)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn("DO_NOT_LOG_ERROR_BODY", str(result))


if __name__ == "__main__":
    unittest.main()
