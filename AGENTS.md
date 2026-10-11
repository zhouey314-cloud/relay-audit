# RelayAudit development contract

The main product is AgentGuard continuous passive monitoring. Preserve legacy probes as optional review/debug tools. Use evals/PROJECT_EVAL_SPEC.md as the release contract.

Never persist credentials, prompt/code/reply/reasoning bodies or raw lifecycle-hook inputs in default runtime state. Never add warnings to model replies, SSE or tool data. Fixed upstream routes and independent bounded monitoring queues are mandatory. Passive mode must produce exactly N upstream requests for N Agent requests, with no retries, substitutions or hidden probes.

Do not alter a developer's existing Agent subscription or configuration. Use isolated homes/profile files and digest-guarded restore. Real API testing needs explicitly provided current configuration and budget. Tests use fake credentials and loopback services.

Before changing detector, probe or AI-facing behavior, preserve applicable baseline/eval evidence. Run normal tests and the repeatable acceptance harness. Mock fixtures and generated answer keys are synthetic_unverified, not business Golden Sets. Never lower thresholds, remove hard cases or invent accuracy to pass a release.

Model identity is always unverified unless a future independently reviewed calibration establishes a narrower evidence claim. Same labels and indistinguishable observable behavior cannot identify hidden replacement. Public copy must retain this boundary.
