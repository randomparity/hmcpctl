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
  passed in by the submitter, and a kind without one pauses (ADRs 0195, 0236). A write that
  returns settles `applied`; only `EffectNotApplied` settles `not_applied`.
- `create_and_stamp_lpar` performs the create, its follow-ups (profile apply on the `mksyscfg`
  path, physical I/O) and the stamp in one call; physical I/O refuses to apply unless the stamp
  was confirmed (`operations/lpar/core.py`).
- The plan contract already gives provision `storage.capacity_mib` for a new disk and a digest
  that hashes `exclusive_writer_window` as `false` without `install` (ADR 0198, plan spec).

## Decision

1. **Inputs.** `request_id` is required, on the MCP tool and as the CLI's `--request-id`, with
   no compatibility path. Both add `continuation` (`none` / `resume` / `abandon`); the MCP tool
   adds `wait_seconds` and `expected_plan_digest`; `storage` adds `capacity_mib`. A
   continuation needs only `request_id`; any other value it gives must match the record.
2. **Result.** `ProvisionResult` keeps its fields and adds `operation`, the operation record.
   The top-level fields are the body's result when the outcome is `completed`, or
   `needs_attention` with steps that agree with the journal; otherwise they are rebuilt from
   the effects (applied → `ok` step, any other → `error` step, `workflow_completed` false),
   because a failed or reconciled run keeps an older result. A clean run ends `completed`;
   `configured` stays reserved for #1840's profile readback.
3. **Effects, in order** (keys, kinds, targets and identities are persisted):

   | Key | Kind | Target | Identity | Classifier (H1 *Resume*) |
   | --- | --- | --- | --- | --- |
   | `create` | `lpar.create` | `system:<uuid>` | `{lpar_uuid, name}` | one partition of that name, not ledgered by another operation, not foreign-stamped, no physical I/O requested → applied; none → not applied; else attention |
   | `stamp` | `lpar.ownership_stamp` | `lpar:<uuid>` | `{owner}` | description owner and caller token: this agent → applied; none → not applied; other → attention |
   | `affinity_policy` | `lpar.minimum_affinity_policy` | `lpar:<uuid>` | `{}` | none |
   | `network` | `lpar.client_network_adapter` | `lpar:<uuid>` | `{adapter_uuid, mac}` | adapters on the VLAN: one → applied; none → not applied; several → attention |
   | `disk` | `vios.virtual_disk` | `vios:<uuid>` | `{vios_uuid, vg_uuid, disk_name, capacity_mib}` | disk in the group, not ledgered by another operation → applied; absent → not applied; else attention |
   | `storage` | `vios.storage_map` | `lpar:<uuid>` | `{vios_uuid, storage_name}` | mapping to this partition → applied, else not applied |
   | `assignments` | `lpar.pcie_assignments` | `lpar:<uuid>` | `{}` | none |
   | `power_on` | `lpar.power_on` | `lpar:<uuid>` | `{job_id}` | activated → applied, else attention (ADR 0199) |

   The MAC is lower-case colon form. An applied classification returns the write's identity
   shape, read live; a classified `create` adds `resumed: true`, and the body then warns that
   a `mksyscfg` profile apply is unconfirmed.
4. **The stamp is required.** The `stamp` effect confirms the stamp the create wrote and
   writes it only when absent. A stamp still absent after that write is `EffectNotApplied`, so
   the run ends `failed`. A create whose own follow-up step reports an error also ends `failed`.
   A create the HMC refused, proven by a name lookup that finds no partition, ends `failed`.
5. **Later legs pause.** A write whose step reports an error (an assignment step error, a
   PowerOn job error or timeout) raises, so it settles `uncertain` rather than `applied`. Any
   error after `stamp` stops the remaining steps and ends `needs_attention`; `resume`
   classifies and continues.
6. **Ledger vocabulary**, keyed by the operation and key, for #1846–#1848 to consume:
   `partition` (key `partition`, identity `{lpar_uuid, name}`); `virtual_disk` (key
   `root_disk` here, identity `{vios_uuid, vg_uuid, disk_name}`); and `installer_media`,
   reserved for #1842 with identity `{vios_uuid, media_name}`. Only a disk this operation
   created is written; mapped existing storage is never owned.
7. **Plan digest.** When `expected_plan_digest` is given and no operation is recorded for the
   agent and `request_id`, the handler runs `plan_lpar` with the plan's own delegated reads and
   refuses, before anything is recorded, unless the fresh `plan_digest` equals it. A repeat
   skips the check; the request digest already binds its arguments. `physical_io`, which the
   plan does not cover, refuses the combination.
8. **Delegated authority.** Before anything is recorded, the handler requires the policy to
   permit and the dispatch authorizer to admit, for the resolved selectors: `hmc_create_lpar`,
   `hmc_add_network_adapter` and `hmc_map_storage_to_lpar` always; `hmc_create_virtual_disk`
   with `capacity_mib`; `hmc_set_minimum_affinity_policy` with a policy;
   `hmc_assign_sriov_logical_port` with `assignments.sriov`; `hmc_add_vnic` with
   `assignments.vnics`; `hmc_power_on_lpar` with `power_on`. The `power_on` write keeps
   `ownership_override=True` against the stamp this operation just confirmed (ADR 0092).
9. **Preconditions refuse before recording.** For a new request the handler runs the
   preconditions, as `dry_run` does, and raises their error to the caller; the body re-runs
   them only on a continuation whose `create` is not applied, ending `failed` on a refusal.
   `dry_run` validates `request_id` and stops there: no operation, lock or guard.
   `affinity_assessment` without `power_on` is refused, since it waits for activation.

## Consequences

- Callers must send `request_id`; a repeat returns the same operation and never duplicates a
  partition. The step list gains `stamp`, and `create`'s result is the UUID and name only.
- Any error in `affinity_policy` or `assignments` needs a human: abandon, then use the
  specialist.
- The CLI is refused while another process holds the ADR 0190 execution lock, such as a
  server that has run a logical mutation.
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
- **Use `stamp_policy="required"`.** verified: it raises after the partition exists
  (`core.py` `create_and_stamp_lpar` docstring, main `ddf3429a`), so the create effect would
  settle `uncertain` and pause instead of ending `failed`.
- **Require `exclusive_writer_window` for disk creation now.** judgment: fit. The plan digest
  hashes it `false` without `install`, so the flag would make plans unmatchable before #1840.
- **Delegate `hmc_add_vscsi_adapter`.** verified: provision adds no vSCSI adapter; the mapping
  creates the pair (ADR 0169, #1030).
