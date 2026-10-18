# ADR 0240: Selected recovery checker inputs

## Status
Accepted (2026-10-18), operator-approved internal orchestration correction.

## Context
Recovery repeats seven arm arguments through main, _run_checks and check_run,
then assembles checker/input associations only at the last stage.

## Decision
Construct the selected ordered tuple of existing checker/input pairs once in main.
Forward it through _run_checks to check_run; keep users an explicit keyword flag
because its existing unreadable aggregation differs from ordinary arms.
Preserve decoding, read-only calls, connection composition and CLI verdicts.

## Consequences
Direct internal callers migrate to the pair collection. Heterogeneous checker
signatures retain their existing callable typing boundary; no new dispatch model.

## Considered & rejected
- **Retain parallel arguments.** judgment: repeats association state across layers.
- **Introduce checker classes or registry.** judgment: adds machinery to a fixed
  seven-arm selection already expressible by ordinary tuples.
