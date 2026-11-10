# `hmc_reconfigure_lpar` added virtual disks design (#1846)

Decision record: [ADR 0322](../../adr/0322-reconfigure-adds-owned-disks-and-force-saves-the-default-profile.md).
Contract: the H1 spec (`2026-10-01-logical-lpar-workflows-design.md`), ADR 0314 (reconfigure),
ADR 0313 Decision 6 (ledger vocabulary), ADR 0169 (mapping writes), ADR 0192 (owned storage),
ADR 0242 (force-save with a verified recovery copy).

## Problem

Reconfigure cannot add a virtual disk to an existing partition, so #1848's `delete_owned`
cannot later delete it as owned. A mapping makes the HMC add a client vSCSI adapter to the
current configuration only, and a profile activation drops an adapter the profile lacks
(`operations/lpar/profiles/sync.py`). `ProfilePatch` carries no adapter fields (ADR 0313).

## Scope

**Patch.** `ReconfigurePatch.disks` (optional, `extra="forbid"`):

- `add[]` (0–4): `vios_uuid` and `vg_uuid` (canonical UUIDs), `name` (StrictStr, the VIOS
  backing-device limits of `validate_virtual_disk`), `capacity_mib` (StrictInt, a positive
  multiple of 1024).
- `detach[]` (0–4): `vios_uuid` and `name`.
- At least one item. No `(vios_uuid, name)` pair appears twice across both lists.
- `add` cannot share a patch with #1844's `boot` or `installer_media`: those legs skip the
  plan's state basis (ADR 0321 Decision 4), and the save after them re-checks it.

**Plan (phase `validating`, before any write).** A patch with `disks` always reads the default
profile under ADR 0314 Decision 2: a missing default, or a last-activated profile other than
the default, is refused.

- An add whose volume group is not on its VIOS is refused. So is an add whose `name` already
  names a virtual disk in any volume group on that VIOS. Nothing is ever resized.
- An add is refused when the console is older than HMC 10.3.1060, the level #637's save
  needs (provision's `_check_profile_jobs`).
- An add on an activated partition is refused unless the partition is `running` with RMC
  `active`. The client adapter is a DLPAR add (ADR 0314's live condition).
- A detach needs exactly one mapping on its VIOS that has a `VirtualDisk` backing called
  `name` and a client this partition. The plan records its `<server adapter>/<target device>`
  id; none, or several, is refused. On an activated partition (any state other than
  `not activated`), a detach needs `allow_disruption=true`.
- **Persistence guard** (operator decision, 2026-11-03). A plan with any add is refused when:
  - it holds a `pending_activation` change, or
  - the default profile's desired memory, processors or (shared) processing units differ from
    the partition's (current when activated, pending when not).
- Each add and detach joins `changes[]` as `field` `disks.add` or `disks.detach`, with the
  disk in `after` or `before`. `applied` is `profile` when not activated and `live`
  otherwise.

**Effects**, after ADR 0314's `profile`, `current` and `rename` (keys and kinds are
persisted, ADR 0195):

| Key | Kind | Target | Identity | Classifier |
| --- | --- | --- | --- | --- |
| `unmap:<i>` | `vios.storage_unmap` | `vios:<uuid>` | `{vios_uuid, mapping_id}` | the recorded mapping still backs the disk to this partition → not applied; gone → applied |
| `disk:<i>` | `vios.virtual_disk` | `vios:<uuid>` | `{vios_uuid, vg_uuid, disk_name, capacity_mib}` | as ADR 0313's `disk`: in the group and not ledgered by another operation → applied; absent → not applied; else attention |
| `map:<i>` | `vios.storage_map` | `lpar:<uuid>` | `{vios_uuid, storage_name}` | a mapping of that backing to this partition → applied, else not applied |
| `profile_save` | `lpar.profile_save` | `lpar:<uuid>/profile-name:<default>` | `{profile_name, recovery_profile}` | no current client adapter is missing from the named profile → applied; else not applied |

`profile_save` is the last effect this issue adds. A leg that a sibling issue adds after it
(#1844's `boot` and `media_detach`) runs after the save and owns its own persistence; the
save never runs again after it. The order is the detaches, then each add (`disk:<i>`, `map:<i>`), then ADR 0314's
verification poll, then `profile_save`. After `disk:<i>` applies, the body writes ledger
key `added_disk:<i>` with kind `virtual_disk`, the partition, and identity
`{vios_uuid, vg_uuid, disk_name}`. The insert is idempotent, under this reconfigure
operation's id (ADR 0313 Decision 6). A detach writes no ledger entry and deletes none.

**Persistence leg** (new `operations/lpar/profiles/persist.py`, reusable by #1844 and #1845).
`profile_save` runs only when the verification poll ends `completed` and the plan has an
add. It re-reads the partition and the default profile and re-applies the persistence
guard. It then calls `save_current_partition_profile(overwrite=True,
expected_etag=<fresh>, exclusive_writer_window=<the caller's assertion>)`. The leg takes the
window as a required argument; reconfigure passes the `true` its handler enforced. The
outcome is decided as follows:

- the helper reports `completed` and `verified` → applied;
- otherwise a by-name readback decides. No current client adapter missing from the profile
  (`adapters_missing_from_profile`) → applied. That is the only way `execution: unknown`
  (#1867) counts;
- something missing and no job submitted → `EffectNotApplied`;
- otherwise uncertain, settled by the classifier, which uses the same readback.

Every exit names the partition's `hmcprec-` profiles, read from the same profile feed:

- the applied result's warning says to delete them when unneeded;
- the `EffectNotApplied` and uncertain messages carry them into the operation's warnings.

When an add applied but `profile_save` did not, the result warns that the added disks live
in the current configuration only. A `resume` persists them; an `abandon` leaves them out
of the profile, so a profile activation drops their client adapters.

**Handler.** `needs_window` (`processors`, `memory_mib` or `disks`) requires
`exclusive_writer_window=true`. Delegated tools are authorized per distinct VIOS, with
`lpar`, `managed_system` and `vios` targets:

- adds: `hmc_create_virtual_disk`, `hmc_map_storage_to_lpar` and
  `hmc_save_current_partition_profile`;
- detaches: `hmc_detach_storage_mapping`.

## Failure model

1. **Actors and deployments**
   - an MCP agent through the served tool, under the access policy;
   - the one hmcpctl process holding the ADR 0190 execution lock;
   - the live arm on the operator's test boundary.
2. **Invariants and assets at stake**
   - no disk is resized, deleted or ledger-removed by reconfigure;
   - a detach removes only the one recorded mapping of the named disk to this partition;
   - a mapping of another partition is never removed (ADR 0169 read-modify-write);
   - only a disk this operation created is ledgered;
   - the force-save never runs over pending profile values (persistence guard);
   - `completed` with an add means the default profile holds every current client adapter;
   - effect keys, kinds and ledger kinds are a persisted contract.
3. **Accepted failure classes**
   - each disk-adding run leaves one `hmcprec-` profile (operator decision; ADR 0242);
   - the force-save copies the whole current configuration, so a profile-only difference the
     guard does not read (anything other than memory, processors and units) is overwritten.
     The guard compares desired values only, so pending minimum and maximum values are
     overwritten too. The previous content survives in the recovery copy (ADR 0242).
     Accepted because the operator scoped the guard to desired values;
   - the readback matches adapters by virtual slot number, so a stale profile entry in the
     new adapter's slot reads as saved. This is bounded: the helper's own
     `completed`/`verified` decides first, and the readback still proves the profile lists
     every current slot;
   - a crash after `disk:<i>` applies but before its ledger row, followed by `abandon`,
     leaves the disk unowned, as in provision. Its VIOS, volume group and name are in the
     effect identity, and a human deletes it with `hmc_delete_virtual_disk`;
   - a same-named disk another writer creates between planning and the write is classified
     applied and ledgered. Accepted under the writer window above;
   - the new VIOS server adapter is not saved into the VIOS's own profile. A VIOS activated
     from a profile without it loses the mapping's server side. Provision has the same
     exposure today; it is reported as a follow-up candidate, not fixed here;
   - other writers inside the window are not fenced: the caller's `exclusive_writer_window`
     assertion, the profile ETag and the pre-write re-reads are the controls (ADR 0242);
   - a detached disk's client adapter stays in the profile with no backing; activation keeps
     an empty adapter;
   - an interrupted save whose job lands later can classify `not_applied`, and resume saves
     again, leaving a second recovery copy.
4. **Covered elsewhere**
   - deleting owned disks → #1848 (ADR 0192);
   - installer media and boot → #1844;
   - network attachments → #1845;


### Threat model

- **Boundaries widened.** `hmc_reconfigure_lpar` arguments now reach VIOS volume-group
  writes, mapping writes and a profile force-save.
- **Actor.** An authenticated agent under the access policy; the HMC is trusted.
- **Controls.**
  - Strict typed patch: canonical UUIDs, `validate_virtual_disk`, bounded lists.
  - Delegated permit and authorize per tool and per VIOS (ADR 0189 Decision 2).
  - The body's ADR 0011 ownership check before every write.
  - A detach refuses any mapping whose client is not this partition.
  - The disk name passes XML escaping in the existing builders.
  - Warnings carry UUIDs and recovery names, never caller text.
- **Out of scope.** Independent HMC actors (accepted above).

## Success

1. An add on a not-activated partition creates, maps and ledgers the disk. It force-saves
   the default profile and ends `completed` with the change `profile`; a running partition
   reports `live`. (criteria 1, 4)
2. Each of these is refused before recording, or before the first write, with a message
   naming the cause:
   - an existing disk name, or a volume group missing from its VIOS;
   - a detach with no mapping, or more than one;
   - a detach on an activated partition without `allow_disruption`;
   - a pending-activation plan with an add;
   - an add on a console older than 10.3.1060, or on an activated partition that is not
     running with RMC active;
   - a patch with more than four adds or four detaches;
   - profile values that differ from the partition's, with an add;
   - disks without `exclusive_writer_window`;
   - an unpermitted delegated tool. (criteria 2, 3, 4, 5)
3. A detach removes only the recorded mapping; the disk, its ledger entry and the other
   mappings remain. (criterion 3)
4. Each new kind classifies applied and not-applied by its live read. Resume after an applied
   `disk:<i>` does not create it again, and still ledgers it. A save the helper reports
   unknown is applied only when the readback shows the adapters. (criteria 4, 6)
5. Tool docs regenerate. `maturity.json` lists the `added-disks` variant in `missing_scope`
   until the live run, then in `implemented_scope`. The CHANGELOG and the H1 spec's
   reconfigure row name the new tools. (criterion 7)
6. Live (ADR 0314's arm 70, extended): on its not-activated scratch partition, an add reads
   back `profile`, and the default profile lists a vSCSI client adapter. A profile
   activation then shows the client adapter in the running configuration. A detach after
   power off removes the mapping. Teardown deletes the disk and reports any VIOS server
   adapter the run left as a manual step, as the lpar-power arm does. Recovery reports a
   stranded `lprcf` disk or mapping, and a server adapter serving a scratch partition. The
   running-partition add has no native proof and stays in `missing_scope`. (criterion 7)

## Validation

Focused tests:

- `tests/unit/test_lpar_reconfigure.py`: plan, body, classifiers and ledger;
- a new `tests/unit/test_profile_persist.py`: the leg;
- `tests/app/test_lpar_reconfigure_tool.py`: the gates;
- the arm and recovery tests (`tests/test_live_lpar_reconfigure_arm.py`, and the recovery
  module's test).

The arm needs `scripts/live_test_recovery.py`'s subtask-70 check and its test module, a
necessary consequence of AGENTS.md's recovery-after-every-arm rule. Then run
`just static`, the four `just test-group` groups, `just smoke`, `just build`,
`just verify-artifacts` and `uv run --no-sync prek run --all-files`. Under a LIVE-GRANT run
preflight, then the arm, then recovery, then `scripts/live_test_evidence.py`.
