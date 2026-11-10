# Verify system and LPAR read and composite contracts (V2a)

Issue #1344, a split of #626 (epic #620, entry V2). No new decision record. This follows the
#627 pattern (PR #1320).

## Problem

ST1 (`scripts/live_test/arms/connectivity.py`) dispatches `system.list`, `capacity.report` and
`placement.find`. It records them through `record_with_expected`, which never promotes and
never fails an observation. It also repeats `hmc_get_system` through `state.record`. Six more
read operations in this slice have no live scenario: `lpar.list_ownership`, `lpar.inspect`,
`lpar.plan`, `inventory.logical`, `health.fleet` and `boot_order.read`. Most of these have no
maturity record either. Their `row_ids` in `docs/capabilities/operations.json` name rows they
never issue, such as CoD commands for `capacity.report` and LPAR job rows for
`boot_order.read`. `lpar.list_refcodes` is the one operation that issues `lsrefcode`, but its
`composite_reason` says that row stays under `capacity.report`, which never runs it.

A 500 on V10R3's managed-system feed was declared as an expected limitation. It is the
null-`VirtualPersistentMemoryVolume/Uuid` serialization failure (#784; fallback in ADR 0138).
Recorded evidence ties that 500 to the `X-HMC-Schema-Version: V1_0` request header (sent only
when `HMC_SCHEMA_VERSION` is set). Without the header the feed answered 200. ADR 0138's client
fallback hides the 500: `inventory_managed_systems` rebuilds the list from `quick/All` and
drops unresolved systems with only a log warning. `hmc_list_systems`, `capacity.report` and
`placement.find` then return success, and the declared 500 reaches the harness only when the
fallback resolves nothing.

## Operations in scope

Each handler's reads were traced to the client methods it calls. All of them are HTTP GETs on
the REST UOM API or `lsrefcode`. None of them writes. `lpar.plan` and `placement.find`
reserve nothing.

| Operation | Reads issued | Rows bound |
|---|---|---|
| `system.list` | ManagedSystem feed or search; quick/All fallback | `rest:managed-system` |
| `capacity.report`, `placement.find` | ManagedSystem feed; each system's LogicalPartition feed | `rest:managed-system`, `rest:managed-system/logical-partition` |
| `health.fleet` | as above, plus each system's VirtualIOServer feed | the two above plus `rest:managed-system/virtual-i-o-server` |
| `lpar.list_ownership`, `boot_order.read`, `lpar.get_state` | system resolution; LogicalPartition feed, entry or quick property | `rest:managed-system`, `rest:managed-system/logical-partition` |
| `lpar.list_refcodes` | `lsrefcode -r lpar` | `cli:commands/lsrefcode` (moved from `capacity.report`) |
| `lpar.inspect`, `lpar.plan`, `inventory.logical` | delegated tool reads | none; their existing `composite_reason` is accurate and kept |

A bound row's `composite_reason` is `null`. The rebinding leaves seven rows bound by no
operation: `cli:commands/lscod` (#695), `cli:commands/lscodpool` and `cli:commands/mkcodpool`
(#640), the `listcodcodeinfo`, `listcodresources` and `manageelasticcod` ManagedSystem jobs
(#680), and `rest:jobs/managementconsole-jobs/referencecodelogs-job` (#678). Each keeps its
`coverage-child` disposition. The other dropped rows stay bound by their other operations. `placement.find` loses its composite reason
because it issues those two feeds itself through `fetch_capacity_report`.

## Design

1. **ST1 records every in-scope read through `record_verified`.** Every list or nested result
   is read through `results.field` (key or attribute), because FastMCP delivers a
   dataclass result as a generated model, not a mapping. Scenario ids and postcondition
   assertions:
   - **Feed probe.** ST1 first reads the raw feed with `hmc_list_resources(resource_type="ManagedSystem")`.
     This is `list_uom` with no fallback. It is recorded as a non-promoting probe row.
     `feed-served-directly` holds when the probe passed and its UUID set equals the read
     under test's UUID set. For capacity and placement, only the probe has to pass.
     That assertion is on all four reads that use `list_managed_systems`: list, capacity,
     placement and fleet health. A fallback-served success therefore records `failed`, never
     `passed`.
   - `hmc_list_systems` (`st1-system-inventory`): `system-list-non-empty`,
     `boundary-system-listed` (an entry's `SystemName` equals `config.system_name`, ignoring
     case), `entries-carry-uuid`, `feed-served-directly`.
   - `hmc_capacity_report` (`st1-capacity`): `boundary-system-reported`,
     `capacity-figures-consistent`. For the boundary row, `0 <= free <= total` and
     `assigned == total - free` hold for memory and for processor units (units within 1e-4).
     Also `feed-served-directly`.
   - `hmc_find_placement` (`st1-capacity`), asking for `config.placement_memory_mib`:
     `candidates-fit` (every candidate's free memory is at least the request and its free units
     are at least 0.5), `candidates-best-fit-first` (free memory does not decrease from one
     candidate to the next), `boundary-candidate-when-it-fits` (when the capacity report's
     boundary row covers the request, the boundary system is a candidate) and
     `feed-served-directly`.
   - `hmc_list_lpar_ownership` (`st1-lpar-inventory`, scoped to `config.system_name`):
     `ownership-entries-non-empty`, `test-partition-listed`, `ownership-facts-consistent`
     (`owned` is true exactly when `owner` is set, and `unparsed` is true only where
     `owned` is false and a description exists).
   - `hmc_read_lpar_boot_order` (`st1-lpar-inventory`, test partition): `boot-order-names-partition`
     (`lpar_uuid` equals ST1's `artifacts.lp3_uuid`).
   - `hmc_inspect_lpar` (`st1-lpar-inventory`, include `resources`, `rmc`, `refcodes`):
     `inspection-names-partition` (uuid equals `artifacts.lp3_uuid`), `resources-read`,
     `rmc-read`, `refcodes-read`. The first is `resources.storage_source.status`; the others are
     `rmc.source.status` and `refcodes.source.status`. Each must be `ok`.
   - `hmc_inventory` (`st1-logical-inventory`, `systems=[config.system_name]`):
     `boundary-system-listed`, `test-partition-listed`, `partitions-belong-to-system`
     (every partition's `system_id` equals the boundary system's `id`) and `system-sources-read`
     (the boundary system's `sources.capacity`, `.partitions` and `.ownership` are `ok`).
   - `hmc_fleet_health` (`st1-fleet-health`): `health-sections-present` (`systems`, `vios`,
     `lpars` and `warnings` are lists), `boundary-system-flag-matches-state` (the boundary
     system is in `systems` exactly when the `State` that `hmc_get_system` read is not
     `operating`) and `feed-served-directly`.
   - `hmc_plan_lpar` (`st1-lpar-plan`). It takes ST13's dry-run inputs: `config.dry_run_lpar_name`,
     VLAN `config.provision_vlan_id`, a new disk named `hmcpctl-st1` (within the VIOS's 15-character
     backing-device limit; ST13's storage name names an existing disk and has no such bound) of
     `config.provision_disk_mib`, and `config.system_name`. Assertions are
     `plan-targets-boundary-system` (a candidate's `targets.system.uuid` equals
     `artifacts.system_uuid`, ignoring case) and `plan-outcome-consistent` (`plan_digest` is
     set exactly when `selected` is set and the top-level `blockers` list is empty). A plan
     whose blockers prevent selection still passes. That is the tool reporting correctly; it
     is not a defect.
2. **Declared limitation.** The three managed-system-feed reads keep their existing
   `ExpectedOutcome` declarations, unchanged; they still match, because `HMCError` carries the
   HMC body's message. The call gets `expected=[...]`. A `SKIP` (a reused confirmation) or a
   `FAIL` the declaration matches goes to `record_with_expected`, which emits the gap row.
   Any other result goes to `record_verified`, so a non-matching failure is a `failed`
   observation and never a skip. A transport `PASS` promotes only when every assertion holds.
3. **Ordering and prerequisites.** The new reads run after `_discover_partitions` and
   `_discover_system`, so `artifacts.system_uuid` and `artifacts.lp3_uuid` exist. When either
   is missing, the assertions that compare against it do not hold, and the observation fails
   rather than skips. The redundant `hmc_get_system (capacity context)` call is deleted.
4. **Catalog.** Rebind rows as tabled. Add a `maturity.json` record (implementation
   `implemented`, one variant per exercised call shape, empty `missing_scope`) for each
   in-scope operation that lacks one. Record each live observation from the ST1 run; a
   declared limitation goes into `missing_scope` as a confirmed gap. Regenerate
   `src/hmcpctl/_operation_maturity.json` (`just capability-metadata`) and `docs/tools/`
   (`just tool-docs`). `lpar.get_state` and `lpar.list_refcodes` keep their ST35 observations.
   Rebinding rows does not change a handler's import closure, so it does not make them stale.

## Live gaps (durable record for this slice)

| Path not run | Prerequisite |
|---|---|
| `lpar.plan` live observation | the campaign's consolidated re-record round. The 2026-10-06 run at `a2667b36` was refused at input validation, because ST1 named the planned disk after the lab's 17-character ST13 storage name and the VIOS allows 15. That was a harness defect, fixed in `82cd83d6`. No observation is catalogued from that run. |
| `system.list` with a `state` filter (server-side search) | none; candidate for a later ST1 extension |
| the feed under `X-HMC-Schema-Version: V1_0` (the declared 500) | a run with `HMC_SCHEMA_VERSION=V1_0`. The ST1 run uses the `.env` as configured; when that leaves the header unset, this row stays a gap. If the orchestrator grants a second run with the header, its outcome is reported, not catalogued, because the catalog keeps one observation per operation. |
| fleet-wide `lpar.list_ownership`, `capacity.report` and `health.fleet` over a non-operating system | a non-operating system on the boundary HMC |
| `lpar.plan` with `placement` enumeration and with existing storage (`hmc_get_vios_storage_detail`) | an authorization grant covering enumeration across the HMC |
| `boot_order.read` on an activated partition (last-booted device populated) | an activated test partition; owned by #1346 |
| `inventory.logical` paging past one page | more than 50 partitions on one system |

## Success

- Each of the nine scoped operations has a ST1 `record_verified` call with the assertions
  above. `lpar.get_state` and `lpar.list_refcodes` have corrected bindings.
- `tests/test_live_runner.py` drives ST1 with scripted results. A passing script yields nine
  `passed` observations. Each assertion is shown to fail on a violating fixture. A declared
  500 yields a gap row and no observation. A feed-read success whose probe failed yields a
  `failed` observation. An undeclared failure yields a `failed` observation.
- `just verify` and `uv run --no-sync prek run --all-files` pass. `just scenario-gap` reports
  no dispatch finding for ST1.
- The live ST1 run on V10R3 is recorded honestly as passed, failed or a confirmed gap.

## Failure model

1. **Actors and deployments.** The operator runs ST1 alone, by hand, against the boundary HMC
   and system from the `default` profile. CI runs the scripted tests only.
2. **Invariants and assets at stake.** No HMC write: every dispatched tool is `effect="read"`.
   Catalog honesty: no observation may be `passed` unless its assertions held, and a
   declared limitation may never become a pass.
3. **Accepted failure classes.**
   - Apart from `feed-served-directly`, the assertions are postconditions on what was
     returned, not proof that the HMC inventory is complete.
   - `placement.find` with no candidates passes when no system covers the request. This is
     stated in Design 1.
   - A rate-limited or locked-out SSH login during `lsrefcode` fails `refcodes-read`.
     Accepted; the observation records it.
   - The feed probe compares UUID sets only. A fallback that resolved every system is
     still caught, because the probe itself fails on the same 500.
4. **Covered elsewhere.** Configuration and DLPAR: #1345. Power and lifecycle: #1346. Snapshot
   capture, inspect and validate: unowned, reported as a follow-up candidate. Staleness in
   other arms: the campaign's consolidated re-record round.
