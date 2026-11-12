# Creation observation evidence

## Problem and authority

Complete `creation_observation_handoff_duplicates_state` after the real input/snapshot
refactor. The original quality request and approved shared-creation design authorize
private carriers using existing models within ADR 0348's concrete coordinator.
[ADR 0362](../../adr/0362-unify-creation-observation-evidence.md) records this refinement.

## Global Constraints

Python 3.11–3.14 on amd64 CI; local Linux amd64 Python 3.11.15.
No new dependencies or toolchain floor changes.
No live hardware operations, pushes, PRs, or shared/external writes.
Use the existing sibling worktree and conventional commits; plans remain untracked.

## Evidence owner

Replace `_CreationObservations` TypedDict with a frozen concrete record whose twelve
typed slots hold a frozen observation value: label, requested, completed, and step.
The record's fields are its membership definition. One `selected(inputs)` factory
sets the existing predicates and labels; remove `_ObservationName` and the separate
LPAR `_creation_step_requests` inventory. Reflection over this one record may project
values, but must not dispatch feature execution or introduce a registry.

Extend private inputs with existing settings, reporting, physical-I/O and apply
selections. Extract the existing LPAR normalization into one pure helper used at
creation admission and public follow-up verification. This introduces no validation
or remote call. VIOS defaults remain distinct and raw transport arguments stay put.

Coordinator retention replaces its frozen record after each completed observation,
including None. `retain(name, step)` records already obtained local/submission facts
without IO or changing active/attempted phase; `observe` preserves its awaited failure
tracking. A snapshot retains the immutable record and phase. Its facts property is
a fresh read-only projection of completed public slots, excluding internal start.
Step and nested payload identities remain shared. No mutable backing map remains.

## Consumers and sequencing

Seed submission apply evidence before post-create identity confirmation, without
changing phase tracking. LPAR apply eligibility reads coordinator evidence plus the
existing ownership-stamped fact directly. Remove the intermediate public-result merge.
Carry retained evidence through completion, identity returns, TLS returns and both
internal follow-up consumers. Compose public results only at the public return
boundary. Keep diagnostic sanitization in its current owner, operating on canonical
evidence with its distinct field subset. The completed handoff is specified in
[canonical creation follow-up](2026-11-11-canonical-creation-followup-design.md).

Missing/unconfirmed identity diagnostics become retained evidence only after their
existing awaited observations. A normal unconfirmed-identity return still discards
the submission apply outcome, while TLS during that observation retains it as before.
Do not move diagnostic construction across an await.

Record raw start internally for both kinds. LPAR start failures retain settings-phase
attribution and its legacy/start combination remains the public settings step. VIOS
reads retained start for metadata and keeps its existing fail-fast behavior. Apply
and raw start are excluded from ordinary requested-step verification; apply retains
its special skipped-on-interruption behavior. Remaining labels and request predicates
keep their existing order and meaning.

The same record factory adapts the public `creation_followup` input boundary; internal
consumers retain canonical observations directly for verification and follow-up.
Interruption marks the active public step, requested missing steps and
the existing fallback settings error, preserving previously completed facts and labels.
No later IO, replay or rollback follows fatal TLS rejection.

## Success

One typed evidence owner replaces the map/name/request inventories. No intermediate
public result is required for apply verification. Both creation consumers retain
their existing outputs, remote/refusal order and diagnostic distinctions. The named
public request/result classes, entry signatures, six-name facade and 207 tool schemas
remain unchanged. Existing behavioral assertions remain intact.

## Failure model

- Actors/deployments: authenticated application callers and offline fake-HMC tests.
- Invariants: pre-effect refusal, ordered remote effects, truthful partial evidence,
  correct failing phase and fatal TLS cutoff. No invented completed observation.
- Accepted classes: existing ambiguous submissions/provider failures retain current
  contracts; shallow WorkflowStep/payload aliasing remains intentional, not transactional.
- Covered elsewhere: existing feature validators, native submission, ownership,
  recovery and result presentation retain their responsibilities.

## Threat model

No boundary is added or widened. Caller-controlled requests retain their validators
and ownership policy; HMC/SSH observations retain transport and TLS identity controls.
The record factory and projections cannot authorize, await, retry or contact an HMC.
Snapshot fields must not overwrite an earlier fact on an interrupted await. Existing
operators remain the trusted application actors; hostile concurrent HMC/filesystem
transactions are outside this representation-only change and retain existing owners.

## Validation

The one-unit Cast plan inventories slot membership/selection, immutable snapshots,
start attribution, interruption, apply gating and public-result projection. Use the
recorded 41-module baseline: 2,783 plus 103 passing cases, 11 existing VIOS skips.
New focused cases must fail before implementation and under controlled faults.
Run the same affected modules, exact public AST/schema comparison and pinned hooks.
Final exact-HEAD full verification and bounded whole-delta review remain separate gates.
