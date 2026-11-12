# Canonical creation evidence through follow-up

## Problem and authority

Complete `creation_observation_result_roundtrip` across creation core, workflow and
provisioning. The original quality request and approved shared-creation design allow
private carriers using existing models; ADRs 0348, 0360 and 0362 retain the coordinator,
caller orchestration, immediate partial facts and public adaptation at return boundaries.
[ADR 0363](../../adr/0363-retain-creation-evidence-through-internal-followup.md) records
the implementation refinement. This does not supersede those accepted contracts.

## Global Constraints

Python 3.11–3.14 on amd64 CI; local Linux amd64 Python 3.11.15.
No new dependency or toolchain floor, hardware operations, push, PR or shared write.
Use the existing sibling worktree and conventional task commits. Plans stay ignored.

## Canonical handoff

Extract the current public creation body's orchestration into private
`_create_and_stamp_lpar`, returning `tuple[LparCreationResult, _CreationObservations]`.
The first value carries resource, identity, ownership and warnings only; feature-step
fields stay at their existing defaults. No new completion model is required.
The existing public `create_and_stamp_lpar` signature and docstring stay in place;
it calls the private path and projects observations once with `_compose_creation_result`.

Make `_complete_lpar_creation`, `_missing_created_identity`,
`_unconfirmed_created_identity` and `_tls_aborted_creation` return the same pair.
Take the same snapshots at the same phase boundaries. Keep submission apply evidence,
unconfirmed-return clearing, missing-identity diagnostics and TLS interruption distinct.
Do not move diagnostic construction across an await or change any remote phase.

Change `_sanitize_creation_steps` to transform retained observations instead of
public result fields. Preserve its exact selected field subset, predicates, error-only
string replacement and safe message. Keep completion and payload identity for untouched
observations, and retain active/attempted phase attribution independently of rendering.

Introduce private `_creation_followup` consuming creation inputs, metadata and canonical
observations. It reads the existing observation steps directly in the existing public
presentation order and applies the current ownership, resource, apply, settings,
reporting, vTPM, physical-I/O and requested-verification gates. `_creation_steps_verified`
uses ownership plus those observations, without inspecting public result fields.
Raw start and apply retain their existing exclusions from requested verification.

Retain public `creation_followup(creation, created)` as a real public input boundary:
adapt public fields once, then delegate to the canonical logic. Move that adaptation
out of ordinary verification; remove the shared reconstruction-only helper if unused.
Keep the existing public field inventory as projection, not a second internal store.

Migrate both preflight branches in `creation/workflow.py` and `_Run._write_create` in
`provisioning/operation.py` to the private creation and follow-up paths. Keep actual
exceptions, UUID admission, ownership metadata, warnings and bounded journal follow-ups.
Migrate their direct mocks to the internal boundary; retain direct public API regressions.
Do not add a registry, callback pipeline, compatibility re-export or verification cache.
VIOS retains its current snapshot projection and behavior.

Update the maintained creation-evidence spec's composition description to reference
this complete handoff; its earlier inventories and immutable snapshot guarantees remain.

## Success

For the two internal consumers named above, evidence stays canonical through decisions;
there is no public-result projection followed by reconstruction. Direct public creation
returns the same dataclass values, and public follow-up returns the same steps/allowance.
Named public models and signatures, 207 MCP schemas and the six-name facade stay exact.
Existing request counts, refusal order, remote order, partial outputs, warnings and TLS
cutoff remain the behavior contract for the affected creation and provisioning tests.

## Failure model

- Actors/deployments: authenticated CLI/MCP/Python callers; offline fake-HMC tests;
  existing native/REST provider responses and interrupted provisioning runs.
- Invariants/assets: truthful post-effect identity and evidence, ownership/apply gating,
  journal follow-ups, public output parity, active failure phase and fatal TLS cutoff.
- Accepted classes: existing ambiguous submissions and provider failures retain their
  existing outcomes; shallow payload aliasing remains intentional under ADRs 0360/0362.
  No new concurrent HMC transaction or hardware maturity guarantee is introduced.
- Covered elsewhere: existing admission validators, transport identity checks, ownership,
  native submission and provisioning recovery retain their existing responsibilities.

## Threat model

Caller requests and provider observations remain at their existing validation and TLS
boundaries. The metadata/evidence pair and pure evidence transformations do not authorize,
retry, roll back or issue remote calls; orchestration keeps its existing effects.
No credentials, untrusted command construction, public input or permission grant changes.
Do not turn missing, skipped or interrupted evidence into permission for dependent work.

## Validation

The ignored Cast plan inventories canonical completion, sanitization, both internal
consumers, public bridges and interrupted evidence. Use meaningful failing real-path
tests before implementation and controlled faults after it; preserve existing assertions.
Compare baseline public outputs, signatures/classes, feature order and actual tool schemas.
Run affected creation/VIOS/provisioning suites, static checks and pinned hooks before commit.
Final exact-HEAD full verification and bounded whole-delta review remain separate gates.
