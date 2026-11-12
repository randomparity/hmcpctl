# ADR 0362: Unify creation observation evidence

## Status

Accepted implementation refinement of ADRs 0348 and 0360.

## Context

The private creation snapshot prevents caller mutation, but its sparse map,
observation-name literal and LPAR requested-step tuples repeat field membership.
LPAR copies facts into a public result before apply eligibility can inspect them.
VIOS start evidence follows a separate return-value route.

## Decision

Use one frozen private observation record in the existing coordinator. Its typed
slots carry each step's label, requested selection, completion and WorkflowStep.
One concrete factory selects the slots from existing request models. Record fields
define membership for retention, public projection, verification and interruption.
Start is retained internally; LPAR keeps its combined settings/start presentation,
and VIOS keeps its existing start metadata. Apply is not its own verification gate.

Callers retain orchestration and public result adaptation. LPAR apply eligibility
reads evidence directly, without an intermediate public result synchronization.
Public results are composed at return boundaries. Snapshots expose the frozen
record and a derived independent read-only facts projection, preserving ADR 0360.

## Consequences

Completed None remains distinct from an unobserved step. Payload identities remain
shared, not deeply frozen. Earlier facts and active failure attribution survive a
later TLS rejection. Submission, remote order, warnings, diagnostics, public models
and permission policy remain unchanged. No registry or execution engine is added.

## Considered & rejected

- Keep three inventories: verified: `45d4da37` has the TypedDict and Literal in
  `partition_creation.py` and `_creation_step_requests` in LPAR creation core.
- A generic feature registry: judgment: replaces explicit phase sequencing with
  machinery outside the approved coordinator boundary.
- Reconstruct observations only from the final result: verified: ADR 0348 requires
  completed evidence to survive a later awaited failure before a final result exists.
