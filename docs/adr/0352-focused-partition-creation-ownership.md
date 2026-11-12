# 0352 — Colocate partition creation without lifecycle coupling

## Status
Accepted (2026-11-10), internal responsibility cleanup.

## Context
The creation model/orchestrator, complete workflow and physical-I/O/start/serial admissions
form one cluster. Generic core also implements actual deletion and renaming.

## Decision
Move creation owners into creation/{core,workflow,physical_io,start,serial}.py, docstring-only
initializer. Existing core keeps exact delete/rename implementations with genuine dependencies.
All direct consumers import real owners; no compatibility re-exports or wrapper implementations.

## Consequences
Creation navigation is explicit and lifecycle mutations no longer import creation orchestration.
Private module paths/fingerprints change; runtime/public/persisted contracts do not.

## Considered & rejected
- Keep all prefixed/generic siblings. judgment: obscures the existing coherent creation cluster.
- Move all of core into creation. verified: rg declaration inventory at d155f4fa shows
  delete_lpar and rename_lpar alongside creation declarations; judgment: lifecycle mutations
  should not become owned by a creation package.
- Leave serial outside. verified: serial.py declaration/import inventory at d155f4fa contains
  only creation admission/confirmation/saved-observation helpers and the same coordinator
  consumers; judgment: splitting this sibling retains the same ownership defect.
- Rewrite or framework creation. judgment: unrelated to owner migration and adds behavioral risk.
