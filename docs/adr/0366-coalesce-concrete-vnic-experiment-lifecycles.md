# ADR 0366: Coalesce concrete vNIC experiment lifecycles

## Status

Accepted as an implementation refactor of ADR 0312's existing three modes.

## Context

The existing arm repeatedly infers creation, update-source or retained-fixture
mode during validation, budget enforcement, observations, stage execution and CLI
formatting. The modes already have distinct packet/effect/recovery contracts.

## Decision

Keep the existing module and entry points. Give creation and update-source
concrete stage functions beside the existing fixture runner. Keep each mode's
validation, budget policy and helpers together. Add a source budget specialization
following the existing fixture specialization; common accounting does not infer mode.
The CLI selects validation, execution and display once; Python stage dispatch selects
its concrete runner. Shared snapshots receive source state expectations explicitly.

Preserve packet/journal schemas, private evidence timing, effect limits/order,
qualified cleanup, public labels/exits and existing request sequences. This adds
no mode, effect authority, module boundary or qualification claim.

## Consequences

Mode-specific lifecycle code is visible together. Small identical stage blocks
remain explicit rather than being routed through callbacks or a strategy registry.
Direct private source-budget tests select the concrete source policy.
Concrete fixture validation also rejects wrong-version nested preparation packets
that previously bypassed the public union discriminator.

## Considered & rejected

- Cache a mode flag in the shared driver: judgment: retains interleaved policy.
- Introduce a strategy framework or another diagnostic driver: judgment: adds
  machinery while ADR 0312 already chooses this concrete owner.
- Change packet/history shapes to simplify execution: judgment: would change the
  settled evidence and cleanup contracts instead of improving their implementation.
