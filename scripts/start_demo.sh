#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_root"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install --no-deps --no-build-isolation -e .
fi
task_output="${1:-agentguard-demo/demo-$(date +%Y%m%d-%H%M%S)}"
exec .venv/bin/python -m relay_audit guard demo --out "$task_output"
