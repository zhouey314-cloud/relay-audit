"""Real CLI subprocess lifecycle against a synthetic loopback-only upstream."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from relay_audit.guard.harness import create_mock_app


class GuardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_status_stop_restart_detach_and_original_route(self):
        project_root = Path(__file__).resolve().parents[1]
        processes = []
        with tempfile.TemporaryDirectory(prefix="relayguard-lifecycle-") as directory:
            root = Path(directory)
            config_path = root / "guard.json"
            state = root / "state"
            runtime = state / "runtime.json"
            home = root / "isolated-codex-home"
            home.mkdir()
            local_token = "fake-lifecycle-local-token"
            environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
            environment.update({"PYTHONPATH": str(project_root), "NO_PROXY": "127.0.0.1,localhost",
                                "RELAY_LIFECYCLE_TOKEN": local_token,
                                "RELAY_LIFECYCLE_UPSTREAM_KEY": "fake-lifecycle-upstream-key"})

            async def cli(*arguments):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "relay_audit", "guard", *arguments,
                    cwd=root, env=environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 7)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    self.fail("lifecycle CLI command exceeded its bounded timeout")
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertNotIn(local_token.encode(), stdout + stderr)
                self.assertNotIn(environment["RELAY_LIFECYCLE_UPSTREAM_KEY"].encode(), stdout + stderr)
                return json.loads(stdout)

            async def start():
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "relay_audit", "guard", "start", "--config", str(config_path),
                    cwd=root, env=environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                processes.append(process)
                for _ in range(100):
                    if process.returncode is not None:
                        _, stderr = await process.communicate()
                        self.fail("gateway exited before runtime readiness: " + stderr.decode())
                    try:
                        info = json.loads(runtime.read_text())
                        if info.get("pid") == process.pid and info.get("port", 0) > 0:
                            return process, f"http://127.0.0.1:{info['port']}/v1"
                    except (OSError, ValueError):
                        pass
                    await asyncio.sleep(.05)
                self.fail("gateway did not write runtime readiness within five seconds")

            async def stop(process):
                result = await cli("stop", "--config", str(config_path))
                self.assertTrue(result["stopping"])
                stdout, stderr = await asyncio.wait_for(process.communicate(), 7)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertFalse(runtime.exists(), "runtime must be cleared after a clean stop")
                self.assertNotIn(local_token.encode(), stdout + stderr)
                self.assertNotIn(environment["RELAY_LIFECYCLE_UPSTREAM_KEY"].encode(), stdout + stderr)

            async def request(session, base_url, token):
                async with session.post(base_url + "/responses", json={"model": "mock-model", "stream": True,
                                                                        "input": "synthetic lifecycle task"},
                                        headers={"Authorization": "Bearer " + token, "X-Session-Id": "synthetic-lifecycle"}) as response:
                    payload = await response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"LIFECYCLE_SYNTHETIC_OK", payload)
                    self.assertIn(b"response.completed", payload)

            mock = create_mock_app({"text": "LIFECYCLE_SYNTHETIC_OK"})
            async with TestServer(mock) as upstream:
                upstream_url = str(upstream.make_url("/v1"))
                config_path.write_text(json.dumps({"listen_port": 0, "state_dir": str(state),
                                                   "local_token_env": "RELAY_LIFECYCLE_TOKEN",
                                                   "upstream": {"alias": "synthetic-lifecycle", "base_url": upstream_url,
                                                                "key_env": "RELAY_LIFECYCLE_UPSTREAM_KEY", "allow_local_http": True},
                                                   "mode": "passive"}))
                original = ('# Existing private profile, synthetic endpoint only\n'
                            'model = "mock-model"\nmodel_provider = "original"\n'
                            '[model_providers.original]\nname = "Original synthetic route"\n'
                            f'base_url = {json.dumps(upstream_url)}\n'
                            'env_key = "RELAY_LIFECYCLE_UPSTREAM_KEY"\nwire_api = "responses"\n').encode()
                profile_path = home / "relayguard.config.toml"
                profile_path.write_bytes(original)
                try:
                    async with ClientSession() as session:
                        first, guard_url = await start()
                        attached = await cli("attach-codex", "--config", str(config_path), "--codex-home", str(home), "--model", "mock-model")
                        self.assertNotEqual(profile_path.read_bytes(), original)
                        await request(session, guard_url, local_token)
                        status = await cli("status", "--config", str(config_path))
                        self.assertTrue(status["gateway_reachable"])
                        self.assertEqual(status["requests_forwarded"], 1)
                        self.assertEqual(status["identity_status"], "unverified")
                        self.assertEqual(status["probe"]["extra_requests"], 0)
                        await stop(first)
                        stopped = await cli("status", "--config", str(config_path))
                        self.assertFalse(stopped["gateway_reachable"])

                        restarted, guard_url = await start()
                        await request(session, guard_url, local_token)
                        status = await cli("status", "--config", str(config_path))
                        self.assertTrue(status["gateway_reachable"])
                        self.assertEqual(status["requests_forwarded"], 1)
                        self.assertEqual(status["probe"]["extra_requests"], 0)
                        await stop(restarted)

                        restored = await cli("detach", "--manifest", attached["manifest_path"])
                        self.assertTrue(restored["detached"])
                        self.assertEqual(profile_path.read_bytes(), original)
                        # Read the restored endpoint, then demonstrate the original
                        # direct route still works after stopping/detaching Guard.
                        restored_base = next(json.loads(line.split("=", 1)[1].strip())
                                             for line in profile_path.read_text().splitlines() if line.startswith("base_url ="))
                        await request(session, restored_base, environment["RELAY_LIFECYCLE_UPSTREAM_KEY"])
                        self.assertEqual(mock["mock_state"]["request_count"], 3)
                        self.assertFalse(runtime.exists())
                        self.assertFalse((home / "relayguard-launch.py").exists())
                finally:
                    for process in processes:
                        if process.returncode is None:
                            process.terminate()
                            try:
                                await asyncio.wait_for(process.communicate(), 5)
                            except asyncio.TimeoutError:
                                process.kill()
                                await process.communicate()


if __name__ == "__main__":
    unittest.main()
