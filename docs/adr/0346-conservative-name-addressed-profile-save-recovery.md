# 0346 — Keep unresolved name-addressed profile saves uncertain

## Status

Accepted (2026-11-10), operator approval of the conservative profile-save recommendation.
Implemented during the November code-quality campaign.

## Context

An asynchronous save can remain pending after an absent profile read. Its submit response
can be lost, leaving no job handle. ADR0313's absent-means-not-applied rule then allows a
second save. At e83311a6 the actual-engine regression submitted twice across resume.

## Decision

1. A possibly submitted name-addressed save remains uncertain until readback confirms it.
   Absence and mismatched expected contents require attention and do not authorize resave.
2. Matching readback settles the existing effect using its existing identity. Keep the
   following provision full-profile readback gate before default/apply.
3. Missing job handles are not evidence of non-submission. Preserve definite pre-submit
   read refusals and explicit rejected outcomes without a conflicting submitted job
   as not_applied on their first attempt.
4. Resume older not_applied name-addressed rows through read-only reconciliation,
   journaled with the existing effect API. Unresolved observations become uncertain.
5. Preserve effect kinds, targets, SQLite schema, request/public shapes and authorization.
   ADR0314 UUID-addressed before/after CAS remains unchanged.
6. This supersedes ADR0313 Decision3's profile absence row and Decision12's absence/replay
   clause, and governs shared name-addressed force-save recovery referenced by ADR0322.

## Consequences

Some genuinely unsubmitted interrupted saves require inspection and abandonment.
No new durable submission marker can distinguish them from old possibly submitted rows.
Earlier completed effects and recovery-copy diagnostics remain available.
No automatic rollback, duplicate submission or default/apply while unresolved is added.

## Considered & rejected

- Infer refusal from absent readback or job=None. verified: at e83311a6,
  provisioning/operation.py _write_profile classified absence as EffectNotApplied,
  logical/engine.py effect retries that status, and profiles/core.py _profile_job
  can lose a submit response without a handle. The actual-engine regressions exercise
  the duplicate-submission boundary rather than inferring it from the exception type.
- Change UUID CAS or add a submission field/schema. judgment: unnecessary to stop blind
  replay and outside the approved preserved contracts.
