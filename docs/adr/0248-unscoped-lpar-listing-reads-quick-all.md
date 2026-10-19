# ADR 0248: The unscoped LPAR listing reads `LogicalPartition/quick/All` when the fleet allows it

## Status
Proposed (2026-10-19), issue #1731. Partially supersedes decision 2 of
[ADR 0197](0197-unscoped-listings-name-unreadable-systems.md) for unscoped `list_lpars` only.

## Context
Unscoped `list_lpars` (`hmc_list_lpars`, `lpar list` without `--system`) reads
`ManagedSystem/quick/All`, then one scoped Atom feed per operating system (ADR 0197). The #244
probe on one V10R3 HMC (16 systems: 15 `operating`, 1 `power off`) measured 16 requests and
about 20 s for that, against one `LogicalPartition/quick/All` request in about 0.5 s
(ADR 0245). The bulk answer listed the same 77 UUIDs as the Atom feed, including the 2
partitions of the `power off` system that ADR 0197 reports unreadable.

The bulk item is a flat 29-key summary, not the Atom entry: no `title`, `link` or
`ManagedSystem` object, `RMCState` where Atom says `ResourceMonitoringControlState`, JSON
types (`PartitionID` is an int) where Atom has strings, and none of the nested configuration
blocks. ADR 0197 dropped the HMC-wide Atom feeds because they stalled 180 s while one system
was `No Connection` (#1293). No bulk read has been captured in that state, and no firmware
other than V10R3 has been probed (#1733).

## Decision
1. **Gate.** After the existing `ManagedSystem/quick/All` read and the
   `MAX_PARENT_DISCOVERY_SYSTEMS` refusal, unscoped `list_lpars` reads
   `LogicalPartition/quick/All` only when every system summary has a UUID and a `State` of
   `operating` or `power off` (compared trimmed and case-insensitively): the states the #244
   probe saw answer. Otherwise it reads as ADR 0197 decides, unchanged.
2. **Fallback.** Any `HMCError` from the bulk read, or any item the projection below cannot
   read, discards the bulk answer and reads as ADR 0197 decides. That path raises its own
   errors, so a failure is never turned into a partial success.
3. **Entry shape.** Each item becomes an entry in the existing envelope:
   `UUID` (the item's), `ResourceType: "LogicalPartition"`, `title` and `link` null,
   `Resource` the item verbatim, and `ManagedSystem` `{UUID, SystemName}`. The system UUID is
   the segment after the last `/ManagedSystem/` in `AssociatedManagedSystem` and must be a
   UUID. `UUID` and `SystemName` come from the matching system summary (case-insensitive), or
   from the href and the item's own `SystemName` when no summary matches.
4. **Membership.** On the bulk path every partition is in `entries`, those of `power off`
   systems included, and `unreadable_systems` is empty: it still names exactly the systems
   whose partitions are absent.
5. **Scope.** Only unscoped `list_lpars`. Scoped `list_lpars`, `list_vios` and
   `list_lpar_ownership` keep ADR 0197 unchanged.
6. **Contract.** `FleetListing` and the tools' output schemas are unchanged. The content change
   is recorded in `CHANGELOG.md` under **Changed** as a breaking change to unscoped
   `hmc_list_lpars` and `lpar list --json` entries.

## Consequences
- On a fleet of `operating` and `power off` systems the listing costs two requests, not one
  per system plus one.
- Which `Resource` an entry carries depends on the fleet, not the arguments: bulk summaries
  when the gate holds and the bulk read succeeds, Atom entries otherwise. The keys present in
  both include `PartitionName`, `PartitionID`, `PartitionState`, `PartitionType`,
  `OperatingSystemType`, `OperatingSystemVersion` and `SystemName`; value types still differ
  (`PartitionID` int against string). A caller wanting the full document scopes the call.
- A `power off` system's partitions are listed or reported unreadable depending on whether
  every other system passes the gate.
- A bulk read that fails costs that failure plus the ADR 0197 read; a timeout costs one
  configured timeout (`HMC_TIMEOUT`, 180 s by default) first. The gate excludes the one state
  known to stall HMC-wide reads.
- The `state` filter, `limit`, the CLI table (it reads `RMCState` as a fallback already) and
  the live capture sweep's `PartitionState` lookup work on both shapes.
- #1733's captures can widen the gate's state set or remove the fallback.

## Considered & rejected
- **Do nothing until #1733 has `No Connection` and other-firmware captures.** judgment: the
  gate and fallback bound both unknowns to today's behaviour, and the operator excluded
  capture work from this change.
- **Always read `LogicalPartition/quick/All`, no gate.** verified: ADR 0197 records HMC-wide
  Atom reads stalling 180 s with one `No Connection` system (#1293), and #244's probe notes
  record `No Connection` as not tested for the bulk read.
- **Per-system child `ManagedSystem/{uuid}/LogicalPartition/quick/All` through `read_fleet`.**
  verified: #244's probe measured 0.14–0.37 s per child read, so about 16 requests and several
  seconds on that HMC against 0.5 s; the child form is equally unprobed off V10R3 and in
  `No Connection`, so it removes no hazard the gate does not.
- **A new summary entry type for both paths.** judgment: it changes the generated output
  schema of `hmc_list_lpars` and every reader for a listing ADR 0012 wants stable; the
  sparse-entry precedent of ADR 0197 (#1501) keeps the envelope instead.
- **Rename `RMCState` and coerce values to the Atom spelling.** judgment: a partial mapping
  still leaves ~50 Atom keys absent, so callers gain no guarantee and the item stops being
  what the HMC said.
- **Exclude `power off` systems' partitions and keep naming them.** verified: the #244 probe
  found the bulk and Atom UUID sets identical (77 of 77), so those entries are present and
  correct; reporting them absent contradicts `FleetListing`'s "entries absent" meaning.
- **Migrate the scoped listing too.** judgment: a scoped read is already one request, and
  ADR 0197 decision 3's refusal of a non-operating system would need a new design.
- **Raise on an unreadable bulk item.** judgment: a firmware whose items differ would lose a
  listing that works today; the fallback returns a correct answer instead.
