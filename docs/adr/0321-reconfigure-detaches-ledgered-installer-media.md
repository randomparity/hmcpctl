# 0321 — Reconfigure detaches only ledgered installer media and boots the ledgered root disk

## Status

Accepted (2026-11-03), issue #1844 (piece 1 of #1228). This record extends ADR 0314's
`hmc_reconfigure_lpar` with the two patch fields that ADR 0191 Decision 7 assigns to reconfigure.
It consumes the `installer_media` ledger entry that ADR 0319 (#1842) has provision write.

## Context

- After `boot_started`, the media stays mounted (ADR 0191 D7). The activation consumed the pending
  boot string that provision set (ADR 0319 D3), so the next activation boots from whatever the
  string holds when it starts.
- Provision ledgers a `partition`, a `virtual_disk` and an `installer_media` entry under its own
  operation id (`provision_operation.py`; ADR 0319). No other record links a partition to the media
  hmcpctl mounted on it.
- `unmount_optical_media` removes the whole optical mapping by `MediaName`, through the VIOS
  read-modify-write (ADR 0079). `unloadopt` empties the device but leaves the mapping (ADR 0317). A
  mapping left with no medium can no longer be unmounted by name.
- The #1844 Q5 capture (`tests/fixtures/live/rest-vios-optical-unmount-running-no-rmc.json`) ran a
  mapping removal on a running partition without RMC, on V10R3. The VIOS mapping and its server
  adapter were removed. The POST then answered HTTP 500 `REST0126` `[HSCL2957 Either there is
  currently no RMC connection between the management console and the partition 1 or the partition
  does not support dynamic partitioning operations. …]`. The partition's client vSCSI adapter
  stayed in its current configuration, so a second unmount after power-off found no mapping.

## Decision

1. **The ledger is the only owner source.** The fields act only on the `installer_media` and
   `virtual_disk` entries that were recorded by the same operation as this partition's `partition`
   entry. Any other case is refused before intent: no such entry, more than one, or no provision
   operation.
2. **Detach removes the mapping.** It uses `unmount_optical_media`; the medium stays in the
   repository. When the media is already unmapped, nothing is written. When the unmount is
   refused, one listing classifies the write inline. If the media is still mapped, the effect is
   `not_applied`. The message names the HTTP status, not a cause, and offers retry or power-off.
3. **Boot names the root disk only.** The pending boot string becomes the disk mapping's device
   path. The change reports `applied: next_activation`. The ADR 0314 sync refusal counts it as a
   current-configuration write.
4. **Order and basis.** The writes run after `rename` in this order: `boot` (`lpar.boot_order`),
   then `media_detach` (`vios.optical_unmount`). Each write re-runs the ADR 0011 check. Neither
   write compares the partition-view basis, because an installer changes state and RMC by itself.
   `set_lpar_boot_order` verifies its own write. A detach on an activated partition is the caller's
   assertion that the install has finished, and the result warns about it.
5. **Classifiers.** `lpar.boot_order` classifies by the pending boot string: after → `applied`,
   before → `not_applied`, anything else → `needs_attention`. `vios.optical_unmount` classifies by
   the media's mappings: none → `applied`, one → `not_applied`, more → `needs_attention`.
6. **Authority.** Detach delegates to `hmc_unmount_optical_media` and requires
   `exclusive_writer_window=true` (H1 spec, VIOS writes). Boot delegates to
   `hmc_set_lpar_boot_order` and does not require the window. The handler authorizes both tools
   with the `lpar` and `managed_system` selectors only. The VIOS is not known until the ledger is
   read, and that read needs the UUIDs that planning resolves. The `vios` selector of
   `hmc_unmount_optical_media` is therefore absent. A grant that limits that tool by target refuses
   detach, which fails closed. Only a grant of the tool for all targets admits it.
7. **Detach only when the HMC can finish it.** Detach is refused unless the partition is
   `not activated` or its RMC is `active`, because otherwise the HMC does the half-done removal
   captured above. The check runs at planning and again before the write. The refusal names the
   state and the fix: power the partition off, then detach (operator decision, 2026-11-03).

## Consequences

- A partition that hmcpctl did not provision cannot use either field. The specialists remain
  available for it.
- Right after `boot_started`, an installer runs without RMC, so detach requires a power-off
  first. A partition whose installed OS has RMC up can detach while running. No guest holding the
  device has been captured.
- If the HMC refuses the detach anyway, the caller retries or powers the partition off.
- Detach requires an all-targets grant of `hmc_unmount_optical_media`.
- The saved profile keeps the optical client adapter (ADR 0319 D2). Adapters stay as the HMC
  leaves them after the unmount.
- Before #1842 merges, no ledger entry of kind `installer_media` exists, so detach always refuses.
- The boot write governs the next activation only, profile activation included. The pending boot
  string is partition state, not profile content (ADR 0319 D3). A profile activation was observed to
  consume it on V10R3 (`operations/lpar/boot_order.py`, `_CLEAR_REFUSAL`). Two things are
  unevidenced: whether the firmware keeps the root disk as its default for later activations, and
  whether the activation boots from the string, which has had no native run yet.

## Considered & rejected

- **Do nothing; keep the specialists.** judgment: fit. ADR 0191 D7 and the H1 spec assign both
  actions to reconfigure.
- **Let the caller name the medium.** judgment: fit. Issue #1844 limits detach to the media
  provision mounted, and a caller-named medium could detach media hmcpctl does not own.
- **Match media by the `hmcpctl_` name prefix.** verified: `media_name_for` names every
  operation's media `hmcpctl_<12 hex>.iso` (#1842 `install_media.py`), so the prefix cannot tell
  this partition's media apart from another's.
- **Detach with `unloadopt`.** verified: ADR 0317 Context records that a mapping with no medium
  has no `MediaName`, so it can no longer be unmounted by name. An unload therefore leaves an empty
  mapping that nothing owns.
- **Authorize the ledger VIOS in the handler.** judgment: complexity. The handler would have to
  resolve the selectors through the HMC before submit, which duplicates the body's resolution.
- **Keep the installer second in the boot string.** judgment: fit. ADR 0191 D6 relies on the
  producer to refuse reinstalls, and the issue asks for a partition that boots from its root disk.
- **Detach on a running partition and warn about the client adapter.** verified: the Q5 capture
  above. That detach removes the VIOS side and leaves a client adapter, which the
  `vmedia-media-change` arm reported for manual removal; the operator chose to refuse instead.
- **Compare the partition-view basis before these writes too.** judgment: fit. An installer that
  halts or reboots changes state and RMC, and that would fail every detach as stale.
- **Require the window for boot as well.** judgment: fit. The boot write touches no VIOS and no
  profile, the same as a rename.
