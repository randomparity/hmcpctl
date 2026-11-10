# Proposed shared creation-feature coordination

Status: implementation authorized by the operator on 2026-11-10 ("approve all three").
The approval covers this coordinator, conservative profile recovery and the separately owned
live-test arm map. This document governs the coordinator implementation only.
Trigger: operator approved recommendation 5, "Design first", during the desloppify campaign.

## Problem and evidence

`operations/lpar/core.py` and `operations/vios/core.py` coordinate the same native
creation capabilities through independently maintained admission and observation sequences.
The feature implementations are already shared. The remaining duplication concerns ordering,
partial evidence, and the relationship between selected features and follow-up eligibility.

Source baseline: commit 55e5ea72. Relevant current owners are LPAR/VIOS `core.py`,
LPAR `memory/creation.py`, `memory/hardware.py`, `serial.py`, `start.py`,
`security_policy.py`, `vtpm.py`, and `affinity_group.py`.

## Preserved differences

| Boundary | LPAR | VIOS |
|---|---|---|
| Simple creation | REST or native, with the existing narrow REST-refusal fallback | Existing REST VIOS path |
| Admission ordering | Controller/role and assignment preflight precede host-feature admission; serial, start, then creation memory | Host-feature admission precedes controller discovery; serial, creation memory, then start |
| Created identity | Serial, memory, then start confirmation before ownership stamping | Start confirmation, then memory/serial confirmation interleaved with their observations |
| Saved observations | Serial, memory, layout, affinity, hardware before local I/O/settings; vTPM/security retain their later positions | Memory, serial, layout, vTPM, security, hardware, affinity, start |
| Incomplete observation | Preserve partial `LparCreationResult` and existing stamping/apply policy | Stop with the existing VIOS failure contract |
| After successful apply | Re-observe current creation memory when currently required | No added apply or current-memory read |

LPAR `stamp_policy`, ownership stamping, profile application, I/O assignments,
role settings and REST fallback remain caller-owned. VIOS public return metadata,
false versus omitted values, and implicit-resource restrictions remain unchanged.
No additional reads, reordering, retries, rollback, or broader reconciliation are allowed.
The operator separately chose to preserve both legacy provider diagnostics and selected-feature
sanitization. This design must not replace those contracts with uniform sanitization.

## Proposed implementation boundary

Add one concrete internal owner, `operations/partition_creation.py`.
Use a private `CreationFeatureCoordinator` for one creation invocation, rather than
introducing a configurable workflow engine, feature registry, hooks, or public facade.
It owns only the shared native-feature lifecycle and records evidence before the next read.

The coordinator holds kind/environment, existing typed feature requests, controller mode,
existing serial/memory/start admission records, confirmed identity, per-feature `WorkflowStep`s,
and the currently attempted feature. It does not persist state or create an HMC client.
Normalize the existing LPAR object and VIOS keyword arguments at their entry boundaries;
preserve `None`, explicit false, and empty request distinctions. Do not reuse LPAR ownership
or apply defaults as VIOS defaults. Any new carrier is private and uses existing model types.

Expose concrete phase methods, not a string-dispatched stage API:

- `discover_mode`: existing capability reads; callers invoke it at their current boundary.
- `admit_host_features`: the common vTPM, security-policy and hardware-memory admissions.
- `admit_partition_features`: serial/memory/start admission in the explicit kind-specific order.
- `confirm_identity`: existing confirmation sequence and failure semantics for that kind.
- `observe_before_followup`: the existing feature observation sequence up to caller-owned work.
- `observe_after_followup`: the LPAR vTPM/security observations at their existing boundary.
- `observe_after_apply`: existing LPAR current-memory verification after a successful apply.

Kind-specific branches remain explicit where the table requires different semantics.
Do not force VIOS through LPAR-only phases or introduce dummy observations. VIOS's interleaved
confirmation/observation sequence remains one concrete branch, rather than being split into
an artificial uniform identity phase. Keep its current exception-translation groupings.

Each successful or incomplete observation is stored immediately. LPAR result assembly maps
those facts to the existing result fields even when a later read raises; it never reconstructs
an earlier successful result after a TLS failure. VIOS retains its current fail-fast messages,
causes and metadata. TLS identity errors stop later reads and writes and retain already obtained
facts. Existing transport submission exceptions and refusal reconciliation remain unchanged.

Pure result adaptation stays in the existing LPAR/VIOS owners. Move feature-only observation
helpers out of LPAR `core.py` when the coordinator needs them; use direct imports without wrappers.
Check runtime import cycles before choosing each helper's final owner. Native command construction,
public request/result models, SQLite state, authorization and source-only live scenarios do not change.

## Failure model and acceptance

The named deployment is the existing REST/SSH HMC application; offline refactoring proves no
new hardware maturity. Preserve pre-effect refusal, ambiguous submission, unavailable identity,
known mismatch, missing fields, ownership-stamp failure, apply failure, and fatal TLS behavior.
An exception during a later feature must retain the earlier feature facts and the active failing
feature; no subsequent request may run after TLS identity rejection. Provider details remain
visible only in the paths that currently permit them.

Before moving coordination, add behavioral trace cases for both kinds and mixed serial/memory/start,
hardware/affinity and policy requests. Record operation order, arguments, result fields, exceptions,
causes and redaction. Include omitted/false, controller/profile-less, REST-only and narrow fallback
arms. Prove new tests fail under a controlled ordering/partial-evidence fault, then restore the code.

Run existing creation-memory, serial, start, hardware, affinity, policy, vTPM, provisioning,
VIOS and presentation suites. Keep the privacy and partial-result assertions intact. Finish with
`just verify` and `uv run --no-sync prek run --all-files`; do not invoke live hardware testing.

## Alternatives and decision

A configurable feature pipeline adds policy machinery without removing the kind-specific contracts.
A single new helper that normalizes all failures would violate the approved diagnostic decision.
Keeping the current shared feature helpers and independent coordinators remains viable accepted debt
if the proposed internal state object does not make ownership and evidence retention simpler.

The operator approved implementation of this concrete owner and preserved phase boundaries
after reviewing the design and its tradeoff. ADR 0348 records the implementation decision.
