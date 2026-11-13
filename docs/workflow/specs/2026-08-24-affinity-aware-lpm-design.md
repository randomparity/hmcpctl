# Affinity-aware LPM preflight design

Issue: #320  
Decision: [ADR 0091](../../adr/0091-compose-affinity-preflight-before-lpm.md)

## Outcome

Expose an opt-in affinity-aware LPM operation that evaluates explicit source and destination
evidence before the canonical HMC validation-first sequence, without changing `JobOutcome` or the
existing migration operation.

## Contract

`LpmAffinityPreflightRequest` names the source/current score, destination estimated score,
destination check basis, configured minimum, platform capability, response (`warn` or `fail`),
and a bounded preflight timeout. `LpmAffinityPreflightOutcome` always reports status, reason, every
input fact, and `proceed`. `LpmAffinityMigrationResult` always contains that outcome and a nullable
job.

The separate MCP and CLI surfaces use this contract. The existing surfaces remain unchanged.

## Data flow and errors

Caller-controlled values are classified before HMC traffic under the requested timeout. Complete
supported evidence at or above the threshold passes. Evidence below the threshold is adverse.
Unsupported capability, malformed or absent evidence, and timeout are unavailable. Warning intent
proceeds for adverse and unavailable outcomes; fail intent stops for both. Only `proceed`
calls the existing `migrate_lpar`, which retains ADR 0018 validation and submission ordering.

Malformed evidence follows the explicit warning or fail-closed response. A malformed response,
capability-limit description, or timeout bound raises actionable `ValueError` before HMC traffic.
Canonical HMC validation failure and timeout preserve ADR 0018's exception and no-submission
behavior.

## Tests

Contract tests prove stable fields and public exports. Operation and tool tests cover passing,
warning, failure, timeout, unsupported capability, malformed input, and exact HMC call counts.
Regression tests prove the legacy operation still returns only `JobOutcome` and keeps its default
validation-first behavior.
