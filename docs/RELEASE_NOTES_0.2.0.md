# RelayAudit 0.2.0 — AgentGuard

AgentGuard becomes the main product: a loopback, fixed-upstream API gateway with passive continuous observation, automatic independent alerts and metadata-only evidence reports. Legacy probes and reports remain available and supply optional independent background review.

Implemented JSON and incremental HTTP SSE forwarding for Responses, Chat Completions and Anthropic Messages, tool rounds/fragments, cache usage, cancellation, upstream failure and auxiliary routes. WebSocket is explicitly unsupported (426), with an independently configured Codex SSE provider.

Default passive mode adds zero model requests. Active review needs explicit endpoints, enabled mode and persisted hard hourly/daily request budgets. Reference self-calibration and matching declared conditions gate descriptive behavioral drift; model identity remains unverified.

Automatic terminal notifications and tested Codex notify / Claude Code Stop adapters are isolated from model replies, SSE and tool data. Slow/failing monitoring, parser, storage and notifier paths degrade observability while preserving forwarding. Guard CLI provides init/start/status/report/doctor/stop/detach/backup/restore/cleanup and isolated client launch configuration.

Local acceptance uses synthetic upstreams and installed clients; real API and real model identity calibration remain NOT_RUN. No accuracy claims. See VALIDATION.md and evals/PROJECT_EVAL_SPEC.md for evidence.
