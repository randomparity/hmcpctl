# C12 partition creation physical and tagged I/O

## Problem and authority

#1773 binds eight creation attributes at P10 `mksyscfg:41` and P11
`mksyscfg:49`. Existing PCIe operations modify a saved profile after creation;
provisioning adds adapters afterwards. Neither configures creation-time tagged I/O.
The operator approved bounded modes and retained the three positive virtual-slot
forms without creation-time linkage for #638 dependency reconciliation. Existing
adapter owners remain unchanged. [ADR 0274](../../adr/0274-lpar-physical-io-creation.md)
records the transport and outcome decision.

## Scope and architecture

Extend `LparCreation` with optional typed `physical_io: LparPhysicalIO` and
`creation_sriov: tuple[SriovLogicalPortAssignment, ...]`. The latter reuses the
existing assignment model; it is not a new adapter family. The create workflow
moves existing `assignments.sriov` into this input only when C12 fields are
requested, then removes those entries from the later assignment phase. Existing
calls without C12 fields retain their current behavior. Dedicated post-create
assignments remain unchanged; reject overlap with creation slots before creation.
No stable facade export, dependency, toolchain, profile CRUD, or raw proxy changes.

`LparPhysicalIO` has exactly the eight named attributes. `io_slots` reuses
`ProfileIoSlot` DRC/pool/required values; `lpar_io_pool_ids` is a tuple of decimal
pool identifiers. Scalars are optional and omission preserves the HMC default.
`virtual_opti_pool_id` accepts integer 0/1, not booleans. Physical DRCs are eight
uppercase hexadecimal digits. The three multi-kind tagged fields accept physical
or declared direct-SR-IOV DRCs; positive decimal virtual slots are rejected before
effects with the #638 linkage explanation. Only `console_slot` also accepts `hmc`.
`alt_console_slot` and `op_console_slot` require physical DRCs. No console default
is inserted. IBM i fields require `partition_type=OS400` and fresh `IBMiCapable`.
Lists reject duplicates, malformed entries, and excessive request counts (256).

C12 selects documented `mksyscfg -r lpar`, so the eight attributes are present in
the initial profile. Explicit keylock is incompatible with this route and is
refused before effects. Complete explicit resources are required; C12 must not
implicitly use `all_resources=1`. Existing settings and partition ID are retained.
The specialized encoder accepts only validated typed C12/direct-SR-IOV values,
uses IBM quoted attribute-list grammar, and shell-quotes the complete record.
It does not widen the generic attribute builder's admitted grammar. Nested SR-IOV
properties have fixed keys and validated DRC/decimal/capacity values.

Before create, resolve the unique managed-system identity and scoped name absence.
For physical slots, reuse the default slot inventory's `drc_index`, `lpar_id`,
`slot_io_pool_id`, `lpar_assignment_capable`, and physical presence fields; require
one matching eligible, unassigned slot. Read existing profile slot rows and reject
slots already listed there. Pool IDs must be observed on that system's slot
inventory; require requested slot pool IDs to match the fresh pool identity and
be present in requested partition pool membership. Missing/ambiguous observations
fail closed. Tagged physical references must belong to requested creation slots;
load/restart/console physical slots must be marked required. Alternative/operations
console references also require declared physical membership. No foreign partition
or profile is modified to satisfy a request.

Reuse existing direct-SR-IOV prevalidation (adapter/port health, capacity,
granularity, containment, and admitted environment), additionally requiring a
unique unconfigured requested logical-port DRC and default_profile association.
Direct-SR-IOV tagged references must match those creation declarations. Existing
post-create direct-SR-IOV assignments are not executed again. Resource ownership
is stamped on the new partition through the existing path. Subsequent assignments
continue to cross their existing authorization checks.

## Outcomes and verification

Reuse `operations.lpar.profiles.read_partition_profile` as the contained native
REST read oracle; do not widen ADR 0165's CLI projection. Its existing inventory,
parent/origin and exact default_profile checks supply the profile `fields` map.
Read before the C12 profile apply effect. Only a successful comparison permits
requested application; stamp/readback failure or mismatch skips application and
later steps. Ordinary creation retains its present ordering.

The retained schema set behind the complete-profile reader supplies exact paths:
`IOConfigurationInstance/PoolIDs` is repeated xsd:int; `ProfileIOSlots/ProfileIOSlot`
has `AssociatedIOSlot/SlotDynamicReconfigurationConnectorIndex`, `IsRequired` and
optional `PoolID`. TaggedIO has `LoadSource`, `AlternateLoadSource`, `Console`,
`AlternateConsole`, `OperationsConsole`. `VirtualOpticonnectPool` is boolean.
`ProfileSRIOVEthernetLogicalPorts/ProfileSRIOVEthernetLogicalPort` inherits
LogicalPortID, AdapterID, PhysicalPortID and ConfiguredCapacityPercentage.
Fresh profile resource/identity and bounded shapes remain mandatory.

Read DRCs as unsigned decimal integers and compare against `int(input_DRC,16)`:
Primitives.xsd DynamicReconfigurationConnectorIndex.Type is unsignedInt >=65536,
whereas VirtualIOSlotIndex.Type is unsignedShort. Unions.xsd defines the three
physical/virtual unions; Enumerations.xsd HMC.Enum is the literal `HMC`, mapping
the documented CLI `hmc` input. Profile slot PoolID and repeated partition PoolIDs
compare integer values; do not equate an absent PoolID with an arbitrary zero or
sentinel. Boolean reads accept only native true/false or 1/0 lexical forms.
Absent requested scalar/slot relationship is unverified, never default-equated.
Schema AttrGroup annotations establish read structure, not mutation permission;
creation grammar remains the cited mksyscfg source. No CLI output grammar is inferred.

Provenance: campaign-retained LogicalPartitionProfileIOConfiguration.xsd SHA256
4578bf6f6a33a328ea428c3cd2d20982e4233efed7f6556215ba8b28d664dfdc,
IBMiProfileTaggedIO.xsd 40b45c8e6eab0da323b2bf32b7454cd4a02bffd24d387089e8ce28d799f68b19,
ProfileIOSlot.xsd ee2d0f4154001cc0ef04770102959bf83adee89e5953705273a3659cd187ccbe,
and the hash-verified retained Primitives/Unions/Enumerations schema bodies.
These declarations are source observations, not positive mutation/live evidence.
Report a `physical_io` workflow step with profile verification separately from
application/current activation. Submission alone cannot make the step successful.

A mismatched or unavailable readback leaves the partition and resources in place,
reports an error step, and stops later assignments/provisioning. A transport error
after dispatch reports an unknown create outcome with inspect-before-retry advice;
no automatic retry, deletion, reassignment or rollback. Existing create and stamp
partial results remain explicit. Successful profile comparison proves configured
relationships, not guest boot, activation readiness or device health. No new
mutation of an existing LPAR is introduced; ADR 0092's creation classification and
existing assignment authorization remain applicable.

## Success

- Eight fields and sixteen source bindings have typed, documented creation inputs;
  operator-approved unresolved virtual-slot forms fail before effects.
- Malformed, duplicate, foreign, occupied, ambiguous or unresolvable requested
  physical/SR-IOV relationships fail before create.
- Requested profile relationships are read back; mismatch/unavailable/unknown
  outcomes stop dependent workflow steps and do not claim completed configuration.
- CLI/MCP inputs share the same operation; generated help/tool/maturity artifacts
  remain synchronized. The #638 virtual-slot gap is disclosed without promotion.

## Failure model

1. Actors and deployments: authenticated local library/CLI operators and MCP
   callers connected to a trusted HMC; CI uses injected transport boundaries.
2. Invariants and assets: existing partition ownership; physical slot and pool
   containment; exact typed record meaning; no foreign resource capture; truthful
   partial-create reporting; no repeated assignment.
3. Accepted failure classes: concurrent out-of-band changes cannot be locked by
   native creation; fresh admission plus post-read detects contention but is not
   a transaction. SSH has finite existing timeouts; shape checks occur after
   collection and do not impose a streaming byte cap. Guest boot and untested
   hardware modes remain unevidenced. Virtual-slot positive forms are explicitly
   excluded by operator approval and tracked at #638.
4. Covered elsewhere: profile CRUD #1724; SR-IOV properties #668; retained client
   network reconciliation #638; existing assignment ownership ADR 0092; transport
   authentication/configuration and secret handling remain existing owners.

## Threat model

- Added boundary: typed C12 caller input into native creation attributes. Strict
  types, counts, DRC/decimal grammar, set uniqueness, fixed keys and two-layer
  quoting prevent attribute/shell injection.
- Widened boundary: trusted system/profile/SR-IOV inventory influences allocation.
  Exact identity, unique rows, membership and unused-state checks fail closed;
  unparseable responses never become empty/free state.
- Actors: MCP inputs and local caller values are untrusted; the authenticated HMC
  is trusted to report resources truthfully. Existing access-policy dispatch still
  governs managed-system mutation; the change adds no permission bypass.
- Out of scope: malicious authenticated HMC, simultaneous external writers beyond
  observed contention, and positive virtual-slot adapter linkage retained by #638.
  Errors use existing redaction and terminal rendering; no private evidence is
  committed or published.

## Validation and live procedure

Focused tests cover source grammar for eight fields, encoding of multiple lists
and nested properties, malformed/foreign/in-use inventories, capability rejection,
slot/pool/tag relationships, readback mismatches, unknown dispatch, no repeated
SR-IOV assignment, CLI/MCP schema/input parity, and post-effect short-circuiting.
Controlled faults must demonstrate that new admission, encoding and cleanup tests
fail when their defended behavior is removed. Guardrails are `just verify` and
`uv run --no-sync prek run --all-files`; Python 3.11–3.14 CI remains authoritative.

Live evidence is optional and bounded separately by the root orchestrator. Initial
minimal named arm49 (`lpar-physical-io`, `st49-lpar-physical-io`): one newly named inactive OS400 fixture with explicit small resources,
`console_slot=hmc`, no physical/SR-IOV assignment, no activation or profile update.
Use existing preflight, exact isolated deployed-HEAD proof, independent before
inventory, `scripts/live_test_runner.py --group lpar-physical-io` native create/readback/delete arm, recovery on every
outcome, and independent after comparison. Only exact owned fixture cleanup is
allowed. Physical/SR-IOV positive arms require a separately approved eligible free
resource packet. No live pass is claimed for omitted or failed arms. Publish only
generated allowlisted evidence, and keep all remaining modes unevidenced.
