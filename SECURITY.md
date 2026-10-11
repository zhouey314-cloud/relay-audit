# Security and data handling

The runner reads credentials only from environment variables explicitly named by the user. Plaintext key fields and custom authentication headers in the configuration are rejected. No telemetry or remote grading service is used.

An API key must be sent to the configured endpoint for authentication. HTTPS is required except explicitly permitted loopback tests. Redirects are blocked, implicit proxy environment variables are ignored, and response error bodies are not retained. The user remains responsible for choosing a trustworthy endpoint and local TLS trust configuration.

By default, reports store answer hashes, scores, endpoint aliases, timestamps, and selected response metadata. Endpoint URLs and auth headers are excluded. Optional answer persistence applies exact replacement of known configured secrets; it is not a comprehensive personal-data scrubber. Inspect optional answer files before publishing.

Generated model code is never executed. The current coding probe grades the output of supplied code. Do not add host-level `exec`, `eval`, subprocess execution of generated code, or unrestricted plugins as a shortcut to a coding benchmark. A future execution-based benchmark must have explicit isolation and resource limits.

Report folders and local configurations are ignored by Git. Secrets must never be submitted in Issues, screenshots, PRs or fixture data. When a potential leak is found, identify the affected files without printing the key; revoke the affected credential through its provider if a real leak occurred.

There is no hosted service or private security-reporting mailbox for this prerelease. For a defect, open a GitHub Issue containing only a minimal reproduction with synthetic credentials. Do not include exploit material that exposes another user's private data.
