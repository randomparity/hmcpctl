# ADR 0385: Synchronize real worker interrupt probes with supervisor ownership

## Status

Accepted within the operator-approved scope of issue #1909.

## Context

The probe's shared marker cannot establish two ready workers. Its 5-second diagnostic
and 0.3-second cleanup overrides also replace the production contracts in
[ADR 0130](0130-interrupt-diagnostic-window-and-reap.md) and
[ADR 0208](0208-bounded-pytest-evaluation.md). The Linux reproduction and isolated
bound comparisons are recorded in the [design](../workflow/specs/2026-11-16-parallel-interrupt-readiness-design.md).

## Decision

Real workers publish distinct PID identities and readiness after installing their
interrupt handlers. The probe waits on those exact markers, then delivers one interrupt.
Real-worker probes use the existing production diagnostic/reap bounds; fast synthetic
controllers retain their short overrides. Inventory remains immediately after the
supervisor returns and before fixture containment. Production constants stay unchanged.

## Consequences

Readiness failure remains bounded and identifies the missing marker or exited child.
A slow real cleanup uses the actual supervisor contract. Fixture collection still has
its existing ceiling; extreme scheduling pressure can fail that ceiling honestly.

## Considered & rejected

- **Keep one marker.** verified: `tests/scripts/test_run_tests.py` at `425fb3cf`
  writes the same `ready` path in both test files; the first writer releases the probe.
- **Keep shortened real-worker bounds.** verified: the Linux contention reproduction
  at `425fb3cf` returned 1 with a survivor; production bounds retained the diagnostic and reaped.
- **Change supervisor policy.** judgment: no demonstrated defect in its existing contract;
  the narrower repair is to exercise that contract faithfully.
