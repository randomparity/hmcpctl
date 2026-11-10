# 0351 — Group journaled partition reconfiguration

## Status
Accepted (2026-11-10), internal ownership cleanup.

## Context
Core planning/execution, installer-media legs and network legs implement one reconfiguration
operation. The related files occupy the flat LPAR directory alongside unrelated workflows.

## Decision
Move the three modules into reconfiguration/core.py, media.py and network.py; initializer
has a docstring only. Update all direct consumers and preserve behavior and persisted identities.

## Consequences
Related planning/recovery code is colocated. Private module paths and source fingerprints
change; public tool and persisted operation contracts remain unchanged. No compatibility facade.

## Considered & rejected
- Keep the three prefixed siblings. judgment: repeats a navigation burden already addressed
  by focused provisioning, decommission and profile workflow packages.
- Split or rewrite core execution. judgment: unrelated to the verified ownership defect and
  introduces behavioral risk without helping this bounded relocation.
