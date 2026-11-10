# LPAR configuration, DLPAR and boot-order contract verification (V2b)

Issue #1345 (split from #626, epic #620). Pattern: #627 / PR #1320, #629 / PR #1361.

## Problem

Eight registered operations in this slice have no maturity record, and their
`docs/capabilities/operations.json` rows are a bulk assignment naming CLI commands
(`chlparstate`, `migrlpar`, `lpar-netboot`) and REST jobs that none of them issue.
The only live coverage is ST8's `hmc_modify_lpar` call, which declares an expected
HTTP 406 (`_REST_MODIFY_UNSUPPORTED`). That declaration predates the read-modify-write
write path (`HMCClient.update_logical_partition`, ADR 0171 terms), and it turns any 406
into a SKIP and a gap row instead of a failure. Nothing exercises rename, DLPAR, or
boot order on hardware, and nothing records the activated-partition case #1170 raises.

## Operations in scope

Selector resolution (system and partition reads) and the ADR 0092 ownership read are
shared plumbing and are not bound, as in PR #1320 and PR #1361.

| Operation | Issues | Rows bound | Implemented variant(s) |
|---|---|---|---|
| `lpar.modify` | `LogicalPartition` GET + POST (`If-Match`); assignments delegate to `pcie.assign_dedicated_slot`, `sriov.assign_logical_port`, `vnic.add` | `rest:managed-system/logical-partition`, `cli:commands/chhwres`, `cli:commands/chsyscfg`, `cli:commands/lshmc`, `cli:commands/lshwres`, `cli:commands/lssyscfg` (delegated writes and their supporting reads) | `resource-read-modify-write`, `delegated-pcie-assignments` |
| `lpar.rename` | same GET + POST | same | `name-read-modify-write` |
| `lpar.dlpar_proc` | same | same | `processor-read-modify-write` |
| `lpar.dlpar_mem` | same | same | `memory-read-modify-write` |
| `boot_order.set` | same, `BootListInformation/PendingBootString` | same | `pending-boot-string` |
| `boot_order.clear` | ownership read only, then refuses (#1048) | same | `authorized-refusal`; missing `clear-pending-boot-order` (state `partial`) |
| `lpar.set_minimum_affinity_policy` | `lssyscfg` (capability), `chsyscfg` | `cli:commands/lssyscfg`, `cli:commands/chsyscfg` | `power11-capability-gated` |
| `system.modify` | `ManagedSystem` POST | `rest:managed-system` | `managed-system-attributes` |

The original ST39 resource run did not exercise assignment delegates. Issue #1386
extends this same arm with an optional dedicated assignment through hmc_modify_lpar
and binds the actual delegate writes/supporting reads. It restores the scratch
profile before SMS and emits a separate observation only after an attempted case
and cleanup. SR-IOV/vNIC live writes remain named prerequisite gaps.

## Design

1. **Arm `lpar-config`, subtask 39** (orchestrator-assigned). `SUBTASKS[39]` dispatches
   `exercise_lpar_config` in a new module `scripts/live_test/arms/lpar_config.py`;
   `SUBTASK_GROUPS["lpar-config"] = [39]`, not in `all`; the subtask SKIPs unless
   `state.group == "lpar-config"`. Wrapper `scripts/live_lpar_config.py`, tested by
   `tests/scripts/test_live_lpar_config.py`. Preflight lists what it mutates.
2. **Scratch partition.** Name `hmcpctl-live-lpar-<8 hex>`, caller token
   `lparcfg-<8 hex>` (same hex), created through `hmc_create_lpar` with explicit
   resources: memory 1024/2048/4096 MiB, shared uncapped, 0.1/0.5/1.0 processing units,
   1/1/2 virtual processors. The arm creates nothing while any partition named
   `hmcpctl-live-lpar-*` exists on the system. Every call names the partition by UUID
   and the system by name, except rename's by-name checks.
3. **Baseline and compare.** Before the create the arm reads, through `hmc_run_command`:
   `lssyscfg -r lpar -m <sys> -F name`, `lshwres -r proc -m <sys> --level sys -F
   curr_avail_sys_proc_units`, `lshwres -r mem -m <sys> --level sys -F
   curr_avail_sys_mem,mem_region_size`. After the delete it reads the first three again
   and records `system baseline compare` PASS only when each equals the baseline (the
   name list as a set). When a read differs it waits 30 s and reads once more; the row
   carries both reads, and only the second decides. A failed baseline read SKIPs the
   arm before any create.
4. **Read-back.** Each postcondition reads `hmc_get_lpar` (UUID + system) and takes leaf
   text from `Resource/PartitionMemoryConfiguration/{Minimum,Desired,Maximum}Memory` and
   `Resource/PartitionProcessorConfiguration/SharedProcessorConfiguration/
   {Desired,Maximum}{ProcessingUnits,VirtualProcessors}`, compared numerically
   (`float`). The boot order reads `hmc_read_lpar_boot_order`.
5. **Not Activated cases** (`R` = `mem_region_size` from the baseline):
   - `lpar.modify`: desired memory `2048 + R`, maximum memory `4096 + R` and desired
     units `0.6` in one call; assertions `memory-read-back`,
     `processing-units-read-back`, `other-values-unchanged`. This call is criterion 3's re-capture of the
     declared 406 (ST8's call changes desired and maximum memory the same way).
   - `lpar.dlpar_mem`: small (`desired + R`), large (`desired = max`), no-op (current
     desired again), empty request; assertions `small-change-read-back`,
     `large-change-read-back`, `no-op-accepted-unchanged`, `empty-request-refused`.
     Then over-maximum (`max + R`) is a non-promoting row recording the HMC's answer and
     the read-back.
   - `lpar.dlpar_proc`: small (units `+0.1`), large (units and virtual processors at
     their maximum), no-op, empty request; the same four assertion ids.
   - `lpar.rename`: to `<name>-rn`, then back; assertions `renamed-same-uuid`,
     `old-name-absent`, `ownership-stamp-kept`, `original-name-restored`.
   - `boot_order.set`: two Open Firmware paths; assertion `pending-boot-string-read-back`
     (the pending string equals the space-joined paths).
   - `boot_order.clear`: assertions `clear-refused-after-authorization` (the call fails
     with the #1048 refusal text) and `pending-boot-string-unchanged`.
   An empty request is a tool-side refusal: its assertion holds only on a FAIL whose
   message names "Nothing to change". Rename's old-name check follows
   `pcie.name_absent`'s rule (#906): HSCL8012 is believed at once, and an answer that is
   neither HSCL8012 nor a readable description is read once more after the delay. The
   delete is confirmed from the partition listing, read twice, so no name of this run's
   partition (renamed or not) remains.
6. **Activated cases (#1170).** Read the associated profile UUID from `hmc_get_lpar`
   (`AssociatedPartitionProfile`), power on to SMS with `wait=True`, poll
   `hmc_get_lpar_state` until `open firmware` or `running`; if it never gets there the
   DLPAR calls are skipped with a SKIP row. Then one small `dlpar_mem` and one small
   `dlpar_proc`, each a `state.record` row (never an observation) carrying the call's
   answer, the tool's `change_location`, and a read-back that adds the `Current*`
   leaves (`CurrentMemory`, `CurrentProcessingUnits`). The row's note follows the
   answer: a refusal is recorded verbatim as the activated-DLPAR gap; an acceptance
   says whether the `Current*` value moved. Power off immediately, wait for
   `not activated`. Power calls are `state.record` rows: lifecycle evidence is #1346's.
   The live answer, whichever it is, is copied into the Live gaps table and the
   runbook section after the run.
7. **Teardown.** When the create response carries no UUID, the arm looks the name up
   and accepts it only with this run's caller token (the `_created_despite_failure`
   shape, #906). It powers the partition off when it is not `not activated`, deletes by
   UUID only when the description still carries this run's caller token, and confirms
   absence. Every exit with the partition not confirmed deleted (unknown UUID, power-off
   failure, foreign token, delete refused, absence unconfirmed) records one
   `MANUAL RECOVERY REQUIRED` row naming the shutdown and `rmsyscfg` commands.
   Observations are recorded after teardown, at literal `record_verified` call sites
   (as `users.py` does), with `cleanup="passed"` only when the delete is confirmed and
   the baseline compare holds; otherwise `"failed"`.
8. **ST8.** Drop `_REST_MODIFY_UNSUPPORTED`; ST8 records its modify call with
   `state.record`, so a 406 is a FAIL row, never a gap.
9. **Recovery.** `live_test_recovery.py` witnesses subtask 39: any partition named
   `hmcpctl-live-lpar-*` in `lssyscfg -r lpar -m <sys> -F name,state` is stranded; the
   finding prints `chsysstate ... -o shutdown --immed` (when not `Not Activated`) and
   `rmsyscfg -r lpar -m <sys> -n <name>`. The reserved prefix is the identity, as the
   users arm's is for HMC users: nothing else may create a partition under it.
10. **Catalog.** Rebind the rows above; add the eight maturity records; copy each
    emitted observation unchanged (ADR 0126); regenerate the runtime projection and
    `docs/tools/`; `CHANGELOG.md`; `docs/live-testing.md` gets an arm section, a row in
    the arm table and the recovery table.

Defects the live run confirms inside `src/hmcpctl/operations/lpar/` are fixed here; one
in `client/` or `documents/` is reported to the orchestrator before any edit.

## Live gaps (durable record)

| Case | Prerequisite | State |
|---|---|---|
| DLPAR on an activated partition | a partition running an operating system with an active RMC connection | refused live at SMS (2026-10-06, V10R3): `REST0126` carrying `HSCL7016` (the partition must be running), plus `HSCL295A` for processors; not promoted |
| `lpar.set_minimum_affinity_policy` | a writable POWER11 system (the POWER11 HMC is read-only) | not run |
| `system.modify` | an operator window for a system-wide setting change | not run |
| `lpar.modify` assignments and their rows | ST39 dedicated case (#1386); SR-IOV/vNIC require separate authority/hardware | original resource run did not exercise assignments |
| clearing a pending boot order | an HMC release that accepts a clear value (#1048) | not available |

## Success

- The eight operations carry the rows and variants in the table, and a maturity record.
- The six operations exercised by the arm carry one live observation each, emitted by a
  run at the branch's final `src/` closure and copied with its emitted result.
- After the run the system's partition-name set and available processing units and
  memory equal the pre-run read, and no `hmcpctl-live-lpar-*` partition exists
  (recovery exit 0 for subtask 39).
- Every mutating call the arm makes names the scratch partition's UUID (rename's
  `new_name` aside), checked by a unit test over the scripted HMC.
- `just verify` and `uv run --no-sync prek run --all-files` pass.

## Failure model

1. **Actors and deployments:**
   - a local operator dispatching `scripts/live_lpar_config.py` from the live-test host
     against the V10R3 HMC and POWER9 boundary system;
   - CI, offline (unit tests with a fake client).
2. **Invariants and assets:**
   - every partition other than the scratch one, including the VIOS and the test
     partition: untouched;
   - the system's processor and memory pools return to the baseline;
   - the honesty of `maturity.json`: no observation from a SKIP, a transport success,
     or a submitted job.
3. **Accepted failure classes:**
   - An interrupted run can leave the scratch partition, possibly activated. Cost bounded
     to one small partition; recovery names it by prefix and prints the commands.
   - A concurrent change by another operator, or pool accounting that lags the delete by
     more than the one re-read, fails the compare; the run is then not clean and is
     re-run, never patched.
   - A partition someone else creates under the reserved prefix is reported by
     recovery as stranded; the prefix is documented as reserved.
   - The activated case runs in firmware only; DLPAR with RMC stays a gap.
4. **Covered elsewhere:**
   - power and lifecycle contracts (#1346); missing configuration fields (#638); profile
     CRUD (#637);
   - authorization semantics (ADR 0092, unchanged); environment isolation of tests (#461).
