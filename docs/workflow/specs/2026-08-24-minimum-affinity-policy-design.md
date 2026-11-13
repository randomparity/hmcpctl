# Power11 minimum-affinity policy read design

## Goal and authority

Issue #315 requires a read-only, capability-aware view of Power11 LPAR
`min_affinity_score` and `min_affinity_score_action`, including portable snapshot capture. The
campaign acceptance requires REST-versus-CLI evidence, closed validation, actionable unsupported
results, malformed-response tests, and no setter.
[ADR 0086](../../adr/0086-power11-minimum-affinity-policy-read.md) selects the evidence-backed
CLI path.

## Contract

`MinimumAffinityPolicyResult` is a frozen presentation-neutral envelope with capability,
resolved system and LPAR names, nullable score/action values, and an unavailable reason. An
available result always has score `0..100`, action `none|warn|fail`, and no reason. A
capability-unavailable result has no policy values and has a nonblank actionable reason.

The async operation resolves SSH names, reads the managed system's
`lpar_proc_compat_modes`, and only when it contains `POWER11` runs an explicitly projected
`lssyscfg -r lpar` query. Capability absence is data. Transport, permission, unexpected HMC, and
malformed-output errors remain errors. The projection must produce exactly one header-labelled row
with both nonempty fields; booleans, decimal spellings, out-of-range integers, unknown actions,
extra rows, and missing columns fail closed.

## Public surfaces

Expose one async Python operation, one MCP read tool, and one CLI read command following existing
LPAR configuration patterns. The tool targets the LPAR and accepts system, LPAR, and optional
profile selectors. Human output explains capability absence; JSON output preserves the envelope.
There is no mutation operation.

## Snapshot integration

Portable capture invokes the shared policy operation once. Add a version-1
`minimum-affinity-policy` capability. Supported results store the policy in a distinct observation
envelope with a versioned media type. Extend `SnapshotCapability` with optional
`unavailable_reason`: the minimum-affinity capability requires a nonblank reason when unsupported
and forbids one when supported. Unsupported results omit the observation and retain that reason, so
capture remains usable. Existing version-1 snapshots without the optional field, capability, and
observation remain valid.

## Error and compatibility behavior

Capability probing parses the explicit compatibility field and never converts unrelated failures.
The policy parser wraps structural/value failures as `HMCCLIError` naming malformed `lssyscfg`
policy output without echoing raw rows. Snapshot serialization and parsing stay backward compatible
because the new capability and observation are optional. No REST parity or native-Power11 live proof
is claimed: the checked-in REST references do not expose these LPAR fields, and the live evidence
uses 000B firmware with advertised POWER11 compatibility on Power9 hardware.

## Testing

Focused tests prove a valid available policy, all three actions and score bounds, capability absence
without the policy command, propagation of probe failures, malformed headers/cardinality/values,
name resolution, API/MCP/CLI delegation, registry security metadata, snapshot supported capture,
snapshot unsupported capture, blank/mismatched capability reasons, and old-snapshot compatibility.
The live runner exercises only the read operation and reports capability absence normally.

## Durable execution context

- Branch: `feat/min-affinity-policy-315`
- Base branch: `main`
- Guardrails: `just test`, `just smoke`, `just verify`
- Architecture: host `x86_64`; no target architectures declared
- ADR index coupling: no index
