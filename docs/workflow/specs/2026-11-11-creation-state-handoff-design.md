# Creation inputs and observation snapshots

## Problem and authority

Complete the residual creation handoff identified after `4c5bc4ba`, rather than
repeating its seven-field cleanup. ADR 0348 and the approved shared-creation design
already allow private carriers using existing model types. Preserve that decision's
shared invocation coordinator, caller sequencing and immediate partial evidence.
See [ADR 0360](../../adr/0360-creation-inputs-and-observation-snapshots.md).

## Global Constraints

Python 3.11–3.14 on amd64 CI; local Linux amd64 Python 3.11.15.
No new dependencies or toolchain floor changes.
No live hardware operations, pushes, PRs, or shared/external writes.
Use the existing sibling worktree and conventional commits; plans remain untracked.

## Input ownership

In `partition_creation.py`, move name, environment, resources, the existing selected
feature fields, and their pure predicates to frozen `_CreationFeatureInputs`.
This replaces the coordinator's input fields; it is not a second mutable request store.
LPAR/VIOS entry boundaries project their existing typed requests once. Raw optional
transport arguments stay with callers to preserve None/false/empty diagnostics.

The coordinator constructor takes HMC, resolved system UUID, original system selector
and inputs. LPAR admission validates local assignments using input predicates, resolves
UUID, then constructs the coordinator before its existing duplicate lookup.
VIOS computes existing flags from inputs, completes resources when currently required,
and uses `replace` on the frozen inputs before construction. Preserve its REST-only path.

The original selector is stable invocation context. A private CLI-name cache remains
runtime context, exposed through a read-only effective-system property. VIOS passes its
resolved CLI name to the existing `discover_mode` phase; that phase records it before
its current capability reads. LPAR creation-memory admission resolves/caches its CLI
name at the current point. No caller assigns coordinator fields after construction.

## Observation ownership

Keep the sparse typed accumulator private. `_CreationObservationSnapshot` contains
an independent read-only copy of its top-level mapping, `active_step`, and
`attempted_feature`. `snapshot()` performs no IO and awaits nothing.
Absent keys remain absent; a completed observation returning None remains present.
WorkflowStep and payload identities remain shared: this is not a deep-freeze promise.

LPAR adapts snapshots at its existing result-assembly boundaries. Its TLS catch takes
one snapshot for both partial facts and the failing phase. VIOS takes a snapshot after
its current observation/start sequence and maps the same facts into its current keys.
Callers never access the live accumulator. Admission records retain their existing
typed interfaces; no parallel admission store or orchestration layer is added.

`observe_unavailable_identity`, `observe_before_followup`, `observe_security_policy`
and `observe_vtpm` use the stored inputs instead of receiving LparCreation again.
The three feature-only observation helpers take `_CreationFeatureInputs`; map its
environment to the same previous partition-type comparison. Remove the reverse
TYPE_CHECKING dependency on the LPAR request model. Keep every control-flow branch.

## Success

1. No caller writes coordinator resources/system UUID/system or reads mutable facts.
   No coordinator input or pure predicate duplicates its immutable input carrier.
2. No observation phase receives LparCreation. Existing public entry signatures,
   request/result dataclasses, tool schemas and the six-name facade remain unchanged.
3. Preserve local refusal order, all remote-call sequences, kind-specific admission,
   identity/observation order, stamping/application policy and diagnostic distinctions.
4. Snapshots retain previous observations across later success/failure, preserve
   absence versus None, expose the active failed phase and reject mapping mutation.
5. TLS exception identity/cutoff, partial result fields, causes and VIOS metadata are
   unchanged. Existing behavior assertions remain; migrate private-owner setup only.

## Failure model

- Actors/deployment: authenticated application callers and offline fake-HMC tests.
- Invariants: pre-effect refusal, truthful partial evidence, selected identity and
  no later requests after TLS rejection. Snapshotting must not invent completed work.
- Accepted classes: existing provider failures/unknown submissions and shallow payload
  aliasing retain their current contracts; no new transaction/concurrency guarantee.
- Covered elsewhere: existing feature validators, ownership guards, native submission,
  recovery and public result adapters keep their responsibilities.

## Threat model

No input or permission boundary is added or widened. Existing request validation still
precedes effects. Input normalization must preserve None/false/empty distinctions;
carrier construction cannot authorize or perform IO. Snapshotting cannot retry, read
an HMC or replace a failed observation with an earlier successful result. Existing
transport/identity guards and their exact ordering remain acceptance criteria.

## Validation

The Cast plan inventories input construction/order, snapshot semantics, observer
ownership and public result/TLS behavior. Baseline the full affected LPAR/VIOS creation
modules before edits. Add meaningful input/snapshot/phase-order regressions, verify they
bite with controlled faults, and run all affected suites plus public API/schema checks.
Compare unchanged function ASTs after explicit ownership normalization where applicable.
Run static and pinned all-file hooks before committing. Final exact-HEAD `just verify`
and bounded whole-branch review remain separate goal-completion gates.
