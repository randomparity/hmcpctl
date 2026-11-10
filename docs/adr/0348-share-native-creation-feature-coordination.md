# ADR 0348: Share native creation feature coordination

Status: accepted
Date: 2026-11-10

## Context

LPAR and VIOS creation use the same typed feature requests and native feature
operations, with different ordering, partial-result and diagnostic contracts.
Maintaining admission and observation coordination in both core owners makes
those contracts harder to preserve when a feature changes.

The operator approved the reviewed shared creation coordination design in
`docs/workflow/specs/2026-11-10-shared-creation-coordination-design.md` with
"approve all three". This decision implements that bounded internal extraction.

## Decision

`operations/partition_creation.py` owns one private invocation-local
`_CreationFeatureCoordinator`. It discovers controller mode, admits host and
partition features, confirms identity and records observations before follow-up,
after follow-up and after successful profile application. Existing typed feature
admissions and `WorkflowStep` values carry evidence. Each observation is retained
before another read starts; the active operation remains available on failure.

Kind-specific branches preserve the existing sequences. LPAR serial, start and
memory admission retain their ordering, with serial, memory and start identity
confirmation before ownership stamping. Its serial, memory, layout, affinity and
hardware observations precede local I/O and settings. VIOS retains host admission
before controller discovery and serial, memory and start admission; start identity
confirmation precedes interleaved memory and serial confirmation/observation.

LPAR stamping, profile application, local I/O and settings remain in their owners.
Native submission, REST creation and its narrow refusal fallback remain there as
well. VIOS adapts coordinator facts to its existing public result keys. There is
no configurable pipeline, registry, callback orchestration or compatibility wrapper.
Public creation/result models remain in LPAR core; the layout helper imports its
existing result model only when called, after module initialization, avoiding a
runtime import cycle.

## Consequences

TLS identity rejection stops subsequent reads and writes. LPAR retains earlier
feature facts in its partial result; VIOS preserves its fail-fast exception
groupings, causes and messages. Legacy provider diagnostics and selected-feature
sanitization stay distinct. No public facade export, request field, result shape,
new dependency, persistence change or hardware maturity claim follows from this
refactor. Offline creation tests protect ordering and partial facts; live-test
source attestations must include the coordinator wherever the moved code executes.
