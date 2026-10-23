# AgentGuard evidence report

Model identity: **unverified**.
Descriptive metadata only. Real model identity calibration has not been completed.

## declared_model_mismatch

Time: 2026-10-23T15:52:07.857940+00:00
Rule version: guard-descriptive-v1

```json
{
  "id": "cbde963631666eac9cb89ed7",
  "event_type": "declared_model_mismatch",
  "observed_at_utc": "2026-10-23T15:52:07.857940+00:00",
  "session_id": "0fd94f4b35f8353b7197c463a1bd0ad1",
  "fingerprint": "601ace05c52f5a38ef487f8316d3431f",
  "occurrences": 1,
  "identity_status": "unverified",
  "rule_version": "guard-descriptive-v1",
  "evidence": {
    "request_id": "247c61c42915464da097b9c93e0d0b9a",
    "requested_model": "mock-model",
    "response_models": [
      "mock-other"
    ],
    "observed_fact": "Response model label is outside the configured allowed mapping.",
    "possible_explanations": [
      "Label mapping or endpoint routing changed; model weights remain unverified."
    ],
    "rule_version": "guard-descriptive-v1",
    "identity_status": "unverified"
  }
}
```

Reproduce with the same configured model, protocol and conditions; inspect the affected request IDs.
Possible explanations include endpoint routing, protocol handling, sampling and request conditions.
Next step: inspect metadata and, only if explicitly enabled and budgeted, run independent synthetic review.
