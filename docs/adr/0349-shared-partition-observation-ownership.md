# 0349 — Share contained partition observations below resource mutations

## Status

Accepted (2026-11-10), internal ownership correction during the requested quality cleanup.

## Context

Native processor changes read memory as well as processor context to prove coupled resets.
Their shared partition observations reside in memory/current.py alongside memory mutation
policy. Primitive nested resource decoding resides with composite inventory collectors.

## Decision

Move dictionary-only resource_container to operations/resource_reading.py. Move the existing
contained partition resolver and common full snapshot/readers to lpar/observation.py.
Specialist memory and processor operations import these owners directly and keep their own
admission, targets, authorization, mutations and postcondition decisions. Remove old definitions
and compatibility aliases. Preserve all fields, diagnostics and remote ordering.

## Consequences

Processor operations no longer depend on private memory mutation infrastructure, and primitive
resource reads no longer initialize composite collectors. Shared snapshots retain both memory
and processor context because coupled reset checks require them. Pre-release internal imports
and commit-bound source witnesses move with the actual owners; public schemas remain unchanged.

## Considered & rejected

- Duplicate the readers in processor operations. judgment: duplicates containment and numeric
  context policy used by both resource domains.
- Keep the current owners. judgment: makes basic reads depend on unrelated mutation/collector
  responsibilities and hides their shared contract.
