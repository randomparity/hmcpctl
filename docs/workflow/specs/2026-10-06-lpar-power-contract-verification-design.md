# LPAR power and lifecycle contract verification (V2c)

Issue #1346 (split from #626, epic #620). Pattern: #627 / PR #1320, #1345 / PR #1366.

Design denominator: re-banded from M (250 changed lines) to L (1000) by the campaign
orchestrator on 2026-10-06. The M band came from triage before design; the implemented
arm, which drives two partitions, a VIOS volume and a durable composite operation with a
teardown for each partial failure, follows the 981-line `lpar_config.py` precedent and
lands near 1,500 lines of arm code and 1,200 of arm tests. The frozen scope requires that
work; nothing was added to fit or cut to meet the earlier number.

## Problem

The power and lifecycle operations below carry a bulk `operations.json` assignment
naming commands none of them issues (`chlparstate`, `migrlpar`, `lpar-netboot`, a
`ChangeDefaultProfileName` job). Live evidence comes only from the bare-cec arm (ST25,
rows 35), which proves the happy path of create, power on, power off, console and
delete. Nothing exercises create's refusals, delete's activated-state refusal,
power-on of a running partition, the composite `lpar.power`, `lpar.decommission`, or a
real `provision.lpar` (ST13 is a dry run; ST14 re-provisions the test partition and is
not this arm's to run).

## Operations in scope

Selector resolution, the ADR 0092 ownership read and the `change_location` read are
shared plumbing and are not bound, as in PR #1320 and PR #1366.

| Operation | Rows bound (issued requests) |
|---|---|
| `lpar.create` | `rest:managed-system/logical-partition` (PUT), `rest:managed-system` (configurable-memory read); fallback `cli:commands/mksyscfg`, `cli:commands/chsyscfg` (apply, ownership stamp) |
| `lpar.delete` | `rest:managed-system/logical-partition` (quick-property GET, DELETE) |
| `lpar.decommission` | `rest:managed-system/logical-partition` (GET, DELETE), `rest:managed-system/virtual-i-o-server` (mapping inventory), `…/client-network-adapter`, `…/virtual-scsi-client-adapter`, `…/virtual-fiber-channel-client-adapter`, `…/virtual-nic-dedicated` (adapter list and DELETE), `rest:jobs/logicalpartition-jobs/poweroff_logicalpartition-job` |
| `provision.lpar` | `lpar.create`'s rows, `…/client-network-adapter` (PUT), `rest:managed-system/virtual-i-o-server` (mapping POST), `rest:virtual-network-management/virtual-network` and `rest:virtual-storage-management/volume-group` (its prevalidation reads), `rest:jobs/logicalpartition-jobs/poweron_logicalpartition-job`; `composite_reason` cleared |
| `lpar.power_on` | `rest:jobs/logicalpartition-jobs/poweron_logicalpartition-job`, `rest:managed-system/logical-partition` (state read), `rest:managed-system/logical-partition-profile` and the three client-adapter feeds (profile containment and adapter warnings) |
| `lpar.power_off`, `lpar.dump_restart` | `rest:jobs/logicalpartition-jobs/poweroff_logicalpartition-job` |
| `lpar.capture_console` | `cli:commands/mkvterm`, `cli:commands/rmvterm` |
| `system.power_on` / `system.power_off` | `rest:jobs/managedsystem-jobs/poweron_managedsystem-job` / `…/poweroff_managedsystem-job` |
| `lpar.power` | composite (ADR 0189, 0199); unchanged, no rows |

Task 1 confirms each row against the code before writing it; a request the code does
not issue is not bound. Delegates of `provision.lpar`'s assignment step
(`pcie.assign_dedicated_slot`, `sriov.assign_logical_port`, `vnic.add`) keep their
own records (#630).
## Design

1. **Arm `lpar-power`, subtask 41** (orchestrator-assigned). `SUBTASKS[41]`
   dispatches `exercise_lpar_power` in a new `scripts/live_test/arms/lpar_power.py`;
   `SUBTASK_GROUPS["lpar-power"] = [41]`, not in `all`. The subtask SKIPs unless
   `state.group == "lpar-power"` and `HMC_AUTHORIZE_POWER_OPERATIONS` is on
   (`bare_cec._power_operations_authorized`), so the power evidence covers the ADR 0092
   ownership-guarded path, as bare-cec's does. Wrapper `scripts/live_lpar_power.py`,
   tested by `tests/scripts/test_live_lpar_power.py`; arm behaviour in
   `tests/test_live_lpar_power_arm.py` over a scripted client. Rows are numbered 41,
   scenario `st41-lpar-power`.
2. **Run-owned names.** Prefix `hmcpctl-live-pwr-` (reserved; recovery reports any
   partition carrying it), suffix 8 hex: partition A `hmcpctl-live-pwr-<hex>`,
   provision partition P `…-<hex>-p`, caller token `lparpwr-<hex>`, logical volume
   `lppwr<hex>` (13 characters). The arm creates nothing while any prefixed partition
   or `lppwr`-prefixed volume exists. Every mutating call names a run partition by
   UUID once it has one, the run volume, or a mapping backed by it; create, provision
   and decommission name the system.
3. **Baseline.** Before any write, recorded as `system baseline`:
   `lssyscfg -r lpar -F name,state`; available processing units and memory (the #1345
   reads); `lshwres -r io --rsubtype slot -F drc_index,lpar_name`; the VIOS's mappings
   (`hmc_list_storage_mappings`); its vSCSI server adapters
   (`lshwres -r virtualio --rsubtype scsi --level lpar --filter lpar_names=<vios>
   -F slot_num,remote_lpar_name,remote_slot_num`, the read `_ADAPTER_SIDE_EFFECT` in
   `client/client_storage.py` names); the configured group's volume names. When no VIOS
   qualifies (step 10) the VIOS reads are omitted. After teardown the same reads are
   compared (one 30 s re-read on a difference) as `system baseline compare`. A failed
   baseline read SKIPs the arm. The operator's private before/after snapshot (dispatch)
   covers the same set.
4. **Create (A)** — observation `lpar.create`. Three cases are hmcpctl's own pre-request
   guards, observed live (after hmcpctl's own reads, before any HMC write; one the guard
   lets through is a FAIL and the teardown's prefix sweep removes the partition): `units-over-vcpus-refused`
   (desired 1.5 units, 1 virtual processor: "virtual processor uses at most 1.0"),
   `memory-over-configurable-refused` (desired 64 TiB: "configurable memory", read from
   the live `ConfigurableSystemMemory`), `duplicate-name-refused` (a second create of A's
   name: "already exists", A's UUID unchanged). HMC-side: `resources-read-back` (memory
   min/desired/max, shared units and virtual processors read from `hmc_get_lpar` equal
   the request), `ownership-stamped` (the description's caller token is the run's).
   Resources as #1345's `RESOURCES`.
5. **Power on (A)** — observation `lpar.power_on`: `profile-activation-reached-firmware`
   (profile UUID from `AssociatedPartitionProfile`, `boot_mode=sms`, `wait=True`; job
   successful and state `open firmware` or `running`), `running-reported-without-job`
   (a second call with `boot_mode=of`: `already_running` true, `job` null, message names
   the unapplied boot mode, state unchanged), `current-configuration-reached-firmware`
   (no profile, `boot_mode=of`, `operation_type=activate`, `keylock=norm`). No
   configuration change precedes either activation, so #1345's
   profile-discards-current-configuration finding does not affect them.
6. **Delete refused while activated, console.** `hmc_delete_lpar` on activated A must
   fail with "must be 'not activated'" and leave A listed (feeds `lpar.delete`).
   `hmc_capture_lpar_console` (30 s, idle 30 s, as bare-cec) — observation
   `lpar.capture_console`: `console-captured`, `console-released`.
7. **Power off (A)** — observation `lpar.power_off`: `delayed-shutdown-not-activated`
   (`immediate=False`, `wait=True`), `immediate-shutdown-not-activated`.
8. **Composite (A)** — observation `lpar.power`, one `request_id` per call
   (`lparpwr-<hex>-<n>`), `wait_seconds=600`, then `hmc_operation_status` until
   `terminal` or `paused` (bounded 10 polls, 30 s; `paused` fails the assertion):
   `start-completed-activated`, `restart-immediate-completed-activated`,
   `stop-immediate-completed-not-activated`, `repeat-stop-already-in-state` (a new
   request id: `already_in_state` true, `job_id` null), `same-request-replays`
   (repeating the stop's arguments and request id returns its `operation_id`).
   Teardown issues `continuation="abandon"` for each run request id not `terminal`.
   Partition A's call order: create cases, profile activation, running re-call, delete
   refusal, console, delayed power-off, current-configuration activation, immediate power-off,
   composite, delete. A step that leaves A's state unknown sends the arm to teardown.
9. **Delete (A)** — observation `lpar.delete`: `activated-delete-refused`,
   `partition-kept`, `delete-call-succeeded`, `lpar-name-absent` (listing read twice,
   #1345 `_gone` rule).
10. **Provision (P).** Preconditions, each a SKIP of steps 10–11 naming why: exactly one
    VIOS lists the configured volume group (`LIVE_TEST_VDISK_VOLUME_GROUP_NAME`) with
    1 GiB free; `LIVE_TEST_PROVISION_VLAN_ID` has a virtual network. The arm creates the
    1 GiB volume (`hmc_create_virtual_disk`, a plain row: #1348 owns its record), then
    `hmc_provision_lpar` with P's name, the run token, `VirtualDisk`, the VLAN, small
    resources and `power_on=True`, and no PCIe argument. Before it, one dry-run
    provision carrying a dedicated slot must be refused before any HMC read (#1390,
    docs/workflow/specs/2026-10-13-provision-dedicated-pcie-refusal-design.md).
    Observation `provision.lpar`: `workflow-completed` (every step `ok`),
    `ownership-stamped`, `network-adapter-on-vlan`, `storage-mapping-listed` (a VIOS
    mapping backed by the volume names P's UUID), `partition-activated` (state polled to
    an activated state), and `dedicated-slot-refused`.
    A provision that returns no UUID, or raises, adopts P by name and run token
    (`lpar_config._adopt_by_name` shape) for teardown.
11. **Decommission (P)** — observation `lpar.decommission`: `dry-run-inventoried`
    (`dry_run=True`: `resource_deleted` false, every step `dry_run`, blast radius lists
    the network and vSCSI client adapters and the volume's mapping),
    `dry-run-changed-nothing` (state, all client adapters, complete vSCSI inventory,
    and volumes unchanged). The real call runs with the mapping still present
    (`immediate=True`): `resource-deleted`, `workflow-completed`, `lpar-name-absent`,
    `storage-mapping-absent` (complete VIOS detail and unchanged foreign mappings),
    and `backing-volume-retained`. No separate pre-detach is allowed. A retention
    latch is set before the first proof/preview await and clears only after every
    typed success flag and complete postcondition passes; cancellation, exceptions,
    failed or incomplete reads retain it. No automatic retry follows ambiguity.
12. **Teardown, in this order:** abandon open composite operations. A retained
    decommission latch records manual recovery and stops all resource cleanup.
    Otherwise, detach any mapping
    backed by the run volume while its client partition still exists; power off and
    delete each run partition not confirmed deleted, by UUID only while its description
    carries the run token (#1345 `_delete` shape, adopting by name when the UUID is
    unknown), except that P is kept while a mapping backed by the run volume could not
    be detached (the tool could not reach that mapping once P is gone); delete the run
    volume once no mapping is backed by it. Anything left
    records one `MANUAL RECOVERY REQUIRED` row naming the commands (for a mapping:
    `rmvdev -vtd <device>` on the VIOS before `rmlv`). Observations are recorded after
    teardown at literal `record_verified` sites, cleanup `passed` only when every run
    object is confirmed gone and the baseline compare holds.
13. **Gaps, recorded as SKIP rows naming the prerequisite, never as calls:**
    `lpar.dump_restart`, SR-IOV and vNIC provision arguments, graceful composite stop,
    system power.
14. **One owning arm per observed operation.** The reader keeps one live observation
    per operation (`check_capability_inventory.py`), so this arm owns `lpar.create`,
    `lpar.power_on`, `lpar.power_off`, `lpar.delete` and `lpar.capture_console`;
    bare-cec's `record_verified` sites for those five become plain `state.record` rows
    with the same PASS/FAIL judgement (its PCIe-fixture facts stay with the `pcie.*`
    observations, except bare-cec's "no profile lists the released slot" half of
    `slot-released`, which no remaining observation asserts; the dedicated arm's
    `fixture-slot-unowned` keeps the ownership half). The dedicated arm (`pcie.py`, #630) still emits `lpar.create` and
    `lpar.delete` observations for its fixture; demoting them needs an edit to #630's file
    and is reported to the orchestrator, so until then the copier keeps whichever
    observation it copies last. A separate arm, not new steps in `bare_cec.py` / `provisioning.py`:
    bare-cec skips without the dedicated-PCIe fixture, and ST13/ST14 act on the shared
    test partition.
15. **Recovery and preflight.** `live_test_recovery.py` witnesses subtask 41: a
    `hmcpctl-live-pwr-*` partition, a mapping backed by an `lppwr*` volume, or a VIOS
    server adapter whose remote partition is a run partition is stranded and prints its
    commands, as is an `lppwr*` volume left in the configured group: recovery admits
    exactly one VIOS command shape, `viosvrcmd -m <system> --id <vios id> -c 'lsvg -lv
    <group>'`, on the VIOS whose volume-group listing names the group.
    A server adapter the run added and left gets its own manual-recovery row from the
    arm's compare. Preflight lists the arm's mutations.
16. **Catalog.** Rebind the rows above; add maturity records for `lpar.power`,
    `lpar.decommission`, `provision.lpar`, `system.power_on`, `system.power_off`; copy
    each emitted observation unchanged (ADR 0126), replacing the bare-cec observation
    for the five operations of step 14; regenerate the runtime projection and
    `docs/tools/`; `CHANGELOG.md`; `docs/live-testing.md` arm section, arm table and
    recovery table.

Defects the run confirms in `src/hmcpctl/operations/lpar/` are fixed here; one in
`client/`, `documents/` or another package is reported to the orchestrator first.

## Live gaps (durable record)

| Case | Prerequisite | State |
|---|---|---|
| `lpar.dump_restart` | orchestrator approval to run bare-cec with `LIVE_TEST_ACCEPT_PLATFORM_DUMP=true`, whose dumprestart row would need to become a `record_verified` site; this arm builds no dump path | not run |
| `provision.lpar` SR-IOV and vNIC arguments | orchestrator-relayed operator approval to consume shared SR-IOV adapter capacity | not run |
| `lpar.decommission` with a mapped vSCSI volume | native exact mapping detach before client adapters and partition deletion, complete postcondition reads, retained backing volume and unchanged foreign baseline | first native proof required; historical pre-detach evidence does not satisfy this |
| `lpar.power` graceful stop / restart | an OS with an active RMC connection | not run |
| `lpar.power_on` network boot | #638 | not implemented |
| `system.power_on`, `system.power_off` | an operator window to power the whole system | not run |
| `lpar.power_on` with `operation_type` | an HMC release that accepts the `OperationType` PowerOn parameter | refused live (2026-10-07, V10R3): `FAILED_TO_START`, "Parameter 'OperationType' is not allowed for this Job."; the request without it reached Open Firmware; observation failed |
| `provision.lpar` dedicated slot live after PowerOn | profile-first provisioning (#1225); until then provision refuses the argument (#1390) | observed live: the slot is in the profile, not owned by the running partition; observation failed |
| `hmc_detach_storage_mapping` answer when the VIOS reads RMC active | an explanation of `HSCL2957` on a detach that takes effect | observed live twice: error answer, mapping removed |

## Success

- The operations in the table carry the rows shown and a maturity record each.
- The eight operations the arm observes (`lpar.create`, `lpar.power_on`,
  `lpar.power_off`, `lpar.delete`, `lpar.capture_console`, `lpar.power`,
  `provision.lpar`, `lpar.decommission`) carry one live observation each, copied with its
  emitted result; bare-cec emits none for the first five. The run is repeated when
  anything it executed changes afterwards — the `src/hmcpctl/` closure,
  `scripts/live_test/arms/lpar_power.py`, or the runner, preflight and recovery hunks;
  catalog and documentation commits after the run are exempt.
- After the run every read of step 3 equals its pre-run read; no prefixed partition or
  volume exists (recovery exit 0 for subtask 41).
- Unit tests over the scripted client pin: each assertion's pass and fail reading;
  every mutating call naming a run partition (UUID once known), the run volume or its
  mapping; the dedicated-slot refusal (a dry run only, and no slot in the real
  provision); the gap SKIP rows of step 13 with no dump or
  system-power call; teardown order (detach before partition delete) on a provision
  that fails after its storage step; adoption when provision returns no UUID; recovery
  rows.
- `just verify` and `uv run --no-sync prek run --all-files` pass.

## Failure model

1. **Actors and deployments:**
   - the operator dispatching `scripts/live_lpar_power.py` from the live-test host against
     the V10R3 HMC and the POWER9 boundary system;
   - CI, offline (unit tests with a scripted client).
2. **Invariants and assets:**
   - every partition other than A and P, including the VIOS and the test partition;
   - the VIOS's other volumes and mappings, the volume group, the media repository;
   - the processor and memory pools; I/O slot ownership;
   - `maturity.json` honesty: no observation from a SKIP, a transport success or a job
     submission.
3. **Accepted failure classes:**
   - an interrupted run leaves A, P, the volume or its mapping; cost bounded to one small
     partition and 1 GiB; recovery names the partitions, mappings, adapters and volumes
     by prefix with the commands;
   - a concurrent change by another operator, or pool accounting that lags by more than
     one re-read, fails the compare; the run is re-run, never patched;
   - the volume-group read-modify-write (#936) and an unpaired server adapter after a
     failed map (#1237) are #1348's classes; this arm's compare reports them;
   - a VIOS server adapter left by the detach shows in step 3's server-adapter compare
     as a FAIL and a manual recovery row; the arm never removes VIOS adapters.
4. **Covered elsewhere:** configuration and DLPAR (#1345); dedicated-slot operations
   (#630); storage operation records (#1348); network boot (#638); migration and remote
   restart (#631); authorization semantics (ADR 0092, 0189).
