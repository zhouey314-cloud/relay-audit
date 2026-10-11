"""Local lifecycle and configuration contracts; no real credentials or providers."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay_audit.cli import main
from relay_audit.guard.config import DEFAULT, load, validate, write_new


class GuardConfigTests(unittest.TestCase):
    def test_default_is_passive_zero_budget_and_no_credentials(self):
        value = validate({})
        self.assertEqual(value["mode"], "passive")
        self.assertFalse(value["probe"]["enabled"])
        self.assertEqual(value["probe"]["daily_limit"], 0)
        self.assertEqual(value["listen_host"], "127.0.0.1")
        self.assertNotIn("api_key", json.dumps(value))

    def test_unsafe_configs_are_rejected_without_reading_keys(self):
        cases = [{"listen_host": "0.0.0.0"}, {"api_key": "secret"},
                 {"upstream": {"base_url": "http://remote.example/v1"}},
                 {"upstream": {"base_url": "https://user:password@remote.example/v1"}},
                 {"upstream": {"base_url": "https://remote.example/v1?key=secret"}},
                 {"upstream": {"base_url": "https://remote.example/../v1"}},
                 {"probe": {"enabled": True}}, {"observation": {"queue_size": 0}},
                 {"probe": {"hourly_limit": True}}, {"local_token_env": "not an env name"}]
        with patch("relay_audit.guard.config.os.environ", {}):
            for case in cases:
                with self.subTest(case=case), self.assertRaises((ValueError, TypeError)):
                    validate(case)

    def test_probe_target_cannot_embed_credentials(self):
        target = {"url": "https://relay.example/v1/responses", "protocol": "responses", "model": "m",
                  "snapshot_id": "m-snapshot", "key_env": "PROBE_KEY", "api_key": "secret"}
        with self.assertRaises(ValueError):
            validate({"probe": {"target": target}})

    def test_paths_resolve_relative_and_existing_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            write_new(path, {"state_dir": "relative-state"})
            original = path.read_bytes()
            self.assertEqual(load(path)["state_dir"], str((Path(directory) / "relative-state").resolve()))
            with self.assertRaises(FileExistsError):
                write_new(path, {})
            self.assertEqual(path.read_bytes(), original)

    def test_cli_backup_restore_creates_new_file_and_checks_digest(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", new_callable=io.StringIO):
            root = Path(directory)
            config, backup, restored = root / "config.json", root / "backup.json", root / "restored.json"
            self.assertEqual(main(["guard", "init", "--config", str(config), "--model", "mock-model"]), 0)
            self.assertEqual(main(["guard", "backup", "--config", str(config), "--out", str(backup)]), 0)
            self.assertEqual(main(["guard", "restore", "--backup", str(backup), "--out", str(restored)]), 0)
            self.assertEqual(load(config), load(restored))
            original = restored.read_bytes()
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main(["guard", "restore", "--backup", str(backup), "--out", str(restored)]), 2)
                value = json.loads(backup.read_text()); value["config"]["listen_port"] = 9999
                backup.write_text(json.dumps(value))
                self.assertEqual(main(["guard", "restore", "--backup", str(backup), "--out", str(root / "other.json")]), 2)
            self.assertEqual(restored.read_bytes(), original)

    def test_doctor_never_calls_upstream_and_marks_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_new(Path(directory) / "guard.json", {})
            with patch("sys.stdout", new_callable=io.StringIO) as output, patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
                result = main(["guard", "doctor", "--config", str(path)])
            self.assertEqual(result, 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["upstream_test"], "NOT_RUN")
            self.assertEqual(value["extra_model_requests"], 0)
            self.assertEqual(value["identity_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
