"""The downloadable synthetic demo never copies runtime state."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from evals.package_demo import package_demo


class SyntheticDemoPackageTests(unittest.TestCase):
    def prepare(self, root):
        run = root / "demo"
        run.mkdir()
        (run / "summary.json").write_text(json.dumps({"evidence_status": "synthetic_unverified",
                                                       "real_api_verified": False, "extra_model_requests": 0}))
        (run / "report.md").write_text("Synthetic mechanical observations; identity unverified.\n")
        state = run / "state"
        state.mkdir()
        (state / ".association-key").write_text("SYNTHETIC_RUNTIME_STATE_MUST_NOT_BE_PACKAGED")
        return run

    def test_package_uses_fixed_allowlist_and_manifest_excludes_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.prepare(root)
            output = root / "demo.zip"
            manifest = package_demo(run, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {"README.md", "summary.json", "report.md",
                                                          "request.json", "MANIFEST.json"})
                for entry in manifest["files"]:
                    raw = archive.read(entry["path"])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                    self.assertNotIn(b"SYNTHETIC_RUNTIME_STATE_MUST_NOT_BE_PACKAGED", raw)
            with self.assertRaises(ValueError):
                package_demo(run, output)

    def test_missing_synthetic_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.prepare(root)
            (run / "summary.json").write_text('{"real_api_verified":true}')
            with self.assertRaises(ValueError):
                package_demo(run, root / "demo.zip")
            self.assertFalse((root / "demo.zip").exists())

    def test_symlinked_allowlisted_input_is_refused_without_reading_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.prepare(root)
            (run / "report.md").unlink()
            (run / "report.md").symlink_to(run / "state/.association-key")
            with self.assertRaises(ValueError):
                package_demo(run, root / "demo.zip")
            self.assertFalse((root / "demo.zip").exists())


if __name__ == "__main__":
    unittest.main()
