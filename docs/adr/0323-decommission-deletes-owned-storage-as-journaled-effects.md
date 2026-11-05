# 0323 — Decommission deletes owned storage as journaled effects and removes its ledger entries

## Status

> **Decision 4 retained/partition-row lifetime superseded by [0341](0341-terminal-decommission-ledger-reclamation.md)** (2026-11-04); physical cleanup and other decisions remain unchanged.

Accepted (2026-11-03), issue #1848 (piece 2 of #1229). Implements ADR 0192 on the journaled
decommission of ADR 0320, whose effect keys, kinds and retain path stand. Consumes ADR 0313
Decision 6's ledger vocabulary: provision writes `virtual_disk` rows today, and rows #1846
(reconfigure's added disks) and #1842 (`installer_media`) write are read the same way once they
land.

## Context

- ADR 0192 decides what `delete_owned` may delete; it leaves open the effects, the ledger read
  and removal, the authorization of the specialists the H1 spec's decommission `delete_owned` row
  names, and the result shape.
- The ledger (`store.py`) is insert-only and keyed `(operation_id, key)`; rows name their
  partition, so a partition's owned items are the rows with its system and partition UUIDs,
  whichever operation wrote them.
- Teardown (ADR 0320) already detaches every vSCSI mapping to the partition, optical ones
  included, before deleting it. A volume-group write is a read-modify-write with `If-Match`
  (ADR 0171), which no live observation shows the HMC enforcing.
- `hmc_decommission_lpar` is registered by `@tool`, which gives a handler no access to the
  policy's `permits` and `authorize` gates; power, provision and reconfigure are built by
  factories to authorize their delegated specialists (ADR 0189 Decision 2).

## Decision

1. **Candidates.** Under `storage_cleanup=delete_owned`, the owned items are the ledger rows of
   kind `virtual_disk` or `installer_media` whose system and partition UUIDs are the guarded
   partition's, read once when cleanup starts. An item is its projected identity —
   `{vios_uuid, vg_uuid, disk_name}` or `{vios_uuid, media_name}`; extra identity fields are
   ignored. An identity also ledgered for another partition is retained (`ledgered_elsewhere`).
2. **Same-pass check.** One pass lists the system's VIOSes and reads each one's storage detail.
   Any unreadable VIOS, or any vSCSI mapping without a verified client UUID or with a `Storage`
   element naming no disk, media, volume or unit, retains every owned item
   (`inventory_incomplete`). Otherwise an item referenced by a mapping to another client is
   retained (`referenced`); one referenced only by mappings to this partition is deletable.
   A disk matches by `DiskName` on its VIOS and, when the mapping links a volume group, that
   group; media match by `MediaName` on its VIOS. The same pass reads the item's group: an item
   no longer listed is retained as `absent`, and a name listed more than once as `ambiguous`;
   both keep their rows.
3. **Effects**, after teardown and only when `delete_lpar` is applied, one item at a time, disks
   then media, each kind in ledger order; UUIDs lower-cased, 16 hex digits the start of the SHA-256 of the `|`-joined parts:

   | Key | Kind | Target | Identity | Classifier |
   | --- | --- | --- | --- | --- |
   | `unmap.<16 hex>` (ADR 0320) | `vios.storage_unmap` | `vios:<uuid>/mapping:<id>` | `{vios_uuid, mapping_id}` | ADR 0320's |
   | `disk.<16 hex of vios, vg, name>` | `vios.virtual_disk_delete` | `vios:<uuid>/vg:<uuid>/disk:<name>` | `{vios_uuid, vg_uuid, disk_name}` | volume group read: disk listed → not applied; absent → applied |
   | `media.<16 hex of vios, name>` | `vios.optical_media_delete` | `vios:<uuid>/vg:<uuid>/media:<name>` | `{vios_uuid, vg_uuid, media_name}` | repository read: media listed → not applied; absent → applied |

   An unmap runs first for each remaining mapping to this partition that references the item;
   an optical mount is such a mapping, so this unmap is also the unmount.
   The media's volume group is the VIOS's one repository-bearing group; none or several retains
   the media (`repository_unresolved`). A write that fails settles `uncertain` and pauses the run.
   An HTTP 412 settles the delete `not_applied` and is not retried: the item is retained
   (`precondition_failed`), the remaining items are still processed, and the run then ends
   `failed` with a warning naming the item and the specialist that removes it.
4. **Ledger removal.** Once an item's delete effect is `applied`, every ledger row naming it for
   this partition is deleted in one transaction. Retained items and the `partition` row keep
   their rows: this implements ADR 0190 Decision 7's removal for deleted resources only, and its
   removal of retained ones is left open.
5. **Inputs and authority.** `storage_cleanup` (`retain` default / `delete_owned`) is recorded with
   the request; `exclusive_writer_window=true` is required with `delete_owned` on a new request
   or `resume` and refused before anything is recorded, naming the flag. The mode is read from the
   effective arguments — the record's on a continuation, `retain` when a record predates the
   field — so a bare `resume` of a `delete_owned` operation needs the flag too. The tool moves to
   a factory with its `lpar`, exhaustive security unchanged. Before recording, a `delete_owned`
   new request or `resume` requires the policy to permit `hmc_detach_storage_mapping`,
   `hmc_unmount_optical_media`, `hmc_delete_optical_media` and `hmc_delete_virtual_disk`, and
   authorizes each against every owned item's recorded VIOS UUID with the recorded system and
   partition selectors; any refusal refuses the call. The body is given that VIOS set and retains
   an item on any other VIOS (`not_authorized`), which only a row written after the check reaches.
6. **Outcome and result.** Phase `cleaning_storage`. `DecommissionResult` gains `storage`, null
   under `retain`, else `{deleted, retained, pending}`. `deleted` is always read from the
   operation's applied delete effects, so it spans every run; `retained` (with reasons) and
   `pending` (owned items not yet deleted) come from the latest pass. A run retaining an item for `inventory_incomplete`
   ends `needs_attention`, so `resume` re-reads; other retentions end `completed`. A dry run
   reports deletable items as `pending` and the rest as `retained`, records nothing, and needs
   neither the flag nor the specialists; without the flag it warns that the run will need it.
   Cleanup deletes are reported only in `storage`, never as teardown steps. A result rebuilt from the journal takes `storage` from
   the record when present, else from the delete effects (applied → `deleted`, open → `pending`).

## Consequences

- A store loss leaves nothing owned, so `delete_owned` deletes nothing (ADR 0192 D6).
- A retained item after the partition is gone keeps its ledger row with nothing that can claim it;
  it counts against the store capacity until an operator removes the storage and the store.
- `delete_owned` needs a policy granting the four specialists; a grant of
  `hmc_decommission_lpar` alone keeps `retain` working.
- After a 412 the partition is usually gone and the run is terminal, so the retained item has no
  tool path left: an operator removes it with `hmc_delete_virtual_disk` or
  `hmc_delete_optical_media`, and its row stays.
- Ownership is by name (ADR 0313 D6). A ledgered disk deleted out of band whose name a later
  writer reuses in the same group would be deleted as owned.
- Effect keys and kinds are a persisted contract (ADR 0195).

## Considered & rejected

- **Do nothing; leave owned storage to the specialists.** judgment: fit. ADR 0192 rejected it.
- **Check references only on the owning VIOS.** judgment: fit. ADR 0192 D4 names any VIOS; the
  extra reads are the teardown inventory's.
- **Key deletes by the ledger row's `(operation_id, key)`.** judgment: fit. Two rows can name one
  disk; keying by the resource makes a repeat idempotent regardless of which row is read.
- **Authorize the specialists inside the body.** verified: a `@tool` handler receives only its
  arguments (`tool_registry.py` `_ToolCollector`, main `d2bb5c8b`), and a body runs after the
  operation is recorded, which ADR 0189 forbids for a denial.
- **Fingerprint the disk (capacity or UDID) before deleting.** judgment: fit. ADR 0313 D6's
  identity records neither, and widening a ledger contract three producers share is its own
  change.
- **Prune the ledger row before the delete.** judgment: fit. A crash between the two would lose
  the provenance of storage that still exists.
