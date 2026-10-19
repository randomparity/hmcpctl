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
types (`PartitionID` is an int) where Atom has strings, and no nested configuration blocks.
ADR 0197 dropped the HMC-wide Atom feeds because they stalled 180 s while one system was
`No Connection` (#1293). No bulk read has been captured in that state, and no firmware other
than V10R3 has been probed (#1733).

## Decision
1. **Gate.** After the existing `ManagedSystem/quick/All` read and 100-system refusal,
   unscoped `list_lpars` reads `LogicalPartition/quick/All` only when every system summary has
   a UUID and a `State` of `operating` or `power off` (trimmed, case-insensitive): the states
   the #244 probe saw answer. Otherwise it reads as ADR 0197 decides.
2. **Fallback.** Any `HMCError` from the bulk read, or an item that cannot be placed (below),
   logs one warning naming the cause and reads as ADR 0197 decides; that path raises its own
   errors, so a failure never becomes a partial success.
3. **Entry shape.** Each item becomes an entry in the existing envelope: `UUID` (the item's),
   `ResourceType: "LogicalPartition"`, `title` and `link` null, `Resource` the item verbatim,
   and `ManagedSystem` `{UUID, SystemName}`. The system UUID is the segment after the last
   `/ManagedSystem/` of `AssociatedManagedSystem` and must be a UUID; an item is unplaceable
   when it is not an object, its `UUID` is not a UUID, or that segment is absent. `UUID` and
   `SystemName` come from the matching summary (case-insensitive), else from the href and the
   item's own `SystemName`.
4. **Membership.** On the bulk path the entries are the bulk answer, `power off` systems'
   partitions included, and `unreadable_systems` is empty. Completeness rests on the #244
   V10R3 probe (77 of 77) until #1733.
5. **Scope.** Only unscoped `list_lpars`. Scoped `list_lpars`, `list_vios` and
   `list_lpar_ownership` keep ADR 0197.
6. **ADR 0012.** ADR 0012 fixes each tool's result kind and output schema; both are unchanged
   (`FleetListing`, `Resource` an untyped object). The keys inside `Resource` were never part
   of it: `hmc_list_systems` already mixes full and sparse entries (ADR 0197, #1501). A null
   `title` marks a bulk summary entry, and the tool docstring and `lpar list --help` say so.
7. **CHANGELOG.** A breaking entry under **Changed** for unscoped `hmc_list_lpars` and
   `lpar list --json`.

## Consequences
- A fleet of `operating` and `power off` systems costs two requests instead of one per system
  plus one. A fleet with any `No Connection` system, as recorded in #1293 and #1309, keeps
  today's cost.
- Which `Resource` an entry carries depends on the fleet and on whether the bulk read
  succeeded, not on the arguments; value types differ (`PartitionID` int against string).
  A caller wanting the full document scopes the call.
- A `power off` system's partitions are listed or reported unreadable depending on whether
  every other system passes the gate.
- A failed bulk read costs that failure plus the ADR 0197 read; a timeout costs one
  `HMC_TIMEOUT` (180 s by default) first.
- The gate and fallback bound transport failures and unplaceable items only. A placeable bulk
  answer from unprobed firmware is trusted, including a different `PartitionState` spelling,
  an omitted partition or a lagging summary, until #1733's captures.

## Considered & rejected
- **Do nothing until #1733's captures.** judgment: the gate and fallback keep today's
  behaviour on the one recorded stall state and on any bulk failure; the operator excluded
  capture work from this change.
- **Always read the bulk listing, no gate.** verified: ADR 0197 records HMC-wide Atom reads
  stalling 180 s with one `No Connection` system (#1293); #244's probe comment lists
  `No Connection` as not tested for the bulk read.
- **Per-system child `ManagedSystem/{uuid}/LogicalPartition/quick/All`, as the read or the
  fallback.** verified: #244 measured 0.14–0.37 s per child read, so about 16 requests on that
  HMC against two. judgment: the child form is unprobed off V10R3 too, so as a fallback it
  would need its own fallback to the Atom feed.
- **A new summary entry type, or a listing-level field naming the source.** judgment: either
  changes the generated output schema of `hmc_list_lpars`; the null `title` already marks
  each summary entry.
- **Rename `RMCState` and coerce values to the Atom spelling.** judgment: ~50 Atom keys stay
  absent, so callers gain no guarantee and the item stops being what the HMC said.
- **Exclude `power off` systems' partitions.** verified: #244 found the bulk and Atom UUID
  sets identical (77 of 77), so those entries are present and correct.
- **Migrate the scoped listing too.** judgment: a scoped read is already one request, and
  ADR 0197 decision 3's refusal of a non-operating system would need a new design.
- **Raise on an unplaceable item.** judgment: a firmware whose items differ would lose a
  listing that works today.
