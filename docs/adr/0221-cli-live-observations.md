# ADR 0221: Record bounded CLI live observations separately

## Status

Accepted (2026-10-14), implementing the operator's 2026-10-13 choice in #1477.

## Context

The v0.1.0 live claims cover MCP operations, not their CLI wrappers. The bare-CEC
recipe uses the CLI. Issue #1477 permits actual CLI steps or a broader fingerprint;
the operator selected actual bounded steps and truthful coverage gaps.

## Decision

The bare-CEC arm runs the installed CLI state, reference-code and job-show commands
against its existing fixture and activation job. record_verified writes asserted
CLI observations on their raw result rows, separate from the MCP catalog sidecar.
The existing candidate provenance scopes them to that run; they do not acquire an
MCP closure freshness claim. The release-window report states CLI coverage or a gap
for each claimed operation. The remaining wrappers are explicitly unverified.
Historical v0.1.0 claims and their CLI exclusion remain unchanged.

## Consequences

CLI failures fail the arm without bypassing its cleanup. Offline tests prove runner
behavior, never live verification. Future release claims must cite both surfaces'
evidence at the candidate or state the remaining CLI gap. The existing promotion
format still carries MCP evidence only; the window summary carries CLI scope.

## Considered & rejected

- **Fingerprint wrappers without running them.** judgment: does not implement the
  operator-selected actual CLI-path evidence.
- **Duplicate the whole lifecycle through CLI.** judgment: increases hardware
  mutation and recovery scope when bounded reads plus explicit gaps meet #1477.
- **Overwrite the MCP catalog observation with CLI evidence.** verified:
  scripts/promote_live_observations.py stores one observation per operation, so
  replacement would erase the independently claimed MCP scenario.
- **Do nothing beyond the historical disclaimer.** judgment: leaves the selected
  actual CLI mechanism unimplemented.
