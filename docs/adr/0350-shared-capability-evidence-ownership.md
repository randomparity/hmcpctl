# 0350 — Share capability evidence below command entry points

## Status

Accepted (2026-11-10), internal responsibility separation in the requested quality cleanup.

## Context

The inventory command supplies schemas, registry discovery, closure hashes and freshness rules
to four independent producer/promotion/window consumers. Those rules share one versioned
evidence contract, while catalog/corpus validation and CLI orchestration are command concerns.

## Decision

Relocate existing code into a docstring-only capability_evidence package with schema, closure,
registry and verification owners. Command and consumers import those real modules directly.
Retain command invocation, catalog validation/reporting and every data/behavior contract.
Keep application/runner discovery lazy, source hashing exact and caches per invocation.
No old-command compatibility facade or package re-export layer is introduced.

## Consequences

Shared evidence no longer depends on a CLI entry point. Each owner follows an existing cohesive
source section and has independent import/behavior proof. Internal lookup/test/clone seams move;
versioned artifacts, runtime eligibility, freshness and hardware authority do not change.

## Considered & rejected

- Keep the command as library. judgment: hides the common data contract behind command
  responsibilities and private command helper imports.
- One large shared module. verified: the existing four groups contain 868 declaration lines
  before constants/imports, measured from check_capability_inventory.py at e231474a using
  Python AST spans; judgment: the existing four cohesive sections provide clearer small owners.
- A new generic evidence framework or dependency. judgment: the current concrete functions
  already supply the required behavior and need only a lower owner.
