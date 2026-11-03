# 0319 — Provision mounts installer media before saving the profile

## Status

Accepted (2026-11-03), issue #1842 (piece 1 of #1227). Partially supersedes ADR 0313 Decision 13. That
decision placed `install`'s steps 4–7 after the saved-profile readback; this record moves steps 4–6 ahead
of the save. The rest of ADR 0313 stands. The decision was taken by the operator on 2026-11-03 (D1).

## Context

- ADR 0313 saves the current configuration into a new profile `hmcpctl_<12 hex>`, reads it back, makes it
  the default, and activates from it. With `CurrentProfileSync` disabled, an adapter added through REST
  lives only in the current configuration. A profile activation starts without it (ADR 0313 Context,
  2026-11-03 capture).
- A mount creates a new vSCSI adapter pair for the optical mapping. `docs/recipes/lpar-iso-install.md`
  §4 records this on V10R3 M1060. The #1284 capture `rest-vios-scsi-mapping-replace-storage.json` shows the
  disk on client slot 2 and the optical medium on client slot 4.
- A mapping create on a partition with no applied current configuration is refused with REST0269. The
  refused create can leave a VIOS server adapter behind (recipe §4; `client/client_storage.py`). A REST
  create sets the current configuration (#1164), and the `mksyscfg` path applies the profile, so the
  mount runs before any profile write touches the partition.
- The installer power-on (#1849) activates the saved profile. If the optical adapter is added after the
  save, that profile does not carry it, and the firmware never sees the installer.

## Decision

1. With `install`, the order is:
   - create, stamp, the network and storage legs, and the assignment legs, as ADR 0313 already orders them;
   - then `bind_media`, the `upload` effect and the `mount` effect;
   - then the profile save, the readback and the default-profile write;
   - then the `boot_order` effect;
   - then the run ends `paused` with outcome `ready_to_boot` (ADR 0191 Decision 1).
2. With `install`, the readback also requires a `ProfileVirtualSCSIClientAdapter` whose `VirtualSlotNumber`
   is the optical mapping's client slot. That slot is recorded in the `mount` effect's identity.
3. The boot order is written after the default-profile write. `PendingBootString` is a partition setting
   that an activation consumes; it is not profile content.

## Consequences

- An upload runs before the profile is persisted. A failed or mismatched upload therefore leaves a stamped,
  unpersisted partition. The run ends `failed`, or `needs_attention` while an upload is uncertain. That is
  the same exposure ADR 0313 already accepts for its other legs.
- The readback rejects a profile that lacks the optical adapter, so `ready_to_boot` is never reported for a
  profile that would drop the media.
- Without `install`, the order and the readback are unchanged.

## Considered & rejected

- **Keep ADR 0313 D13's order (media after the readback).** verified: `docs/recipes/lpar-iso-install.md`
  §4–5 (main `35174c09`) says a mount creates its own adapter pair, and a profile activation drops adapters
  that live only in the current configuration.
- **Save a second profile after the mount.** judgment: two hmcpctl profiles per partition, and an
  extra #637 job on a path whose job read is already defective (#1867).
- **Force-save over the saved profile after the mount.** verified: ADR 0313 records that force-save fails
  at #637's recovery copy with HTTP 500 (#1868).
- **Apply the profile again before the mount.** judgment: cost. Both create paths already leave an
  applied current configuration (`operations/lpar/core.py` `_REST_CREATE_CONFIGURED`, main
  `35174c09`). A refusal ends the run `failed` with the server-adapter note, not a retry loop.
- **Do nothing (no install path).** judgment: fit. Issue #1842 and the H1 spec require it.
- **Turn on `CurrentProfileSync`.** judgment: fit. ADR 0313 already rejected changing a partition setting
  the caller did not ask for.
