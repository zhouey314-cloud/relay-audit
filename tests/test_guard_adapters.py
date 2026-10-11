import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from relay_audit.guard import adapters
from relay_audit.guard.harness import create_mock_app, run_harness


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def attach(self):
        return adapters.attach_codex(codex_home=self.root / "codex", guard_url="http://127.0.0.1:18711/v1",
                                     model="mock-model", state_dir=self.root / "state")

    def test_isolated_current_provider_and_profile(self):
        result = self.attach()
        content = Path(result["configuration_files"][0]).read_text()
        self.assertIn('wire_api = "responses"', content)
        self.assertIn('supports_websockets = false', content)
        self.assertIn('env_key = "RELAY_GUARD_TOKEN"', content)
        self.assertNotIn("[profiles.", content)
        self.assertIn("relayguard.config.toml", result["configuration_files"][0])
        self.assertFalse((self.root / "codex" / "auth.json").exists())
        self.assertEqual(result["launch_argv"][2], "--ignore-user-config")

    def test_active_subscription_trees_are_protected(self):
        with self.assertRaises(ValueError):
            adapters.attach_codex(codex_home=Path.home() / ".codex", guard_url="http://127.0.0.1:1",
                                  model="test", state_dir=self.root)
        with self.assertRaises(ValueError):
            adapters.attach_claude(settings_dir=Path.home() / ".claude", guard_url="http://127.0.0.1:1",
                                   model="test", state_dir=self.root)

    def test_detach_restores_exact_original_and_preserves_later_change(self):
        home = self.root / "codex"
        home.mkdir()
        config = home / "relayguard.config.toml"
        original = b'# original\nmodel = "original"\n'
        config.write_bytes(original)
        result = self.attach()
        adapters.detach(result["manifest_path"])
        self.assertEqual(config.read_bytes(), original)
        result = self.attach()
        config.write_text('model = "user-change"\n')
        with self.assertRaisesRegex(ValueError, "preserving user changes"):
            adapters.detach(result["manifest_path"])
        self.assertEqual(config.read_text(), 'model = "user-change"\n')

    def test_detach_refuses_modified_backup(self):
        home = self.root / "codex"
        home.mkdir()
        (home / "relayguard.config.toml").write_text("original")
        result = self.attach()
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        Path(manifest["files"][0]["backup_path"]).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "backup hash mismatch"):
            adapters.detach(result["manifest_path"])

    def test_notify_deduplicates_and_discards_payload_and_untrusted_message(self):
        state = self.root / "state"
        state.mkdir()
        sensitive = "SYNTHETIC_PRIVATE_PROMPT sk-test-do-not-store"
        (state / "incidents.jsonl").write_text(json.dumps({"incident_id": "incident_1", "type": "declared_model_mismatch", "message": sensitive}) + "\n")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            first = adapters.notify(state)
            second = adapters.notify(state)
        self.assertEqual(first["new_incidents"], 1)
        self.assertEqual(second["new_incidents"], 0)
        self.assertNotIn(sensitive, captured.getvalue())
        # Actual hook shape: Codex JSON argv and Claude JSON stdin are both ignored.
        process = subprocess.run([sys.executable, adapters.__file__, "notify", "--state-dir", str(state),
                                  json.dumps({"type": "agent-turn-complete", "input-messages": [sensitive]})],
                                 input=json.dumps({"last_assistant_message": sensitive}), text=True, capture_output=True)
        self.assertEqual(process.returncode, 0)
        receipts = (state / "hook_receipts.jsonl").read_text()
        self.assertNotIn(sensitive, receipts)
        self.assertEqual(process.stdout, "")

    def test_claude_api_settings_stop_hook_has_no_credential(self):
        result = adapters.attach_claude(settings_dir=self.root / "claude", guard_url="http://127.0.0.1:1/v1",
                                       model="claude-sonnet-5", state_dir=self.root / "state")
        settings = json.loads(Path(result["configuration_files"][0]).read_text())
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1")
        self.assertNotIn("ANTHROPIC_API_KEY", settings["env"])
        self.assertIn("Stop", settings["hooks"])


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_protocols_unicode_fragments_and_tool_multiturn(self):
        for protocol, route, terminal in [("responses", "responses", b"response.completed"),
                                           ("chat_completions", "chat/completions", b"[DONE]"),
                                           ("anthropic_messages", "messages", b"message_stop")]:
            app = create_mock_app({"tool_first": True, "chunk_sizes": [1, 2, 3], "labels": ["model-A", "model-B"]})
            async with TestServer(app) as server:
                async with ClientSession() as session:
                    body = {"model": "model-A", "stream": True, "input": [], "messages": [], "max_tokens": 64}
                    async with session.post(server.make_url("/v1/" + route), json=body) as response:
                        payload = await response.read()
                self.assertIn(terminal, payload)
                self.assertIn("杭", payload.decode())
                self.assertIn("州", payload.decode())
                # Complete JSON lines reconstruct valid tool arguments despite network fragmentation.
                events = [json.loads(line[6:]) for line in payload.decode().splitlines()
                          if line.startswith("data: {")]
                self.assertTrue(events)
                if protocol == "chat_completions":
                    arguments = "".join(call["function"].get("arguments", "") for event in events
                                        for choice in event.get("choices", [])
                                        for call in choice.get("delta", {}).get("tool_calls", []))
                    self.assertEqual(json.loads(arguments), {"city": "杭州"})
                summary = await run_harness(str(server.make_url("/v1")), "fake-local-key", protocol=protocol, count=2, tool_multi_turn=True)
                self.assertEqual(summary["agent_requests"], 2)
                self.assertEqual(app["mock_state"]["request_count"], 3)
                self.assertTrue(app["mock_state"]["requests"][-1]["tool_output_present"])
                self.assertNotIn("synthetic Agent task", json.dumps(app["mock_state"]))

    async def test_controlled_http_failure_is_single_upstream_request(self):
        app = create_mock_app({"http_statuses": [429]})
        async with TestServer(app) as server:
            result = await run_harness(str(server.make_url("/v1")), "fake", count=1)
            self.assertEqual(result["observations"][0]["http_status"], 429)
            self.assertEqual(app["mock_state"]["request_count"], 1)


if __name__ == "__main__":
    unittest.main()
