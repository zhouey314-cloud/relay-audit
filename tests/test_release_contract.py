"""Offline release contract checks; never uses real credentials or providers."""

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from relay_audit.cli import main


class PlanContractTests(unittest.TestCase):
    def test_example_plan_does_not_read_keys_or_open_http(self):
        class ForbiddenKeyEnvironment(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("plan must not read configured credentials")

            def __getitem__(self, key):
                raise AssertionError("plan must not read configured credentials")

        config = Path(__file__).resolve().parents[1] / "config.example.json"
        with patch("relay_audit.runner.os", SimpleNamespace(environ=ForbiddenKeyEnvironment())), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            status = main(["plan", "--config", str(config)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["planned_requests"], 54)


if __name__ == "__main__":
    unittest.main()
