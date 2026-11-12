# ADR 0361: Colocate profile synchronization and restore

## Status

Accepted.

## Context

Profile synchronization and collective restore remain in LPAR configuration,
while the profile package already owns synchronization observations and backup
artifact operations. The quality review identified this remaining split.

## Decision

Move synchronization to `operations/lpar/profiles/sync.py` and collective restore
to `operations/lpar/profiles/artifacts.py`. Move existing functions unchanged,
update direct consumers and maintained ownership citations, and remove old definitions.

## Consequences

The profile adapter uses one domain package. Authorization, SSH transport,
public contracts and failure behavior remain unchanged. Tests patch the actual
operation owner. ADR 0092's inventory includes the destination modules and
classifies their existing backup deletion as a non-LPAR mutation.

## Considered & rejected

- Retain the split: judgment: related profile operations belong with their existing owners.
- New forwarding modules: judgment: extra indirection has no protected compatibility contract.
- New unified profile service: judgment: unnecessary abstraction for two direct moves.
