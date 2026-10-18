# ADR 0241: Live scenario validation and evidence owners

## Status
Accepted (2026-10-18), operator-approved internal ownership correction.

## Context
The execution runner also implements AST scenario source validation and Git/catalog
provenance and publication. Those policies can change independently of execution.

## Decision
Move source validation to live_test/scenario_validation.py and evidence admission,
provenance, publication and the shared atomic report writer to live_test/evidence.py.
Keep RunState, the executable scenario registry and lifecycle in runner; pass its
scenario-module collection explicitly to validation. Migrate direct callers and tests,
removing obsolete runner definitions without compatibility aliases.

## Consequences
Internal consumers use the responsible module. Saved records, publication eligibility,
validation rules, startup order and runtime authorization remain unchanged.

## Considered & rejected
- **Retain mixed ownership.** judgment: source policy and evidence publication remain
  coupled to navigating and importing the execution owner.
- **Add a registry or service layer.** judgment: ordinary module calls and one explicit
  existing registry collection express the boundary without new machinery.
