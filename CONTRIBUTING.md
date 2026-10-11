# Contributing

Keep contributions tied to a measurable question. A new probe should specify its local oracle, intended capability, failure modes, expected request cost and whether it contributes to identity evidence or only capability evidence.

Run `python3 -m unittest discover -s tests -v` before submitting. Use synthetic loopback fixtures for protocol tests; CI must never need real credentials or spend API credits.

Do not contribute copied benchmark items or fingerprint tables without checking their licenses and documenting attribution. Do not relabel a paper's reported accuracy as this project's measured accuracy.

Useful first contributions: protocol fixtures for Responses/SSE, bounded context-length sweeps, richer code-tracing oracles, controlled downgrade test environments, and evaluation datasets with model snapshots and matching collection conditions. Publish neutral methods and reports rather than unverified provider accusations.
