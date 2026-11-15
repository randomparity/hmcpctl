# ADR 0383: Test groups start alongside the static hooks

## Status

Accepted (2026-11-15), operator-approved direction for #1997; hosted timing
confirmation pending.

## Context

ADR 0309 runs the real hooks for each supported Python version and then the test
groups: `test-groups` declared `needs: static`, so every group waited for the
slowest static leg. Over the 20 most recent green CI runs before this change
(through 2026-11-14) the static phase took 180 s at the median and 191 s at most,
out of a median 848 s from run creation to completion.

The per-version `verify` aggregator already runs under `if: always()` and fails
unless both `needs.static.result` and `needs.test-groups.result` are `success`.
`tests/test_ci_pipeline.py` executes that guard against failed, skipped, cancelled
and empty results. The edge between `static` and `test-groups` therefore ordered
the jobs without gating anything the aggregator does not gate.

## Decision

`test-groups` no longer depends on `static`. Both start when the run starts. The
`verify` aggregator keeps `needs: [static, test-groups]`, `if: always()` and its
explicit result guard, and remains the only job that joins them.

This supersedes ADR 0309's ordering of hooks before groups only. The group
definitions, per-version coverage combination, artifact contract, check names and
hook-per-Python-leg placement are unchanged.

## Consequences

A run's elapsed time loses the static phase from its critical path. A static
failure no longer prevents that run's test jobs from starting, so a run that
fails a hook spends the test minutes it previously skipped. Peak concurrent jobs
for one run rise from the number of test jobs to that number plus the static legs;
overlapping runs reach the account's concurrent-job limit sooner.

A red static leg and a red test group can now appear in the same run, where the
earlier ordering showed only the static failure.

## Considered & rejected

- **Keep the edge and shorten the static jobs instead.** judgment: worth doing and
  tracked separately (#1996), but any remaining static time still delays every
  test group for no gating benefit.
- **Cancel test groups when a static leg fails.** judgment: needs either
  `fail-fast` across unrelated jobs, which Actions does not offer, or a
  cancelling step with `actions: write`; the workflow keeps `contents: read`.
- **One shared static job for all Python versions.** ADR 0209 resolved that
  relocation as NO-GO pending an enforceable required-check contract; this change
  does not revisit it.
