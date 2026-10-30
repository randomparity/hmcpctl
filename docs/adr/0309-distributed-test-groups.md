# 0309 — Distributed test groups with per-version coverage

## Status

Accepted (2026-10-29), operator-approved direction for PR #1823; verification pending.

## Context

The Python 3.12 producer at `1f235b83` completed in 23m34s; its runtime step
took 20m44s. A passing run alone gives insufficient growth margin within the
existing 25-minute test deadline and 30-minute job budget.
The operator approved four test groups, combined coverage per Python version,
and a measured maximum of twelve minutes per 3.12 group job before handoff.

## Decision

Keep test files and node IDs. Extend the existing pytest supervisor with named
groups: `domain-and-unit`, `application`, `procedure-arms`, and `tooling`.
Existing resource admission and subprocess cleanup apply to each invocation.
Grouped runs collect branch coverage but report the final coverage gate pending.
The default full run retains its exact existing coverage gate and summary.

CI runs fourteen real hooks for each supported Python version, then the four
groups on separate runners. Matrix failures do not cancel sibling groups.
The existing per-version `verify` jobs become final aggregators: require successful
dependencies and exactly four same-version coverage artifacts from this workflow
run, validate each, combine them, then enforce the configured 90.5% branch floor.
Only successful aggregation permits MCP/CLI smoke, build, artifact validation and
wheel retention. Existing wheel/library consumers retain their artifact names.

This supersedes ADR 0207's same-job colocation only. Hooks still run once for
each native Python leg; no gate is consolidated across Python versions.
The existing check names remain, with explicit failed-dependency guards rather
than skipped aggregators. No branch-protection administration is claimed.

## Consequences

More runners and setup work trade total runner consumption for shorter test jobs.
Shared fixtures and application dependencies remain installed: these are execution
groups, not newly hermetic library/CLI/MCP environments. Mixed presentation tests
remain together; real end-to-end coverage stays. Local `just verify` remains full.
The twelve-minute acceptance target is measured in confirming CI, not a permanent
runtime guarantee. Coverage is never combined across Python versions.

## Considered & rejected

- **Keep one full producer or increase its deadline.** verified: CI run
  [40968360111](https://github.com/randomparity/hmcpctl/actions/runs/40968360111)
  reports the 3.12 times above, at `1f235b83`; the operator rejected that margin.
- **Timing-only file shards.** judgment: weaker component ownership than the
  approved groups; historical group workloads already support useful headroom.
- **Physically reorganize files and shared fixtures first.** judgment: enlarges
  the behavior and identity surface before execution separation requires it.
- **Apply 90.5% independently to subsets.** judgment: each group covers only part
  of the package; the per-version union owns the existing whole-package gate.
