# 0197 — Partition, VIOS and ownership listings name the systems they could not read

## Status

Accepted (2026-10-02), issue #1293.

> **Partially superseded by [0248](0248-unscoped-lpar-listing-reads-quick-all.md)** (2026-10-19): decision 2, for unscoped `list_lpars` only.

## Context

Unscoped `hmc_list_lpars`, `hmc_list_vios` and `hmc_list_lpar_ownership` read the HMC-wide
`LogicalPartition` and `VirtualIOServer` feeds. On 2026-10-02 every such read on an HMC timed out
after 180 s while one of its eight systems was `No Connection`; the same system's scoped feeds
answered within a second. The operator re-scoped the fix: read the `ManagedSystem` feed, skip each
system that is not `operating`, and read the rest through the scoped feeds. The scoped feeds refuse
an empty answer from a non-operating system (#1301).

This replaces the one fleet-wide `LogicalPartition` read that [ADR 0071](0071-bulk-lpar-ownership-read-over-rest.md)
chose for unscoped ownership; the rest of ADR 0071 stands.

Skipping a system makes the result partial. Issue #1293 requires the result to name the system it
could not read. The three tools returned `list[dict]`, so they had nowhere to put that name.

## Decision

1. The three listings return one shape, `FleetListing`
   (`hmcpctl.operations.systems.fleet`). It holds `entries` (the list the tool used to return)
   and `unreadable_systems`, a list of `UnreadableSystem` records. Each record has `system_name`,
   `system_uuid`, `state` and `detailed_state`. The CLI's `--json` output is the same object.
2. A listing without a system reads the managed-system inventory once. It records every system
   whose `State` is not `operating`, or that has no UUID, in `unreadable_systems`, and reads the
   other systems one at a time through their scoped feeds. A system that the inventory's
   null-property fallback names but cannot resolve is also recorded, with no state. A fleet
   larger than `MAX_PARENT_DISCOVERY_SYSTEMS` (100) is refused with
   `supply managed-system scope`.
3. A scoped listing returns the same shape with an empty `unreadable_systems`, so the type does
   not depend on the arguments. A scoped read of a non-operating system still raises (#1302).
4. When the scoped read of a system the feed reported `operating` raises `HMCError`, the system
   is read again. If it is no longer operating, it is recorded as unreadable. Otherwise the error
   fails the call.
5. `limit` caps `entries` only. `unreadable_systems` is never truncated.
6. A VIOS `state` filter is applied locally in every case. The unscoped
   `VirtualIOServer/search/(PartitionState==…)` request is gone with the HMC-wide feeds.

## Consequences

- This is a breaking change to the return value of three MCP tools, two CLI `--json` outputs, and
  the pre-release operations `list_lpars`, `list_vios` and `list_lpar_ownership`. kdive's Tier A
  ownership readback changes with it: it now reads `.entries`.
- An unscoped listing costs one `ManagedSystem` read plus one scoped read per operating system. On
  the HMC above, the `ManagedSystem` feed alone took about 160 s, and the change has not been
  measured live. Whether the HMC-wide feed is ever a safe fast path stays with #1290.
- Amended 2026-10-13 (#1309): a read-only capture on two V11R2 HMCs, each with one system in
  `No Connection`, found the full `ManagedSystem` feed timing out too (at 60, 300 and 600 s), while
  `ManagedSystem/quick/All` answered fast and listed every system with its `State`. The inventory
  in decision 2 now comes from `quick/All`. A system whose `State` is not `operating`, or whose
  `UUID` is not a UUID, is recorded as unreadable; `detailed_state` is `quick/All`'s non-blank
  `StateDetail`; the re-read in decision 4 also uses `quick/All`. The null-property fallback that
  decision 2 mentions no longer applies to these listings, because `quick/All` does not trip it.
- A system in `Standby`, `Power Off` or any other state that is not `operating` is listed as
  unreadable, even when its scoped feed would have returned partitions.
- Amended 2026-10-13 (#1501): `hmc_list_systems`, `hmc_get_system` and the `systems list` /
  `systems show` CLI list systems from `quick/All` too, and read `ManagedSystem/{uuid}` only for
  an operating system. They keep returning plain system entries rather than a `FleetListing`:
  a system that is not operating gets a sparse entry in the same envelope (`UUID`,
  `ResourceType`, `title` and `link` null, and `Resource` holding only `SystemName`, `State`
  and `StateDetail`). The marker entry rejected below for partition listings mixed a skipped
  system into a list of partitions. Here every record is a system, the sparse entry is a true
  subset of the full one, and its own `State` says why the rest is absent, so `limit`, the
  `state` filter and the CLI table need no special case. The operator chose this over the
  envelope on 2026-10-13 to keep these outputs' shape. The `state` filter now matches the
  summary's `State` exactly instead of calling the `State` search, which stalled with the
  feed.
- The scoped `lpar list --system` / `vios list --system` and `list_lpar_ownership(hmc, system)`
  also change shape. Every reader of these results changes with them: the CLI, the live-test
  runner, the capture sweep, the inventory recipe, and kdive's ownership readback.
- The client's unscoped `list_logical_partitions()` and `list_vios()` branches are removed, and
  `system_uuid` becomes required on both.

## Considered & rejected

- **Keep `list[dict]` and log the skipped systems.** verified: package logging goes to stderr
  (ADR 0043), and stderr belongs to the process that spawned the server, not to the agent
  reading the tool result (ADR 0043, Context). A list with sys-R1 missing
  reads as "sys-R1 has no partitions", which is the empty-reads-as-none failure that #1289 and
  #1301 close.
- **Put a marker entry for each skipped system in the list.** judgment: it mixes two record kinds
  in one list, so `limit`, the state filter and every CLI table column would each need a special
  case.
- **Fail the whole listing when any system is not operating.** verified: issue #1293's Expected
  section requires returning the other systems' entries.
- **Use the envelope only on unscoped calls.** judgment: scoped readers such as kdive would keep
  their list, but a return type that depends on which arguments were passed costs every caller a
  branch, and the generated output schema could not describe both shapes.
- **Try the scoped read of every system and keep whatever answers, as the capacity report does.**
  verified: the operator decision of 2026-10-02 in #1293 says to skip non-operating systems
  before reading them.
- **Treat every per-system `HMCError` as unreadable.** judgment: an authentication or transport
  failure would turn into a partial success. #1293 covers non-operating systems, and the
  capacity report (#1301) already re-raises errors from operating systems.
- **Read the systems concurrently.** judgment: the fleet is capped at 100 systems, and the
  serial cost has not been measured. Concurrency would add a second bound and its error handling
  before any evidence shows they are needed.
