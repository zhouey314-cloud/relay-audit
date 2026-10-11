# AgentGuard evidence report

Model identity: **unverified**.
Descriptive metadata only. Real model identity calibration has not been completed.

## declared_model_mismatch

Time: 2026-10-11T03:26:15.437560+00:00
Rule version: guard-descriptive-v1

```json
{
  "id": "0972de222b8b3acdde22557b",
  "event_type": "declared_model_mismatch",
  "observed_at_utc": "2026-10-11T03:26:15.437560+00:00",
  "session_id": "9529a6aa8b45e6832efd233652dfa50e",
  "fingerprint": "3930431b46065c5bae84b1b56840559f",
  "occurrences": 1,
  "identity_status": "unverified",
  "rule_version": "guard-descriptive-v1",
  "evidence": {
    "request_id": "d5f59c98d20846f49f606dc9d795dbf2",
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
