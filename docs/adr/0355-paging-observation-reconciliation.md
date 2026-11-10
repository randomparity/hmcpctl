# 0355 — Separate paging evidence from reconciliation

## Status
Accepted (2026-11-10), internal observation clarity refactor.

## Context
One collector reads configured/UOM/native planes and compares them within a long shared scope.
Its19-field result is positional, while unavailable and invalid evidence must remain independent.

## Decision
Keep reads/local extraction in observe_paging. Construct the existingrecord with keywords and
reconcile five cross-plane checks in one pure helper, also passing existing UOMdevice names.
Keep recordshape/policy/callorder unchanged; use replace toretain every unrelatedfield.

## Consequences
Collection and evidenceagreement have separate readable responsibilities. No new hardware
support, fallback or source-custody surface is introduced; existingmodulepath remains.

## Considered & rejected
- Keep interleavedchecks. judgment: obscures the independent evidence planes and error channels.
- Add a new collector model/class. judgment: existing observation plus one unpersisted fact
  supplies the needed data without a new schema or framework.
- Split every plane into a new abstraction. judgment: beyond this cohesive comparison boundary.
