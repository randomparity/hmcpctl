# 0207: Execute CI static gates through hooks once per native leg

## Status

> **Superseded by [0309](0309-distributed-test-groups.md)** (2026-10-29), per-version job colocation only.

> **Superseded by [0210](0210-amd64-hosted-ci.md)** (2026-10-09), architecture coverage only.

Accepted

## Context

Issue #1431 in epic #1429 removes within-leg repetition while retaining native
matrix coverage, real hooks and complete standalone verification. The previous CI
orchestration invokes each static gate through verify and hooks; the two documentation
gates also have separate steps. Their validation responsibilities remain distinct.

## Decision

Focused just recipes remain canonical. `verify-runtime` owns the existing test,
MCP smoke, build, artifact validation and root/group CLI checks. `verify` composes
`static` followed by `verify-runtime`, remaining complete for local use.
Each native CI leg runs the real pinned `prek run --all-files` once, then
`just verify-runtime`, then retains its validated wheel. Each static recipe has
exactly one unrestricted system hook. Documentation hooks remain individually named,
and a failure exposes its hook identifier and gate diagnostics while failing the job.

This supersedes ADR 0002's requirement that hosted CI invoke the `just verify`
entry point, while retaining its single recipe graph and security controls.
It supersedes ADR 0097's separately named CI step plus verify/static and hook
execution, and ADR 0098 §6's same repeated wiring. Their generated-document
content, regeneration, non-vacuity and correspondence guarantees remain in force.
No gate moves out of the amd64/arm64 × Python 3.11–3.14 matrix. Tests, exact
coverage, installed-wheel jobs, permissions, pins, schedule and artifact lineage
are unchanged. Local verify does not exercise prek; local hook validation remains
an additional required command.

## Consequences

Successful CI orchestration executes each static gate once, with both documentation
checks separately identifiable inside the hook step instead of separate workflow
steps. Runtime gates execute once, and failure prevents wheel retention and dependent
jobs. Regression tests exercise orchestration with real just/prek and controlled
leaf failures; actual gate failures and exact-head CI provide integration evidence.
Timing reports distinguish elapsed latency, runner consumption and queue/cache variance.

## Considered & rejected

- **Skip already-run gates through environment flags.** judgment: adds a bypass
  contract and state without simplifying ownership.
- **Remove real hook execution from CI.** judgment: loses the requested integration
  proof that configured hooks invoke the same gates.
- **Keep duplicate named documentation steps.** judgment: preserves repeated work
  when distinct hook names and failure output retain diagnostic attribution.
