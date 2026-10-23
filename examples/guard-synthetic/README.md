# Synthetic AgentGuard demo

This downloadable fixture contains only invented text and loopback test results.
`request.json` illustrates the Chat Completions request shape; the built-in demo
also exercises Responses, Anthropic Messages, tools and Unicode fragmentation.
It emits a normal synthetic label, a changed label and a request after the alert.
A changed label is an observable label mismatch, not verified model identity.

## Stable example and release

[View the versioned synthetic report](report.md) and [summary](summary.json).
Download the v0.2.1 wheel and synthetic-demo ZIP from the
[release](https://github.com/zhouey314-cloud/relay-audit/releases/tag/v0.2.1).
The report is an invented loopback run, not evidence of model identity.
Actions artifacts remain per-run test evidence with a retention deadline.

## Reproduce in about five minutes

Use Python 3.10, 3.12 or 3.13. Clone the repository and run from its root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m relay_audit guard demo --out agentguard-demo/quickstart-001
python evals/package_demo.py --run agentguard-demo/quickstart-001 --out synthetic-demo.zip
```

Choose a new output path for every run. Dependencies require internet on first
install; all demo requests then stay on `127.0.0.1`, using fake credentials.
The demo doesn't read or modify Codex/Claude configuration or call a real model.
`summary.json` should show 3 Agent requests, 3 upstream requests, 0 extra model
requests, an independent notification and a successful request after the alert.
`report.md` records the mechanical observations. Runtime state (including the
locally generated association key) stays in the run directory and is excluded
from the ZIP. Never substitute real prompts, credentials or recordings here.

## Repeat the measured comparison

```sh
python evals/run_acceptance.py --out acceptance-new --requests 100 --concurrency 1
python evals/run_acceptance.py --out acceptance-concurrent --requests 200 --concurrency 8
```

Each comparison uses identical synthetic JSON/SSE requests and response bytes in
direct, observation-disabled and full passive modes. `benchmark.json` contains
raw latency samples, median/p95, throughput including queue drain, Python peak
allocations, HTTP/entity errors and exact upstream counts. Any error aborts the
gate. Memory covers the entire Python process, not RSS; no saturation or public
internet latency claim follows. These fixtures are `synthetic_unverified`.
Real paid APIs, actual Agent integration, identity calibration, human judgement
and semantic accuracy remain **NOT_TESTED** by this demo.

The newly authored request fixture is covered by the repository MIT license.
