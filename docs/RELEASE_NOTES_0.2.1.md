# RelayAudit 0.2.1 — protocol fixes and reproducible release

This patch packages the fixes merged after v0.2.0:

- Filter hop-by-hop header names from every repeated Connection field on requests and responses.
- Keep the actual upstream timeout or client-disconnect outcome when the observer fails or its parser queue is full.
- Include the current allowlisted synthetic-demo packaging path and a stable, version-controlled report; archive summaries use a package-relative report path instead of a local filesystem path.

The source version, runtime versions and wheel metadata are 0.2.1. The old v0.2.0 tag and assets remain unchanged. Source archives are tied to the new tag; SHA256SUMS and RELEASE_PROVENANCE.json in the Release identify the exact commit, source archive and wheel.

## Validation

The pre-release baseline at `4d6c15874ecaa9d404a17ebe4112b48bb2599902` passed 125 traditional tests and the existing loopback acceptance harness (30 requests per workload/mode, six modes, 240 upstream requests including warmups). The release changes version metadata, documentation and synthetic archive path sanitization; protocol and detector implementations are unchanged.

The candidate wheel is installed into a new virtual environment outside the source checkout. The copied regression suite and acceptance harness run there without the `relay_audit` source directory, ensuring imports resolve to the installed wheel. This includes repeated Connection header and observation/transport-failure regressions, the CLI, a synthetic demo and legacy demo/plan entries. Release assets include the resulting metadata-only evidence and checksum manifest. GitHub CI covers Python 3.10, 3.12 and 3.13; use the linked runs and provenance for actual results.

All mock cases are `synthetic_unverified`. Real paid providers, model identity calibration and new real-client integration checks are NOT_RUN for this patch; no new semantic or identity accuracy claim follows. Existing historical client checks remain historical evidence.
