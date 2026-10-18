# ADR 0236: Explicit logical-operation classifiers

## Status

Accepted (2026-10-18), under the operator-approved code-quality cleanup.
Refines the classifier delivery mechanism of ADR 0195; its journal safety rules
and ADR 0199's classifier behavior remain unchanged.

## Context

LPAR power registers classifiers into a global engine dictionary during import.
Repeated provider import via reload raises a duplicate-registration error, and
engine reconciliation depends on prior imports.

## Decision

The submitter passes its effect-kind classifier mapping alongside its operation
body. Submission copies the mapping and propagates it to its worker context.
The engine owns no provider registry. Power supplies its existing start/stop checks;
restart still has none. Missing checks retain fail-closed needs_attention results.

## Consequences

Classifier selection is explicit and local to each execution attempt, including
resumes. Caller mutation after submission cannot replace a worker's checks.
No database schema, authorization policy or persisted record changes. Internal
registration callers migrate directly; no compatibility registration path remains.

## Considered & rejected

- **Keep registration and tolerate duplicate imports.** judgment: this retains
  hidden process-wide coupling and cannot isolate providers between submissions.
- **Have the engine import the power provider.** judgment: it reverses domain
  ownership and couples a generic durable engine to one operation family.
