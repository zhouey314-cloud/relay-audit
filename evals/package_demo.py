#!/usr/bin/env python3
"""Package only an explicit synthetic demo allowlist; runtime state stays local."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

REPO = Path(__file__).resolve().parents[1]


def package_demo(run, output):
    run, output = Path(run), Path(output)
    if run.is_symlink() or output.exists():
        raise ValueError("a regular demo directory and a new output file are required")
    sources = {"summary.json": run / "summary.json", "report.md": run / "report.md",
               "request.json": REPO / "examples/guard-synthetic/request.json",
               "README.md": REPO / "examples/guard-synthetic/README.md"}
    if any(path.is_symlink() or not path.is_file() for path in sources.values()):
        raise ValueError("regular allowlisted demo files are required")
    summary = json.loads(sources["summary.json"].read_text(encoding="utf-8"))
    if (summary.get("evidence_status") != "synthetic_unverified" or summary.get("real_api_verified") is not False
            or summary.get("extra_model_requests") != 0):
        raise ValueError("only the synthetic zero-extra-request demo may be packaged")
    contents = {name: path.read_bytes() for name, path in sources.items()}
    manifest = {"evidence_status": "synthetic_unverified", "identity_status": "unverified",
                "real_api": "NOT_RUN", "excluded": ["state", "credentials", "association keys", "raw runtime inputs"],
                "files": [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in sorted(contents.items())]}
    contents["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(contents.items()):
            archive.writestr(name, raw)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    package_demo(args.run, args.out)
    print("Synthetic demo packaged: 5 allowlisted files; no runtime state included.")


if __name__ == "__main__":
    main()
