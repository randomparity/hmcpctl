# Conservative name-addressed profile-save recovery

Status: implementation authorized by the operator on 2026-11-10.
Baseline: e83311a6; scope is the approved conservative profile-save recovery policy.
Decision record: ADR 0346, conservative-name-addressed-profile-save-recovery.

## Problem and success
A RUNNING save followed by absent readback is recorded not_applied and submitted again
on resume. A lost submit response also yields job=None, so that field proves nothing.
The real-engine probe submitted twice; six handle/readback cases fail before the change.
No second save may run while a previous submission can still complete.

## Owners and transitions
- Provision _write_profile: retain existing positive by-name readback behavior and the
  following full profile_readback gate; absent readback after invoking save raises existing
  HMCError, leaving the effect uncertain. TLS still stops every follow-up.
  Render only bounded exception type, numeric status and HMC message codes for opaque
  helper/readback failures; retain causes without copying provider text into steps.
- logical/profile_write.py: move the existing nested named classifier into concrete
  classify_named_profile(hmc, lpar_uuid, system_uuid, name, expected). Its factory uses
  that owner. Absence now returns needs_attention; matching and mismatch behavior remain.
- profiles/persist.py: invoking the helper makes submission possible even without a job
  handle. Keep the existing bounded settle/readback loop; unconfirmed results raise
  SaveUnconfirmed. Keep EffectNotApplied for the existing pre-save read refusal
  and an explicit execution=rejected outcome with no conflicting submitted job.
  A missing handle with unknown execution remains uncertain.
- Extract the existing force-save classifier into classify_saved_profile(hmc, lpar_uuid,
  name, *, removed_slots=(), require_adapter=True), reused by its factory. A missing
  required adapter/slot or unavailable profile holds; matching readback settles applied.
- Extract the existing safe diagnostic branch in profiles/persist.py into
  profile_failure_summary(error), reused by its existing formatter and new provision
  save/readback/legacy boundaries. Keep trusted pre-call validation messages unchanged.
- Preserve UUID-addressed profile_write before/after CAS classification unchanged.
- Keep existing kind, target, identity, SQLite, request, authorization and public result shapes.

## Legacy rollout
Recorded not_applied profile rows can contain the old uncertainty mistake.
At the provision profile key and reconfigure profile_save key, inspect ctx.recorded.
For a recorded not_applied row, call ctx.effect with a read-only classifier callback
instead of the native-save callback. Existing intent/outcome writes atomically turn an
interrupted observation into an open effect; matching readback returns its existing
identity, and unresolved readback raises an existing uncertain-write exception.
Reuse the concrete classifiers with the already-open admitted HMC client.
No engine API, global retry rule, schema migration, marker or new registry is needed.
An older genuinely unsubmitted write can require inspection/abandonment: the existing
record cannot distinguish it from a possible pending submission.

## Failure model
- Actors/deployments: existing local/CLI/MCP operators and the durable logical engine;
  HMC boundaries are simulated offline. No new hardware maturity is claimed.
- Invariants/assets: no duplicate asynchronous save, no unsafe default/apply follow-up,
  retain prior completed effects, exact journal targets and guarded partition ownership.
- Accepted classes: unresolved/ambiguous name-addressed state stays held for operator
  reconciliation. A genuinely unsubmitted interrupted write may need abandonment.
- Covered elsewhere: store bounds/locking and authorization by existing engine guards;
  UUID CAS by ADR0314; native profile jobs by existing typed job/privacy tests.

## Validation
- focused-test: actual engine over pending save, handle present/absent, absent/matching/
  mismatched resume, repeated resume, and legacy not_applied row; one save maximum.
  tests/lpar/test_profile_save_uncertainty.py exercises the actual engine and classifier.
- focused-test: name classifier absent/mismatch hold; exact matching identity; UUID
  before/after CAS unchanged; force-save missing handle and readback mismatch hold.
- focused-test: pre-submit read refusal invokes no save; late matching readback settles;
  no default/apply after uncertainty or TLS; helper, immediate readback and legacy
  readback provider/session markers are absent from returned and persisted results.
- focused-test: controlled legacy-check bypass must fail the no-resave assertions.
- Integration: related provision/install/reconfigure/profile tests, lint/types/format,
  actual MCP schema comparison, just verify and pinned all-file hooks.
- task-test-not-applicable: prose ADR/spec meaning has no executable consumer; check
  numbering/format and review the decision without adding wording snapshots.

## Decision and rollback
Supersede only the absence/replay rules of ADR0313; retain its historical body and
existing supersession notice. Update current reconfigure persistence specs to the same
name-addressed policy. Reverting code can revive unsafe retries, so reconcile pending
jobs and journals before rollback; never automatically rewrite or delete HMC profiles.
No registry, new durable marker, blind retry or UUID CAS change is warranted.
