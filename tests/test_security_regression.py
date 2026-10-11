"""Synthetic regression fixtures for the documented credential-handling contract."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay_audit.config import validate
from relay_audit.runner import demo_config, run
from relay_audit.transport import Result, extract


class MetadataRedactionTests(unittest.TestCase):
    def audit_metadata(self, secret, model, finish):
        config = demo_config()
        config["endpoints"] = config["endpoints"][:1]
        endpoint = config["endpoints"][0]
        endpoint.update(
            protocol="chat_completions",
            url="https://private.example.invalid/v1/chat/completions",
            key_env="AUDIT_SECURITY_REGRESSION_KEY",
        )
        config["families"] = ["modular"]
        config["variants"] = 1

        class EchoClient:
            def call(self, ep, probe, key, timeout):
                return extract(ep, {
                    "choices": [{"message": {"content": "{}"}, "finish_reason": finish}],
                    "model": model,
                }, probe)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AUDIT_SECURITY_REGRESSION_KEY": secret}
        ):
            output = Path(directory) / "report"
            run(validate(config), output, client=EchoClient())
            row = json.loads((output / "results.jsonl").read_text())
            contents = "".join(path.read_text() for path in output.iterdir())
        self.assertFalse(secret in contents, "A configured synthetic key was persisted")
        self.assertFalse(endpoint["url"] in contents, "The configured endpoint URL was persisted")
        self.assertLessEqual(len(row["reported_model"]), 200)
        self.assertLessEqual(len(row["finish_reason"]), 80)
        return row, contents

    def test_key_crossing_metadata_boundaries_is_redacted_before_clipping(self):
        secret = "SYNTHETIC_KEY_1234567890"
        row, contents = self.audit_metadata(secret, "M" * 195 + secret, "F" * 75 + secret)
        self.assertFalse(secret[:5] in contents, "A clipped synthetic key prefix was persisted")
        self.assertTrue(row["reported_model"].startswith("M" * 195))
        self.assertTrue(row["finish_reason"].startswith("F" * 75))

    def test_long_key_echo_is_redacted_before_metadata_clipping(self):
        secret = "SYNTHETIC_REVIEW_LONG_" + "Q" * 240
        row, contents = self.audit_metadata(secret, secret, secret)
        self.assertFalse(secret[:20] in contents, "A clipped long synthetic key was persisted")
        self.assertEqual(row["reported_model"], "[REDACTED]")
        self.assertEqual(row["finish_reason"], "[REDACTED]")

    def test_endpoint_url_crossing_metadata_boundaries_is_redacted_before_clipping(self):
        url = "https://private.example.invalid/v1/chat/completions"
        row, contents = self.audit_metadata(
            "SYNTHETIC_OTHER_KEY_1234567890", "M" * 180 + url, "F" * 55 + url
        )
        self.assertFalse("https://private" in contents, "A clipped endpoint URL was persisted")
        self.assertTrue(row["reported_model"].startswith("M" * 180))
        self.assertTrue(row["finish_reason"].startswith("F" * 55))


class ErrorPriorityTests(unittest.TestCase):
    def test_api_error_takes_precedence_over_tool_grading_failure(self):
        config = demo_config()
        config["endpoints"] = config["endpoints"][:1]
        config["families"] = ["tool_arguments"]
        config["variants"] = 1

        class ErrorClient:
            def call(self, ep, probe, key, timeout):
                return Result(error="http_error", status_code=429, grading_error="tool_selection")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            summary = run(validate(config), output, client=ErrorClient())
            row = json.loads((output / "results.jsonl").read_text())
        self.assertIsNone(row["passed"])
        self.assertEqual(row["grade_reason"], "http_error")
        endpoint = summary["endpoint_summaries"]["reference_demo"]
        self.assertEqual(endpoint["graded"], 0)
        self.assertEqual(endpoint["errors"], {"http_error": 1})


if __name__ == "__main__":
    unittest.main()
