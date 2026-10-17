# ADR 0227: Scoped volume-group selectors

## Status
Accepted (2026-10-16), implementing #1697.

## Context
Inventory exposes VG names, but storage commands require UUIDs and disagree on
positional arguments. A name has meaning only within its VIOS.

## Decision
Resolve exact VG names and UUID membership in the storage operation layer using
the selected VIOS inventory. Reject missing, duplicate or incomplete matching
identity; never choose the first VG. Keep low-level client UUID/path validation.
VIOS-centric CLI commands take positional VIOS, `--vg` and object `--name`;
mapping targets use `--lpar`. Attach-disk keeps positional LPAR, `--vios` and
`--vg`, resolving before the existing UUID-based provisioning workflow. Its VG
preflight uses the same membership predicate, retaining UUID-only typed inputs.

## Consequences
Old positional VG/media syntax is replaced with migration examples. Selection
adds an inventory read; existing guarded writes and their If-Match remain.
Repository inventory has no LPAR filter; list-mappings supplies relationships.

## Considered & rejected
- CLI-only VG lookup: judgment: shared operation consumers would lack the same
  containment guarantee.
- Client name APIs: judgment: would mix operator selection with transport/path
  validation, contrary to #1697's explicit boundary.
