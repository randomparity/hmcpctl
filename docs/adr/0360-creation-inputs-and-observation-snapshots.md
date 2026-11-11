# ADR 0360: Creation inputs and observation snapshots

## Status

Accepted implementation completion of ADR 0348's approved ownership boundary.

## Context

Creation callers currently write resolved identities/resources into the coordinator,
pass the LPAR request back into observations, and consume its live facts dictionary.
The seven-field cleanup did not remove this mutable handoff.

## Decision

Replace coordinator input fields and predicates with one frozen private
`_CreationFeatureInputs` carrier using the existing typed request models.
Construct the invocation coordinator after UUID resolution and required VIOS resource
completion. Keep late CLI identity as explicit context of its existing discovery phase.
The coordinator retains private partial observations and exposes a frozen
`_CreationObservationSnapshot`: an independent read-only top-level facts mapping plus
active and attempted phase. Callers adapt snapshots into their existing public results.
Feature observers consume the stored inputs, without importing `LparCreation`.

## Consequences

ADR 0348's coordinator, sequencing, partial evidence and caller-owned transport remain.
Native transport retains the original optional arguments where None/empty distinctions
affect existing diagnostics. Neither carrier deep-copies or freezes payload objects.
No new workflow layer, public model, request field, retry or permission follows.

## Considered & rejected

- Another field-substitution-only pass. verified: `4c5bc4ba` removes seven reads;
  source still exposes caller writes and live observation state afterward.
- Resolve every CLI name before construction. verified: VIOS creation at `4c5bc4ba`
  performs duplicate/capacity/host admission before CLI resolution; preserve that order.
- Reconstruct facts only in callers. verified: ADR 0348 requires completed facts to
  survive a later failed read. Keep the coordinator's accumulation and snapshot it.
- A registry or generic pipeline. judgment: adds orchestration unrelated to the handoff.
