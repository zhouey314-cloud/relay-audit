# AgentGuard release contract

The main journey is Agent → local gateway → configured relay → unchanged reply,
with independent automatic incident notifications. Passive mode adds **zero**
model requests. The earlier 28 deterministic tests passed before this change;
continuous monitoring had no executable baseline (NOT_IMPLEMENTED).

All mock responses, probe cases, expected behaviors and performance loads in this
repository are **synthetic_unverified**. They test deterministic mechanics and
release rules, not model identity or a business Golden Set. Real model identity
calibration, accuracy, false-positive rates and paid-provider acceptance are
NOT_RUN. No external model calls are permitted by the local release suite.

## Gates

1. JSON/SSE entity content stays unchanged for Responses, Chat Completions and
   Anthropic Messages, including UTF-8 fragmentation, tool rounds and usage.
2. Streaming text reaches the downstream before the upstream completes. Role,
   heartbeat and reasoning events do not count as first visible text.
3. Unexpected declared model labels trigger a separate notification/report;
   aliases and explicit model switches follow their configured mapping.
4. Rate limits, output budgets and client cancellation keep distinct outcomes.
   Protocol truncation is observable; parser loss means monitoring degraded.
5. Comparable service baselines are isolated by session, model, protocol and
   relevant request conditions. Too few samples means 数据不足, never verified.
6. Detector, parser, queue, notification and storage failures do not add bytes
   to replies and do not block forwarding; loss is explicitly degraded.
7. N passive requests produce N upstream requests. No automatic retry or model
   substitution. Active probes require enabled mode plus explicit hard budgets.
8. Probe requests are separate sessions. Reference comparisons require matched
   conditions and successful self-variation calibration. No reference means
   uncalibrated; observationally identical replacement is unverified.
9. No prompts, code, replies, reasoning, keys, auth or raw hook payloads persist.
10. A real installed Agent must exercise its supported notification hook against
    a loopback mock. Harness-only adapters are labeled explicitly.
11. Attach/detach uses isolated configuration, backups and digest guards;
    changed user files are preserved. Current ~/.codex is untouched.
12. Startup/shutdown/restart, bounded storage cleanup, disabled WebSocket behavior
    and reproducible local overhead are reported with exact test conditions.

Critical failure in content preservation, secret retention, zero-extra-request,
fail-open observation, notification or configuration protection blocks release.
Traditional tests and the repeatable acceptance harness must both pass. Results
must report failed, improved, new and untested cases; never infer identity accuracy
from mock success. Protocol/tool failures, parsing loss, notification failure,
service changes and Unknown are separate error-analysis categories.
