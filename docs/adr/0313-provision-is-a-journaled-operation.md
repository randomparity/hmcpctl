# 0313 — LPAR provision is one journaled operation that persists through a saved profile

## Status

> **Partially superseded by [0319](0319-provision-mounts-installer-media-before-the-profile-save.md)** (2026-11-03)

Accepted (2026-11-02), issues #1839 and #1840 (pieces 1 and 2 of #1225, one PR by operator
decision). Implements the H1 spec's `hmc_provision_lpar` row, without `install`, under ADR 0189
Decision 2 and ADRs 0190, 0195, 0236 and 0242, with ADR 0199 as the wiring precedent and ADR
0314's `lpar.profile_write` kind reused. ADR 0005's step workflow stands as the order of
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
- A live capture on 2026-11-03 (V10R3 M1060, POWER9, one scratch partition, private evidence):
  with `CurrentProfileSync` `Disabled`, a REST client-adapter add reaches the current
  configuration only; a profile activation then starts without it. SaveCurrentConfig into a
  new profile name holds the adapter, memory and processors, though #637's helper reads its job
  as unknown. Force-save over the existing profile fails at its recovery copy (HTTP 500).

## Decision

1. **Inputs.** `request_id` is required, on the MCP tool and as the CLI's `--request-id`, with
   no compatibility path. Both add `continuation` (`none` / `resume` / `abandon`); the MCP tool
   adds `wait_seconds` and `expected_plan_digest`; `storage` adds `capacity_mib`. A
   continuation needs only `request_id`; any other value it gives must match the record.
2. **Result.** `ProvisionResult` keeps its fields and adds `operation`, the operation record.
   The top-level fields are the body's result when the outcome is `configured`, or
   `needs_attention` with steps that agree with the journal; otherwise they are rebuilt from
   the effects (applied → `ok` step, any other → `error` step, `workflow_completed` false;
   while the operation is still running, a write in flight is left out), because a failed or
   reconciled run keeps an older result. A clean run ends `configured`, and only after the
   profile readback (Decision 10).
3. **Effects, in order** (keys, kinds, targets and identities are persisted):

   | Key | Kind | Target | Identity | Classifier (H1 *Resume*) |
   | --- | --- | --- | --- | --- |
   | `create` | `lpar.create` | `system:<uuid>` | `{lpar_uuid, name, follow_ups}` | one partition of that name, not ledgered by another operation, not foreign-stamped, no physical I/O requested → applied; none → not applied; else attention |
   | `stamp` | `lpar.ownership_stamp` | `lpar:<uuid>` | `{owner}` | description owner and caller token: this agent → applied; none → not applied; other → attention |
   | `affinity_policy` | `lpar.minimum_affinity_policy` | `lpar:<uuid>` | `{}` | none |
   | `network` | `lpar.client_network_adapter` | `lpar:<uuid>` | `{adapter_uuid, mac}` | adapters on the VLAN: one → applied; none → not applied; several → attention |
   | `disk` | `vios.virtual_disk` | `vios:<uuid>` | `{vios_uuid, vg_uuid, disk_name, capacity_mib}` | disk in the group, not ledgered by another operation → applied; absent → not applied; else attention |
   | `storage` | `vios.storage_map` | `lpar:<uuid>` | `{vios_uuid, storage_name}` | mapping to this partition → applied, else not applied |
   | `assignments` | `lpar.pcie_assignments` | `lpar:<uuid>` | `{steps}` | none |
   | `profile` | `lpar.profile_write` | `lpar:<uuid>/profile-name:<name>` | `{profile_uuid, profile_name}` | profile of that name: absent → not applied; memory, processors and units as requested → applied; else attention (ADR 0314, by name) |
   | `default_profile` | `lpar.default_profile` | `lpar:<uuid>` | `{profile_name}` | partition's `DefaultProfileName` is the saved name → applied, else not applied |
   | `power_on` | `lpar.power_on` | `lpar:<uuid>` | `{job_id}` | activated with `LastActivatedProfile` the saved name → applied; anything else → attention (ADR 0199) |

   `follow_ups` lists the create's own profile-apply and physical-I/O steps as
   `[step, status, text]`; `steps` lists each assignment step as `[step, status]`. The MAC is
   lower-case colon form. An applied classification returns the write's identity
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
   `assignments.vnics`; `hmc_save_current_partition_profile` and
   `hmc_set_default_partition_profile` always; `hmc_power_on_lpar` with `power_on`. Because the
   save delegates to a `destructive` specialist, provision registers as `destructive`
   (ADR 0189 Decision 2). The `power_on` write keeps
   `ownership_override=True` against the stamp this operation just confirmed (ADR 0092).
9. **Preconditions refuse before recording.** For a new request the handler runs the
   preconditions, as `dry_run` does, and raises their error to the caller; the body re-runs
   them only on a continuation whose `create` is not applied, ending `failed` on a refusal.
   `dry_run` validates `request_id` and stops there: no operation, lock or guard.
   `affinity_assessment` without `power_on` is refused, since it waits for activation.
10. **Persistence (#1840).** After the current-configuration writes, provision saves the
    current configuration into a NEW profile `hmcpctl_<first 12 hex digits of operation_id>`
    through #637's `save_current_partition_profile`, never force-saving. It then reads that
    profile back by name and requires:
    - the requested desired memory, processors and, for a shared partition, processing units;
    - a client network adapter on the requested VLAN, and with storage a vSCSI client adapter;
    - at least one profile vNIC per requested vNIC, and one SR-IOV logical port per requested
      direct port.

    Physical I/O is verified by the create's own profile comparison and is not re-checked.
    Next the profile becomes the default through `set_default_partition_profile`; with
    `power_on`, provision then activates from it. A save the helper reports as `unknown`
    counts when the by-name readback passes (operator decision D1, 2026-11-03). A readback
    that fails ends `needs_attention`. The default-profile write raises unless the helper
    reports `completed` and `verified`, so an uncertain write is decided by its classifier.
    The power-on write raises when the partition was already active, so its classifier
    decides. Both #637 writes need HMC 10.3.1060 or later, which the preconditions check
    before anything is recorded. `lpar.default_profile` records the partition's
    `DefaultProfileName`, not a profile's content, so it is not a second profile-write kind.
    #637's job-read and force-save defects are #1867 and #1868.
11. **The writer window is required.** `exclusive_writer_window=true` is a required caller input
    for every provision that records an operation (operator decision, 2026-11-02, as #637's
    writes require it, ADR 0242). Without it the call is refused before anything is recorded.
    A `resume` re-asserts it, because its writes may run long after the first call. `abandon`
    writes nothing and does not need it.
12. **Name-addressed profile writes.** ADR 0314's `lpar.profile_write` target gains the form
    `lpar:<uuid>/profile-name:<name>` for a profile the write creates. Its classifier reads the
    profile by name, and an absent profile is `not_applied`. The UUID form is unchanged
    (operator decision D2, 2026-11-03).
13. **Boundaries kept open.** `install` (#1227, #1842/#1843) adds steps 4–7 and
    `ready_to_boot` after Decision 10's readback. Its gate that suppresses early power-on
    lives in the body after the readback, where it skips the `power_on` effect. #1230 (#1849/#1850) owns steps 8–9, `continuation: boot` and `boot_started`.
    The ledger vocabulary of Decision 6 stands, and #1229 consumes it.

## Consequences

- Callers must send `request_id`; a repeat returns the same operation and never duplicates a
  partition. The step list gains `stamp`, and `create`'s result is the UUID and name only.
- Any error in `affinity_policy` or `assignments` needs a human: abandon, then use the
  specialist.
- The CLI is refused while another process holds the ADR 0190 execution lock, such as a
  server that has run a logical mutation.
- Every provision needs `exclusive_writer_window=true` and a policy permitting the profile
  specialists. Provision is now `destructive`, so an effect grant must list `destructive`, or
  name the tool, to admit it; a `mutate`-only grant no longer does.
- Each provision leaves a second saved profile (`hmcpctl_…`) as the default; the profile the
  create wrote is kept, unchanged, and is not activated again.
- A resumed `profile` write classified `applied` from memory, processors and units alone is
  still re-checked by the body's full readback, adapters included, before `configured`.
- An HMC older than 10.3.1060 cannot provision, since #637's profile jobs need that level.
- Effect keys, kinds and ledger kinds are a persisted contract (ADR 0195).

## Considered & rejected

- **Do nothing.** judgment: fit. #1839 and the H1 spec require the journaled tool.
- **Return the bare operation record, as power does.** judgment: fit. The H1 spec keeps
  `ProvisionResult`'s fields, and the live arms read them; a dry run has no record to return.
- **Report `completed` until a later piece adds persistence.** judgment: fit. The operator
  declined an interim outcome (2026-11-02).
- **Force-save over the create's own profile.** verified: the 2026-11-03 capture's force-save
  failed at #637's recovery copy with HTTP 500 and left the profile unchanged (#1868).
- **Typed profile updates (`update_partition_profile`).** verified: `ProfilePatch` carries no
  adapter fields (`documents/partition_profile.py` `ProfilePatch`, main `e117ca52`), so adapters
  would not persist.
- **Turn on `CurrentProfileSync`.** judgment: fit. It changes a partition setting the caller did
  not ask for, and nothing captured shows the HMC syncing a never-activated partition's adapters.
- **A second effect kind for the save.** judgment: fit. The operator required reusing ADR 0314's
  kind; a name-addressed target covers a profile that does not exist yet.
- **Ledger a mapped existing disk as the root disk.** judgment: fit. `delete_owned` (ADR 0192)
  would then delete storage the caller brought.
- **Split stamping out of `create_and_stamp_lpar`.** verified: physical I/O applies only after
  a confirmed stamp (`core.py` `created_io_step` guard, main `ddf3429a`), so a deferred stamp
  would break it.
- **Use `stamp_policy="required"`.** verified: it raises after the partition exists
  (`core.py` `create_and_stamp_lpar` docstring, main `ddf3429a`), so the create effect would
  settle `uncertain` and pause instead of ending `failed`.
- **Delegate `hmc_add_vscsi_adapter`.** verified: provision adds no vSCSI adapter; the mapping
  creates the pair (ADR 0169, #1030).
