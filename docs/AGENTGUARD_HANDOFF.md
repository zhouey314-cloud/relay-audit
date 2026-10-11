# AgentGuard maintenance handoff

This document describes the 0.2.0 implementation. The archived 0.1.0 CODEX_HANDOFF.md is historical material; the current main product is continuous monitoring.

Start with README.md, guard.example.json, docs/AGENT_INTEGRATION.md and evals/PROJECT_EVAL_SPEC.md. Do not alter the operator's existing ~/.codex or subscription authentication. Generate a separate home/profile and use its returned launch_command; the tested Codex alpha required explicit CLI overrides because --ignore-user-config excluded the profile provider.

Runtime is relay_audit/guard: config.py validates; proxy.py transfers first and queues bounded observations; observer.py parses independent protocols without retaining body; detector.py groups comparable samples and emits descriptive rules; store.py redacts metadata, reports and budgets; notifier.py separates delivery; probe_worker.py runs explicitly budgeted synthetic review; adapters.py handles isolated configs and content-free hooks; harness.py supplies loopback fixtures and actual-client checks.

Run the acceptance script in a new output directory. Default CI has no installed Agent requirement; --clients explicitly runs local Codex/Claude against mocks. Treat all fixtures and references as synthetic_unverified. Real API testing requires new endpoint configuration, credentials explicitly named by the operator and a budget; never source a key from history or switch login methods.

Release gates: byte preservation, zero extra passive requests, automatic notification, no sensitive persistence, fail-open observation and config recovery. Tests do not certify identity. Preserve hard cases, do not lower thresholds or change answers to make a pass. Promote verified external failures only after human review.

In a release, verify the account and repository identity, avoid unrelated repositories/force pushes, run local acceptance, inspect staged files, then push and record real CI results. Publication authorization is session-specific. X drafts are copy-ready artifacts; publication to X is a separate action.
