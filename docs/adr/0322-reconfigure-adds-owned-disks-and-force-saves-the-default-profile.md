# 0322 — Reconfigure adds owned disks and persists them by force-saving the default profile

## Status

Accepted (2026-11-03), issue #1846 (piece 3 of #1228). Extends ADR 0314: new patch field,
effects and delegated tools. ADR 0314's Decisions 2, 3 and 7 stand. The operator chose
the persistence path and the guards on 2026-11-03.

## Context

- A mapping makes the HMC create a client/server vSCSI adapter pair (ADR 0169). The client
  adapter lands in the current configuration, and a profile activation drops an adapter the
  profile lacks (`operations/lpar/profile_sync.py`; ADR 0313's 2026-11-03 capture).
- `ProfilePatch` carries no adapter fields, so ADR 0314's typed profile write cannot persist
  one (ADR 0313, Considered & rejected).
- After #1870, `save_current_partition_profile(overwrite=True)` force-saves over an existing
  profile once a retained `hmcprec-` recovery copy verifies. An operator run showed an added
  client adapter in the overwritten `default_profile` (ADR 0242's dated note).
- ADR 0313 Decision 6 names ledger kind `virtual_disk`, with identity
  `{vios_uuid, vg_uuid, disk_name}`, for #1846–#1848.

## Decision

1. **Patch.** `disks.add[]` creates a disk in a named volume group and maps it.
   `disks.detach[]` removes one mapping of a named disk to this partition. Both are limited
   to 4 items, so the plan stays within the store's identity bound. An add naming an existing disk on its VIOS is refused, so reconfigure never
   resizes. A detach never deletes a disk or a ledger entry. On an activated partition a
   detach needs `allow_disruption=true`. An add on an activated partition needs it to be
   running with RMC active.
2. **Ownership.** Each created disk is ledgered under the reconfigure operation's id, with
   key `added_disk:<i>`, kind `virtual_disk` and the partition. A mapped existing disk is
   never ledgered.
3. **Persistence.** With any add, the last write force-saves the current configuration over
   `DefaultProfileName`, through #637's helper with the profile's fresh ETag. It runs after
   ADR 0314's verification poll completes. The helper's `completed` and `verified` decide it.
   Failing that, a by-name readback decides: the profile must lack no current client
   adapter. An unknown `execution` (#1867) counts only through that readback. Every outcome
   names the partition's `hmcprec-` profiles. The leg takes the caller's writer-window
   assertion as an argument. An add needs HMC 10.3.1060 or later and is refused before any
   write on an older console.
4. **Guard.** Because the save overwrites the whole profile, a plan with an add is refused
   before any write when:
   - it holds a `pending_activation` change, or
   - the default profile's desired memory, processors or processing units differ from the
     partition's.

   The guard is re-checked just before the save.
5. **Effects**, after ADR 0314's:
   - `unmap:<i>` (`vios.storage_unmap`);
   - `disk:<i>` (`vios.virtual_disk`), then `map:<i>` (`vios.storage_map`), as ADR 0313's
     kinds;
   - `profile_save` (`lpar.profile_save`, target `lpar:<uuid>/profile-name:<name>`).

   Each has a live classifier (spec table). The save leg and its classifier live in
   `operations/lpar/profile_persist.py`, for #1844 and #1845.
6. **Authority and window.** Disks need `exclusive_writer_window=true`. Adds delegate
   `hmc_create_virtual_disk`, `hmc_map_storage_to_lpar` and
   `hmc_save_current_partition_profile`; detaches delegate `hmc_detach_storage_mapping`.
   Each is authorized per VIOS.

## Consequences

- Each disk-adding run leaves one `hmcprec-<16 hex>` profile. A human deletes it once it is
  unneeded.
- Profile-only differences the guard does not read are overwritten. Their previous content
  is in the recovery copy.
- A partition holding pending profile values cannot take a disk until they are activated.
- A detached disk's client adapter stays in the profile with no backing.
- A run that pauses after the adds but before the save leaves the disks in the current
  configuration only, and its result says so. `abandon` there means the next profile
  activation drops their client adapters, while the ledger still owns the disks.
- A crash between an applied `disk:<i>` and its ledger row, followed by `abandon`, leaves an
  unowned disk named in the effect identity, as in provision.
- The new VIOS server adapter is not saved into the VIOS's own profile (unchanged from
  provision).
- Disks need HMC 10.3.1060 or later, as #637's save does.

## Considered & rejected

- **Save into a new `hmcpctl_` profile and make it the default (ADR 0313).** verified:
  ADR 0314 Decision 2 refuses a partition whose last-activated profile differs from its
  default. On an activated partition, every later reconfigure would refuse until a profile
  activation.
- **Write the adapter into the profile document.** judgment: fit. The adapter element is
  not live-proven, and ADR 0303 declined to extend #1724's profile vocabulary.
- **Leave added disks current-configuration only.** judgment: fit. It breaks ADR 0314's
  persistence promise, and the operator declined it (2026-11-03).
- **Order the save before ADR 0314's profile write so that pending values survive.**
  judgment: complexity. The save changes the profile ETag that the plan's profile write
  rests on. The operator chose refusal.
- **Ledger under the provision operation's id.** judgment: fit. #1844 finds the root disk as
  the `virtual_disk` entry of the operation that ledgered the partition. A second entry
  there would make that lookup ambiguous.
