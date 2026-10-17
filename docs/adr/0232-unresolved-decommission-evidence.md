# ADR 0232: Preserve unresolved decommission evidence

## Status
Accepted

## Context
Issue #1701 requires actionable mapping evidence while preserving the existing
LPAR decommission JSON envelope and conservative detach authorization.

## Decision
Retain existing blast-radius inventories and counts; add observational mapping
records with explicit unknowns and per-mapping disposition. Mutation eligibility
continues to come from the existing validated detach inventory. Render CLI details
as scalar records through existing table/TSV/JSON format owners. Inspection commands
omit an LPAR filter when it could hide unresolved evidence. Backing storage remains.

## Consequences
Consumers retain established keys and receive additive evidence. A failed detach
request is a failed attempt with uncertain resulting state, not proof of deletion.
Human operators can inspect known VIOS scope before deciding the next action.

## Considered & rejected
- Replace storage_mappings with unresolved entries: judgment: changes the established
  meaning of a legacy JSON field unnecessarily.
- Infer ownership or detachability from partial identity: judgment: uncertainty cannot
  authorize destructive action and contradicts #1701.
- Render nested objects inside table cells: verified: CLI source at b3fd1414 uses
  str(step.result), placing entire nested dictionaries into width-limited cells.
