# Security and data handling

AgentGuard binds only to loopback and forwards fixed API routes to one configured HTTPS upstream. Explicit local HTTP is allowed only for loopback mocks. Authentication uses a local gateway token plus an upstream key read from environment variables. Configuration rejects plaintext credential fields, arbitrary upstream destinations and credential-bearing URLs. Control and upstream requests ignore environment proxies and refuse redirects. Subscription OAuth credentials and existing Agent authentication files are not read or modified.

Default storage contains metadata only. Prompts, code, replies, reasoning, tool arguments, Authorization, Cookie and hook conversation payloads are not persisted. Provider labels are untrusted, bounded, redacted against all known configured secrets and never executed. Usage is upstream-reported. HMAC session associations use a locally generated key. State directories/files use 700/600 permissions; default retention is 14 days. See docs/PRIVACY_AND_LIMITS.md for the exact limits and cleanup behavior.

Observer, detector and notification queues are bounded. Slow/faulting parser, detector, storage and notifier paths cannot insert bytes into model replies. Lost observation produces monitoring_degraded rather than certification. Gateway process failure still interrupts connections; safe config detach and restart are available.

The retained manual probe CLI has optional --save-responses. It saves only explicitly requested, known-secret-redacted answer text and is not a general personal-data scrubber. Never publish those private files. Generated model code is not executed; local coding probes use deterministic answer keys.

There is no hosted telemetry, remote judge, paid test or private security mailbox. For defects, use an Issue with synthetic credentials and a minimal reproduction. Do not attach real keys, customer material, conversation logs or another person's private data. Real model identity calibration and accuracy remain unverified.
