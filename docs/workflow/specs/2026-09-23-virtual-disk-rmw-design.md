# Spec: virtual-disk create and delete by VolumeGroup read-modify-write

**Issue:** #936 · **Branch:** fix/vdisk-rmw-936 · **BASE_BRANCH:** main ·
**Guardrails:** `just test`, `just lint`, `just typecheck`; `just verify` and
`uv run --no-sync prek run --all-files` before push.
**ADR:** [0171](../../adr/0171-virtual-disk-volume-group-read-modify-write.md)

## Problem

`create_virtual_disk` POSTs a sparse `VolumeGroup` holding only the new disk in a
`VirtualDisks kb="CUD"` collection; `delete_virtual_disk` POSTs a sparse document naming the
disk in a `VolumeGroupName` field. Both omit the group's existing disks and physical volumes.
On V10R3 (#879 window, 2026-09-23) both are rejected at schema validation (`kb` not allowed
on `VirtualDisk`; `DiskCapacity` out of order). Read-modify-write worked live: GET the group,
insert one `VirtualDisk` after the `VirtualDisks` `Metadata`, POST the whole element back with
`If-Match: <GET ETag>`; free space moved by exactly the requested size and the physical volume
was untouched.

## Design

The file already holds the read-modify-write pair the media-repository operations use:
`_get_vg_raw_xml` (GET, parse the `VolumeGroup` element) and `_post_vg_xml` (POST the element
with `Accept: */*` and `Content-Type: ...uom+xml; type=VolumeGroup`, reconciling a 5xx). The
live RMW used exactly those headers. Virtual-disk create and delete reuse the pair; nothing new
is added to `core.py`.

1. **`_get_vg_raw_xml` returns `(etag, element)`** instead of `(url, element)`. The URL is
   unused by every caller. It issues the GET through `_request_with_uuid_path_arguments` with
   the headers `_get` would send (`_uom_headers("VolumeGroup", include_schema_version=False)`),
   because `_get` returns only the body. Status handling is unchanged: a non-200/204 status
   raises `HMCError("GET <path> failed", status, body)`; a 204 or empty body raises the existing
   "returned empty body" error. `etag` is the response's `ETag` header or `None`.
2. **`_post_vg_xml` gains keyword-only `operation` and `etag`.** `operation` names the write in
   the possible-side-effect error (default `"update_virtual_media_repository"`, today's text);
   a non-empty `etag` is sent as `If-Match`. Media operations pass neither, so their requests
   are byte-identical to today. A 412 response raises `HMCError(..., 412)` saying the group
   changed since it was read, nothing was written, and re-running the operation is safe.
3. **Builder.** `build_virtual_disk_document` is replaced by `build_virtual_disk_element`,
   which returns one `<VirtualDisk xmlns=UOM schemaVersion="V1_0">` with `Metadata`, then
   `DiskCapacity kb="CUR" kxe="false"` (GiB), then `DiskName kb="CUR" kxe="false"`, and no `kb`
   on `VirtualDisk`. The capacity rule (positive multiple of 1024 MiB) is unchanged.
   `build_virtual_disk_delete_document` is deleted; delete builds nothing.
4. **Create:** validate UUIDs and build the element (both before any request); GET; refuse
   without an ETag; take the direct-child `VirtualDisks` (created empty — `kb="CUD"
   kxe="false" schemaVersion="V1_0"`, `Metadata/Atom` — and appended as the last child when the
   GET has none, the XSD's last position); refuse with `HMCError(..., 409)` when a direct-child
   `VirtualDisk` already has that `DiskName`; insert after the collection's `Metadata` (index 0
   when none); `_post_vg_xml(operation="create_virtual_disk", etag=...)`.
5. **Delete:** GET; refuse without an ETag; select direct-child `VirtualDisk` elements of the
   direct-child `VirtualDisks` whose `DiskName` equals `disk_name`; refuse zero matches
   (`HMCError`, 404) and more than one (`HMCError`, 409) without writing; remove the one match;
   `_post_vg_xml(operation="delete_virtual_disk", etag=...)`.
6. **In-use guard** (operator decision (A), 2026-09-23): the operation-level
   `delete_virtual_disk` refuses when a mapping on the same VIOS names the disk inline by
   `DiskName` (V10R3 sends no `href`), keeping the `href` match; a `VolumeGroup` link on the
   mapping's disk, when present, must name the target group.
7. Element names are matched by local name so a bare (non-namespaced) document, which
   `_get_vg_raw_xml` already accepts, works the same way.

Return values are unchanged: the first parsed entry of the POST response, or `None`.

## Failure model

1. **Actors and deployments** — a local operator or agent using `hmcpctl` / the MCP server
   against an HMC it is authorized to mutate; V10R3 is the verified target.
2. **Invariants and assets at stake** — existing virtual disks (guest data) and physical-volume
   membership of the target group (#779: an earlier VolumeGroup write destroyed PV metadata).
   The POST is conditioned on `If-Match`; that the HMC enforces a mismatch is to be confirmed under
   #879 (live evidence shows only that a matching tag is accepted).
3. **Accepted failure classes** —
   - An HMC that returns no `ETag` on the VolumeGroup GET cannot create or delete virtual disks
     through this path; the error says so. Accepted: V10R3 returns one, and an unconditional
     whole-group write is the lost-update risk this change exists to remove.
   - The created-empty `VirtualDisks` (group with no disks) is not live-verified; a wrong shape
     is rejected at schema validation before any change, as both live rejections were.
   - Delete by omission (the whole-group POST without the disk) is not live-verified; live
     evidence covers the create. If the HMC merged rather than replaced the collection, delete
     would return success with the disk still present. Confirmation belongs to #879; a read-back
     postcondition (re-GET and look for the disk) is deferred there too, not added here.
   - `ET` re-serialization of the fetched element (namespace prefixes, whitespace) is the same
     serialization the media operations already POST.
4. **Covered elsewhere** — write-header strategy (#935); live confirmation (#879); builder
   `kb`/order sweep beyond this `VirtualDisk` (#961); VIOS mapping sparse documents (#962);
   media-repository units (#963); 15-character name validation (#964). Criterion 5's review of
   the other VolumeGroup writes is reported in the PR body, not changed here: the media RMW
   operations POST the whole group without `If-Match`, so one racing a virtual-disk write can
   re-add a deleted disk or drop a new one; `_broker_iso_import` POSTs a sparse document to the
   same endpoint; `create_volume_group` PUTs a sparse document to the collection.

## Success

- The create POST body is the fetched `VolumeGroup` with exactly one added `VirtualDisk`, and
  every `VirtualDisk` and `PhysicalVolume` element from the GET appears in it canonically
  unchanged; the delete POST body is the fetched group minus exactly the matched disk, with every
  other `VirtualDisk` and `PhysicalVolume` canonically unchanged.
- Both POSTs carry `If-Match` equal to the GET's `ETag`.
- Missing ETag, a duplicate create name, and zero or multiple delete matches raise `HMCError`
  and send no POST; a 412 raises `HMCError` with status 412 and no second POST.
- The media-repository operations' requests are unchanged (existing tests stay green).

## Validation

Tests use a synthetic V10R3-shaped fixture (no lab identifiers): `VolumeGroup schemaVersion`,
`AvailableSize`/`FreeSpace` (ROR), `GroupCapacity`/`GroupName` (CUR), `PhysicalVolumes` (CUD)
with one `PhysicalVolume`, `VirtualDisks` (CUD) with `Metadata` and two `VirtualDisk` children
(`DiskCapacity`, `DiskLabel`, `DiskName` CUR; `VolumeGroup` href and `UniqueDeviceID` ROR).
The contracts and their tests are listed per task in
the local plan `2026-09-23-virtual-disk-rmw.md`.

## Considered and rejected

- **Keep the sparse document, fix its attributes.** verified: the owner's #936 comment
  (2026-09-23, V10R3) records a sparse document rejected at schema validation, and RMW succeeding.
- **POST through `_post` / `_write_uom`.** verified: `_post` accepts no extra headers
  (`src/hmcpctl/client/core.py`, `_post` signature), so it cannot carry `If-Match` without a
  `core.py` change, which #935 owns.
- **Send `If-Match` only when an ETag is present.** judgment: silently degrades to the
  unconditional whole-group write on exactly the path that risks guest data.
