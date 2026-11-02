# 0313 — LPAR provision is one journaled operation with a required ownership stamp

## Status

Accepted (2026-11-02), issue #1839 (piece 1 of #1225). Implements the H1 spec's
`hmc_provision_lpar` row, without `install`, under ADR 0189 Decision 2 and ADRs 0190, 0195 and
0236, with ADR 0199 as the wiring precedent. ADR 0005's step workflow stands as the order of
writes; its best-effort stamp and its partial-failure result no longer describe provision.

## Context

- `provision_lpar` runs its writes directly: no `request_id`, no journal, and a stamp that may
  fail as a warning (`operations/lpar/provision.py`, main `ddf3429a`).
- The engine replays a body against its journal; an open effect is classified by a live read
  passed in by the submitter, and a kind without one pauses (ADRs 0195, 0236).
- `create_and_stamp_lpar` performs the create, its follow-ups (profile apply on the `mksyscfg`
  path, physical I/O) and the stamp in one call; physical I/O refuses to apply unless the stamp
  was confirmed (`operations/lpar/core.py`).
- The plan contract already gives provision `storage.capacity_mib` for a new disk and a digest
  that hashes `exclusive_writer_window` as `false` without `install` (ADR 0198, plan spec).

## Decision

1. **Inputs.** `request_id` is required, on the MCP tool and as the CLI's `--request-id`, with
   no compatibility path. The MCP tool adds `continuation` (`none` / `resume` / `abandon`),
   `wait_seconds` and `expected_plan_digest`; `storage` adds `capacity_mib`. A continuation
   needs only `request_id`; any other value it gives must match the record.
2. **Result.** `ProvisionResult` keeps its fields and adds `operation`, the operation record
   with its `result` omitted. The record's `result` is the `ProvisionResult` the body returned.
   While no result exists the top-level fields are read from the effects. A clean run ends
   `completed`; `configured` stays reserved for #1840's profile readback.
3. **Effects, in order** (keys and kinds are persisted):

   | Key | Kind | Classifier (H1 *Resume*) |
   | --- | --- | --- |
   | `create` | `lpar.create` | partition by name on the system: none → not applied; one with no foreign stamp → applied; one with physical I/O requested, a foreign stamp, or several → attention |
   | `stamp` | `lpar.ownership_stamp` | description owner (and caller token): this agent → applied; none → not applied; other → attention |
   | `affinity_policy` | `lpar.minimum_affinity_policy` | none |
   | `network` | `lpar.client_network_adapter` | client adapters on the VLAN: none → not applied; one → applied; several → attention |
   | `disk` | `vios.virtual_disk` | disk name in the volume group → applied, else not applied |
   | `storage` | `vios.storage_map` | a mapping of the storage to this partition → applied, else not applied |
   | `assignments` | `lpar.pcie_assignments` | none |
   | `power_on` | `lpar.power_on` | partition activated → applied, else attention (ADR 0199) |

   `network` records `{adapter_uuid, mac}` with the MAC in lower-case colon form; `disk`
   records `{vios_uuid, vg_uuid, disk_name, capacity_mib}`.
4. **The stamp is required.** The `stamp` effect confirms the stamp the create wrote and
   writes it only when absent. A stamp still absent after that write is `EffectNotApplied`, so
   the run ends `failed`. A create whose own follow-up step reports an error also ends `failed`.
   A create the HMC refused, proven by a name lookup that finds no partition, ends `failed`.
5. **Later legs pause.** An error in any effect after `stamp` stops the remaining steps and
   ends `needs_attention` with the result kept; `resume` classifies and continues.
6. **Ledger vocabulary**, keyed by the operation and key, for #1846–#1848 to consume:
   `partition` (key `partition`, identity `{lpar_uuid, name}`); `virtual_disk` (key
   `root_disk` here, identity `{vios_uuid, vg_uuid, disk_name}`); and `installer_media`,
   reserved for #1842 with identity `{vios_uuid, media_name}`. Only a disk this operation
   created is written; mapped existing storage is never owned.
7. **Plan digest.** When `expected_plan_digest` is given for a new request, the handler runs
   `plan_lpar` with the plan's own delegated reads and refuses, before anything is recorded,
   unless the fresh `plan_digest` equals it. `physical_io`, which the plan does not cover,
   refuses the combination.
8. **Delegated authority.** Before anything is recorded, the handler requires the policy to
   permit and the dispatch authorizer to admit, for the resolved selectors: `hmc_create_lpar`,
   `hmc_add_network_adapter` and `hmc_map_storage_to_lpar` always; `hmc_create_virtual_disk`
   with `capacity_mib`; `hmc_set_minimum_affinity_policy` with a policy; `hmc_power_on_lpar`
   with `power_on`.
9. **`dry_run`** validates `request_id` and runs the preconditions (and the digest check)
   without submitting: no operation, lock or guard.

## Consequences

- Callers must send `request_id`; a repeat returns the same operation and never duplicates a
  partition. The step list gains `stamp`, and `create`'s result is the UUID and name only.
- A provision interrupted with `affinity_policy` or `assignments` open always needs a human.
- A resumed create classified `applied` does not re-run a `mksyscfg` profile apply; a power-on
  that then fails pauses.
- No `exclusive_writer_window` yet: disk creation keeps the specialist's behavior until #1840.
- Effect keys, kinds and ledger kinds are a persisted contract (ADR 0195).

## Considered & rejected

- **Do nothing.** judgment: fit. #1839 and the H1 spec require the journaled tool.
- **Return the bare operation record, as power does.** judgment: fit. The H1 spec keeps
  `ProvisionResult`'s fields, and the live arms read them; a dry run has no record to return.
- **Report `configured`.** judgment: fit. The H1 spec ties it to a profile readback (#1840).
- **Ledger a mapped existing disk as the root disk.** judgment: fit. `delete_owned` (ADR 0192)
  would then delete storage the caller brought.
- **Split stamping out of `create_and_stamp_lpar`.** verified: physical I/O applies only after
  a confirmed stamp (`core.py` `created_io_step` guard, main `ddf3429a`), so a deferred stamp
  would break it.
- **Require `exclusive_writer_window` for disk creation now.** judgment: fit. The plan digest
  hashes it `false` without `install`, so the flag would make plans unmatchable before #1840.
- **Delegate `hmc_add_vscsi_adapter`.** verified: provision adds no vSCSI adapter; the mapping
  creates the pair (ADR 0169, #1030).
