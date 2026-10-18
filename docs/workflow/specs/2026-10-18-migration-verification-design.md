# Existing migration and remote-restart verification

## Problem and scope

Issue #631 verifies `lpar.migrate_validate`, `lpar.migrate`,
`lpar.migrate_affinity`, `lpar.migrate_abort`, `lpar.migrate_recover` and
`lpar.remote_restart`. It adds evidence and explicit native obligations to the
existing operation/client/job boundaries, without new parameters or APIs.
The frozen authority is #631's `q631-73031f04` WORK:SCOPE annotation.

## Design

Retain ADRs 0017/0018 job outcomes and validation-first ordering, ADR 0078's
explicit remote-restart controls, ADR 0091 preflight composition and ADR 0092
ownership. No owner or caller migration is needed. Bind the finite reference
rows already recorded in operations.json for these six operations; the procedure
states both supported behavior and missing native proof. Existing REST paths
are documented by both reference corpora, including the unscoped partition
RemoteRestart endpoint despite its managedsystem-jobs reference directory.

Add focused negative/postcondition assertions in the existing LPM test modules.
Exercise UUID destination resolution, exact source partition/job attribution,
cancellation and terminal partial failure, and destination-resolution failure
before submission. Reuse real ownership and target-authorization suites separately;
tests that patch ownership do not prove that guard.

Record bounded implemented variants in maturity.json with empty evidence arrays.
ADR 0127 removed not-run catalog placeholders; ADRs 0186 and 0212 govern current
observation attribution and release projection. No simulated result becomes a
live observation. Regenerate the packaged projection and tool docs.

## Success

The six reference bindings remain unchanged except necessary test-path links.
Focused tests pass with meaningful negative postconditions. Six bounded maturity
entries report implementation independently of their unevidenced live status.
A runbook names prerequisites, source/destination recovery obligations and the
absence of an approved pair and applicable named runner arm. It directs an
operator to the repository preflight, named-arm and recovery protocol before
native execution; adding an arm is outside this verification slice.

## Failure model

- Actors and deployments: local operator and offline CI exercising existing MCP,
  operation and client paths; authorized native operators following live-testing.md.
- Invariants and assets: source/destination identity, ownership, validation order,
  normalized job attribution, native partition availability and truthful evidence.
- Accepted failure classes: missing native pair or arm leaves explicit unrun proof;
  this change grants no hardware authorization and makes no native success claim.
- Covered elsewhere: new migration parameters/API (#638/#698), affinity policy writes
  (#626), runner architecture/transport inventory (#623/#625); oversized defects
  require reviewed #620 decomposition. Existing HMC errors remain caller-visible.

## Validation

Run focused LPM, reconfiguration ownership and target-authorization suites. Prove
new assertions bite with a temporary controlled fault, restore it, then run green.
Run capability-inventory after catalog/projection generation, tool-docs-check after
regeneration, then just verify and pinned prek over all files before pushing.
Human procedure wording has no executable prose test; review it against the
existing live runbook and explicitly preserve its unrun obligations.
