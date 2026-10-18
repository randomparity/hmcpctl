# Logical inventory: `hmc_inventory`

**Issue:** #1220 (H4 of epic #1215)
**Decision:** [ADR 0196](../../adr/0196-logical-inventory-authorizes-each-source.md), which
narrows [ADR 0189](../../adr/0189-logical-lpar-catalog-and-delegated-authorization.md)
Decision 2 for this tool
**Contract:** the `hmc_inventory` row of
[the logical LPAR workflows spec](2026-10-01-logical-lpar-workflows-design.md#primary-tools)
**Branch:** `feat/logical-inventory-1220` from `main`
**Guardrails:** `just verify`; `uv run --no-sync prek run --all-files`

## Problem

To find candidate systems and their partitions, an agent calls `hmc_list_systems`,
`hmc_capacity_report`, `hmc_list_lpars` and `hmc_list_lpar_ownership` and joins the results
itself. Names repeat across systems and connections. Nothing tells the agent whether a missing
figure is zero, unknown or withheld.

## Scope

One primary read tool, `hmc_inventory`, for one connection per call. It composes existing reads
and adds no HMC client call. Excluded, with their owners: planning (#1221), the default
listing switch (#1232), fan-out across connections in one call, a CLI mirror, VIOS and storage
inventory, measured utilization (#1242), and any change to the `hmcpctl.api` facade (ADR 0118).

### Inputs

| Input | Rule |
| --- | --- |
| `systems` | optional list of 1–16 non-empty `system_name_or_uuid` selectors; duplicates collapse |
| `lpar_state` | optional `PartitionState` (`operations/partition_state.py`) |
| `owner` | optional agent id, 1–64 characters, matched exactly against the ADR 0011 stamp |
| `limit` | 1–200, default 50: the most partitions on one page |
| `cursor` | `next_cursor` from an earlier page, unchanged |
| `profile` | the connection, as for every tool |

A violated bound is a tool error that names the input, raised before the HMC session opens. An
invalid cursor is `invalid_cursor: pass next_cursor from a previous page unchanged`. A blank
`owner` or `cursor` reads as absent (ADR 0094).

### Result

`InventoryPage`:

- `connection`: the policy label for `profile` (`store.connection_label`).
- `systems_source`: how systems were found. On the enumeration path it is a `SourceStatus`
  for `hmc_list_systems`. With `systems` given, it is `null`.
- `systems`: at most 16 entries, plus `systems_limit` (16) and `systems_truncated`.
- `partitions`: at most `limit` entries, plus `limit`, `truncated` and `next_cursor`.

`SourceStatus` has three fields:

- `status`: `ok`, `unavailable` or `denied`;
- `tool`: the delegated tool;
- `detail`: the denial or HMC error text, at most 500 characters, or `null`.

Each `InventorySystem` carries:

- `id`, `selector` (the caller's selector, or `null` on enumeration), `uuid`, `name`, `state`;
- `total_memory_mib`, `free_memory_mib`, `total_proc_units`, `free_proc_units`;
- `sources`, with `capacity`, `partitions` and `ownership`, each a `SourceStatus`.

Each `InventoryPartition` carries:

- `id`, `system_id`, `uuid`, `name`, `partition_id`, `partition_type`;
- `state`, `rmc_state`;
- `current_memory_mib`, `current_proc_units`, `has_dedicated_processors`;
- `owned`, `owner`.

**Scoped identifiers** are `<connection>/<system uuid>` and
`<connection>/<system uuid>/<partition uuid>`. HMC UUIDs are stable, so an id survives a
rename. Two partitions with the same name, on different systems or connections, get different
ids. #1221 consumes these ids. An id is stable per connection label. `<default>` names whatever
an omitted `profile` resolves to at call time (ADR 0038), so it can differ from the same
profile named explicitly, and under `HMC_HOST` every profile is `<default>`.

**Unknown is not zero.** A figure the HMC omits, or reports unparseably, is `null` and never
`0`. A capacity figure `system_capacity` refuses is `null`, and `sources.capacity` is
`unavailable` with the refusal as `detail`. When `sources.ownership` is not `ok`, `owned` and
`owner` are `null`. Ownership has no read of its own: when `sources.partitions` is not `ok`,
`sources.ownership` takes the same status, with `detail` naming `hmc_list_lpars`.

**Empty is not inaccessible.** If `sources.partitions` is `ok` and the system has no matching
partitions, it is empty. If it is `denied` or `unavailable`, the system contributes no
partitions and the status says why.

### Authorization (ADR 0196)

`hmc_inventory` registers as `read`, `target_kind="console"`, not exhaustive. That is the
`hmc_operation_status` shape, so only an `all-targets` grant admits the tool itself. Target
scope is enforced by the four tools it delegates to. Each delegated check calls the server's
own `dispatch_authorizer` as that tool, with `profile` and, where the tool declares one, its
`system_name_or_uuid`. Each authorizer check writes the ADR 0040 record under the delegated
name. A tool the policy ceiling withholds is `denied` without an authorizer call, and so writes
no record, since no grant names it. A
`TargetScopeError` or `ConnectionScopeError` is `denied`, with its message as `detail`.

| Source | Delegated tool | Checked with |
| --- | --- | --- |
| systems, without `systems` | `hmc_list_systems` | no selector |
| partitions, without `systems` | `hmc_list_lpars` | the system's UUID |
| partitions, with `systems` | `hmc_list_lpars` | the caller's selector, before it is resolved |
| ownership | `hmc_list_lpar_ownership` | the same value as partitions |
| capacity | `hmc_capacity_report` | no selector, once per call |

If `hmc_list_systems` is denied, nothing is read. The page has `systems_source` set to `denied`,
and its `detail` tells the caller to pass `systems` selectors. A denied selector becomes an
`InventorySystem` with `selector` set, `id`, `uuid` and `name` all `null`, and `partitions`
`denied`. Nothing is read for it. A selector that is admitted but matches no system has
`partitions` set to `unavailable`. Denied selectors appear on the first page only. Selectors are
resolved again on every page, so one that fails to resolve on a later page is reported there.
An admitted selector also returns that system's `uuid`, `name` and `state` under the
`hmc_list_lpars` decision. This is accepted: the selector already names the system.

### Reads and paging

Read paths:

- **Enumeration:** one `list_uom("ManagedSystem")` read per page. `list_managed_systems` is not
  used: on firmware that cannot serialize a null hardware property, its fallback reads every
  system by name and skips the ones that fail, so a partial feed would read as `ok`. That
  firmware failure makes `systems_source` `unavailable`, with the next action to pass system
  names as `systems` selectors, since the same firmware can refuse a direct UUID read.
- **Selectors:** for each admitted selector, one `get_uom("ManagedSystem", uuid)` (UUID) or
  `find_system_by_name` (name) read, without `get_managed_system`'s multi-read fallback.
  Results collapse by UUID. An `HMCError`, or the `ValueError` an ambiguous name raises, makes that selector
  `unavailable` with the error text.
- **Partitions:** one `list_logical_partitions(uuid)` read per admitted system that the page
  reads. When that feed is empty, the client also reads the system's state (#1301): one
  `get_managed_system` read, or three on the firmware fallback. A system that is not operating
  raises `HMCError` instead of reading as empty. An `HMCError` makes that source `unavailable`.

Systems are visited in UUID order, and partitions within a system in UUID order. The cursor is
the unpadded urlsafe base64 of the JSON `[system_uuid, partition_uuid | null]`, at most 256
characters, and both values must be UUIDs. A page skips systems ordered before the cursor's
system and, within that system, partitions at or before the cursor's partition. It then reads
until one of these happens:

- `limit` partitions are collected, and more remain in this system or later ones: `truncated`;
- 16 systems are read, and more remain: `systems_truncated` and `truncated`;
- the systems run out: `next_cursor` is `null`;
- a read raises `HMCTransportError` (a timeout or connection failure): that system is
  `unavailable`, nothing further is read, and `next_cursor` points at the next system. Selector
  resolution stops the same way: the unresolved selectors are `unavailable` with that detail,
  and so are the resolved selectors' partitions, which are not read on that page. Every
  system on that page is then `unavailable`, so the page is final (`truncated` is false): retry
  the call.
  One stalled HMC therefore costs one read timeout per page, not one per system. When the
  whole HMC has stalled, the session logoff after the page times out as well; that failure
  then replaces the page as a tool error, so the bound is one read timeout plus the logoff.

`truncated` means "more remains to read", not "more partitions exist". A later page may be
empty. Filters apply before counting.

### Failure model

1. **Actors and deployments:** an MCP agent client of `hmcpctl serve` under an operator-written
   access policy (stdio or HTTP), against one HMC per call.
2. **Invariants and assets at stake:**
   - no data from a source the policy denies for that target, whether in the result, in a
     filter outcome or in the read itself;
   - every decision the dispatch authorizer makes recorded under the delegated tool's name;
   - published identifiers stable per connection label for #1221;
   - one HMC's read load bounded per page: at most 32 feed reads, plus the client's state check
     for each system whose partition feed is empty (one read, or three on the firmware
     fallback), so at most 80; and at most one read timeout plus the session logoff.
3. **Accepted failure classes:**
   - A targets table that lists a system by name denies enumeration-path partitions, which are
     checked by UUID. Bounded: the caller passes the name as a selector.
   - `hmc_capacity_report` cannot be target-bound, so capacity is `denied` under a
     targets-table grant. This is the delegated tool's own shape.
   - Reading live state page by page can miss or repeat a partition created or deleted between
     pages. This is tolerated for a live read and stated in the tool description.
4. **Covered elsewhere:**
   - the connection check on `hmc_inventory` itself (`dispatch_authorizer`, ADR 0038);
   - target-table matching semantics (ADR 0039);
   - VIOS and storage visibility (#1221, #1225).

### Threat model

- **Boundaries:** the tool's arguments come from an untrusted MCP client. The boundary this
  design adds is one call fanning out to four delegated authorizations. HMC responses are an
  existing boundary.
- **Actors:** an agent whose policy is narrower than the HMC user's rights. Trust is placed in
  the operator's policy and the HMC credentials.
- **Controls:** the bounds under *Inputs* and the cursor checks validate arguments. Each source
  is authorized before its read, and a filter on a denied source drops that system's
  partitions. `detail` carries authorizer or HMC error text, or fixed inventory text naming a
  delegated tool and a next action; it carries no data from a denied source.
- **Out of scope:** an HMC user who can read more than the policy allows, outside hmcpctl.

## Success

1. A permitting policy lists systems and partitions with scoped ids, state, capacity and owner.
2. Two partitions with the same name on two systems, and one system read through two profiles,
   all have distinct ids.
3. A capacity figure `system_capacity` refuses is `null` with `capacity` `unavailable`. A
   system with no partitions has `partitions` `ok` and contributes none.
4. Under a targets-table policy, a call without `systems` reads nothing and reports `denied`. A
   call with `systems` admits each selector separately. A denied selector or source returns
   none of its data, and the audit stream records each authorizer decision under the delegated
   tool.
5. An `HMCError` on one system's partitions, or an ambiguous selector name, makes that system
   or selector `unavailable`, and the other systems still answer. A transport error ends the
   page with a cursor at the next system.
6. Following `next_cursor` across a 3-system, 450-partition inventory at `limit=200` returns each
   partition exactly once. A 20-system inventory spans two pages of 16 and 4 systems.
7. `lpar_state` and `owner` filter before paging. An `owner` filter on a system whose ownership
   source is denied returns none of that system's partitions.

## Validation

Each Success item is covered by a focused test in one of these files:

- `tests/unit/test_logical_inventory.py` (domain, fake client): Success 1–3 and 5–7, plus input
  bounds and the cursor;
- `tests/app/test_logical_inventory_tool.py` (served application, real policies): Success 4,
  the registration shape and the audit records.

The existing guardrails check the generated tool docs, the capability ledger and tool counts.
