# RelayAudit report

Synthetic data: **True** · Run: **complete**

Capability evidence only. Model identity and operator intent are NOT verified. Small synthetic suites can saturate and can be recognized by a relay.

| Endpoint alias | Attempts | Graded | Passes | API errors | Median response ms |
|---|---:|---:|---:|---:|---:|
| reference_demo | 27 | 27 | 27 | 0 | 220.0 |
| relay_demo | 27 | 26 | 13 | 1 | 90.0 |

## Paired comparisons

- relay_demo vs reference_demo: 26 jointly graded tasks; delta -50.00 percentage points; declared conditions match: True; descriptive_only. Model identity: not verified.

## Per-category counts

| Endpoint | Category | Passed / graded |
|---|---|---:|
| reference_demo | code_tracing | 3 / 3 |
| reference_demo | context_retrieval | 9 / 9 |
| reference_demo | instruction_following | 3 / 3 |
| reference_demo | reasoning | 6 / 6 |
| reference_demo | structured_output | 3 / 3 |
| reference_demo | tool_calling | 3 / 3 |
| relay_demo | code_tracing | 1 / 3 |
| relay_demo | context_retrieval | 0 / 9 |
| relay_demo | instruction_following | 3 / 3 |
| relay_demo | reasoning | 5 / 6 |
| relay_demo | structured_output | 2 / 3 |
| relay_demo | tool_calling | 2 / 2 |

Counts and paired deltas are descriptive. No calibrated false-positive rate, p-value, dilution percentage, or authenticity score is claimed.

Latency measures the full non-streaming HTTP response, including network and queueing. It is not TTFT, pure inference speed, or evidence of a cheaper model.

Usage is provider-reported. It does not independently prove token billing accuracy.
