# `hmc_reconfigure_lpar` installer-media detach and root-disk boot (#1844)

Decision record: [ADR 0321](../../adr/0321-reconfigure-detaches-ledgered-installer-media.md).
Contract: ADR 0191 Decision 7, ADR 0314 (the reconfigure operation it extends), the H1 spec
(`2026-10-01-logical-lpar-workflows-design.md`, *Patch and install inputs*, delegated-authorization
row), ADR 0319 (#1842: provision mounts the installer media and records the `installer_media` ledger
entry). Piece 1 of #1228's split.

## Problem

After `boot_started` the installer media stays mounted, and the pending boot order still names it
first (ADR 0191 D6–D7). Detaching the media and booting from the root disk are reconfigure actions.
Nothing implements them, so an agent must use two specialists and work out which mapping and disk
hmcpctl owns.

## Scope

**Patch** gains two optional fields. `ReconfigurePatch` still needs at least one field.

- `installer_media: "detach"` removes the optical mapping that holds the installer media this
  partition's provision recorded. The medium stays in the repository; deleting it belongs to #1229.
- `boot: "root_disk"` sets the pending boot string to the device path of the root disk this
  partition's provision recorded. That root disk is the only entry.

**Ownership source** (ADR 0321 D1). The *provision operation* is the one operation whose `partition`
ledger entry names this partition. Its `installer_media` entry gives `{vios_uuid, media_name}` for
detach. Its `virtual_disk` entry gives `{vios_uuid, vg_uuid, disk_name}` for boot. Planning refuses
the patch before any write in these cases: no store, no provision operation or more than one, no
entry of the needed kind, more than one entry, or an entry missing its fields.

**Plan** (recorded in the existing `plan` effect, so a replay never re-reads the ledger):

- `media`. First, list the partition's optical mappings on the media's VIOS. When none holds
  `media_name`, the field is unchanged: no change entry and no write. When exactly one holds it,
  the plan records `{vios_uuid, media_name}` and change `{field: installer_media, before:
  {current: "mounted"}, after: "detached", applied: "live"}`. When more than one holds it, the
  patch is refused.
- `boot`. The root disk's device path is found by listing the disk's VIOS storage mappings. The
  mapping must be a `VirtualDisk` named `disk_name` whose client is this partition, and there must
  be exactly one. Its path comes from `device_path` (#1842's `install_media.py`). If none is found
  or the path is unreadable, the patch is refused. The plan then reads the pending boot string.
  When it already equals the path, the field is unchanged. Otherwise the plan records `{before,
  after}` and change `{field: boot, applied: "next_activation"}`.
- The ADR 0314 sync refusal counts the boot write as a current-configuration write.

**Writes**, after `rename`, in this order (ADR 0321 D4):

- `boot` (`lpar.boot_order`, target `lpar:<uuid>`) calls `set_lpar_boot_order`.
- `media_detach` (`vios.optical_unmount`, target `lpar:<uuid>`) calls `unmount_optical_media`.

Before each write, the body re-resolves the partition and runs the ADR 0011 check, as for every
write. It does not compare the partition-view basis, because an installer changes state and RMC on
its own. `boot` instead re-derives the root-disk path and fails stale if it differs from the plan's.
Refusal mapping:

- `ValueError`, `LookupError` or `PermissionError` before the POST → `EffectNotApplied`. So is
  `LparUpdateError(submission="rejected")`.
- An `HMCError` from the unmount is followed by one mapping listing. Media still mapped →
  `EffectNotApplied`, with a message that names a possible client reservation and says to power
  the partition off and retry. Media gone → applied. Listing unreadable → the original error
  propagates (uncertain).

**Classifiers.** `lpar.boot_order` reads the pending boot string: the planned `after` → applied,
the planned `before` → not_applied, anything else → needs_attention. `vios.optical_unmount` lists
the mappings: none holds the media → applied, one does → not_applied, more than one →
needs_attention.

**Verification.** ADR 0314 Decision 7's poll also waits for the pending boot string to equal
`after` and for no mapping to hold the media. `observed` gains `boot` and `installer_media`.

**Handler.** `DELEGATED` gains `installer_media → hmc_unmount_optical_media` and `boot →
hmc_set_lpar_boot_order`. `installer_media` requires `exclusive_writer_window=true`, because the
VIOS read-modify-write is under the H1 spec's window rule. `boot` does not, as a rename does not.

**Live capture.** The issue asks for live evidence first. The `vmedia-media-change` arm (ST71)
gains Q5. It powers the test partition on with the medium mapped, unmounts through
`hmc_unmount_optical_media` while it runs, and records the answer. If that unmount is refused, Q5
powers the partition off immediately and unmounts again. Q5 runs under a LIVE-GRANT only.

## Failure model

1. **Actors and deployments**
   - an MCP agent using the served tool under the access policy;
   - one hmcpctl process holding the execution lock (ADR 0190);
   - the operator running ST71 under a grant.
2. **Invariants and assets at stake**
   - only the ledger-recorded media mapping of this partition is removed. The medium, the root disk
     and other mappings are untouched;
   - the boot string names only the ledger-recorded root disk of this partition;
   - no write happens before the plan is recorded;
   - effect keys and kinds are a persisted contract (ADR 0195).
3. **Accepted failure classes**
   - a guest's reservation refuses the unmount. The effect is `not_applied` and the message names
     the recovery. hmcpctl never forces `-release` (ADR 0317 D3 keeps that a separate audited tool);
   - the saved profile keeps the optical client vSCSI adapter (ADR 0319 D2). Adapters stay as the
     HMC leaves them after the unmount. Removing them is not a detach;
   - a partition that no provision recorded cannot use either field. It has no ledger owner.
4. **Covered elsewhere**
   - provision writing the `installer_media` ledger entry → #1842 / ADR 0319;
   - media and disk deletion → #1229 / #1848;
   - network and added disks → #1845 / #1846.

### Threat model

- **Boundary widened:** two literal patch values reach a VIOS mapping write and a partition write.
  The identities written come from the ledger and the HMC, never from the request.
- **Actor:** an authenticated agent under the access policy. The HMC and the local store are
  trusted.
- **Controls:** a delegated permit and authorize step for each tool, before anything is recorded;
  the ADR 0011 check before every write; the `exclusive_writer_window` rule for the VIOS write;
  `Literal` types. Messages name UUIDs, never media names from the request.
- **Out of scope:** a tampered local store. Its trust is the existing ledger's.

## Success

1. `installer_media: detach` removes only the planned media mapping. Media that is already detached
   plans no write. A missing ledger entry, a duplicate ledger entry or a duplicate mapping is
   refused before any intent. (criterion 1)
2. `boot: root_disk` writes exactly the root-disk path. A boot string that already holds it plans
   no write. A missing entry or mapping is refused. A path that changes after planning fails stale.
   (criterion 2)
3. Each classifier gives its three answers. An uncertain write resumes by classification. A
   refused unmount with the media still mapped is `not_applied`. (criterion 3)
4. A patch with detach and no window, or with a denied delegated tool, is refused before
   recording. (criterion 4)
5. Docs regenerate, and `maturity.json` records the variant with no native evidence.
   (criterion 5)
6. ST71 Q5 is tested without hardware and runs once under a grant: preflight → arm → recovery →
   evidence. (criterion 6)

## Validation

New `tests/lpar/test_reconfigure_media.py` (ledger lookup, plan, writes, classifiers, body on a
fake HMC with seeded ledger); handler gates in `tests/app/test_lpar_reconfigure_tool.py`; Q5 in
`tests/scripts/test_live_vmedia_media_change.py`; then `just verify` and
`uv run --no-sync prek run --all-files`.
