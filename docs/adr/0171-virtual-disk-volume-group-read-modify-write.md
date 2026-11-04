# ADR 0171: VolumeGroup writes use read-modify-write with If-Match

## Status

Accepted (2026-09-23). Scope amended 2026-09-23 (#996) to cover every VolumeGroup write: the
four media-repository operations join the virtual-disk writes under the same contract.

Concurrency assurance superseded (2026-11-04, #1888): ADR 0328 Finding 6 records
V10R3 M1060 accepting a VolumeGroup POST with a never-matching `If-Match`.
The historical concurrency claims below do not hold on that HMC: a whole-group
write can silently lose a concurrent change. Callers must serialize writers to
the same VolumeGroup for the entire operation. Keep sending the GET's ETag and
refusing a missing ETag, but neither establishes concurrency safety. If a server
returns 412, the existing refusal handling still applies; 412 cannot be relied
on to detect races. The six primitive mutation tools and `hmc_attach_disk_to_lpar`
warn callers explicitly; logical mutation tools retain their writer-window
assertions. This observation does not establish VIOS mapping enforcement;
ADR 0169's unprobed mapping claims remain unchanged.

## Context

`create_virtual_disk` and `delete_virtual_disk` POSTed a sparse `VolumeGroup` document holding
only the disk being changed. On V10R3 both were rejected at schema validation, and a sparse
document omits the group's other disks and physical volumes; an earlier VolumeGroup write
(#779) destroyed physical-volume metadata. In the #879 live window (2026-09-23, V10R3 M1060) a
read-modify-write succeeded: GET the group, insert the disk, POST the whole element back with
`If-Match` set to the GET's `etag` header. That GET returned an `etag`; ETag presence was
observed on V10R3 M1060 only.

## Decision

Both writes GET the whole `VolumeGroup`, add or remove exactly one `VirtualDisk`, and POST the
whole element back through the existing media-repository helper (`Accept: */*`, typed
`Content-Type`) with `If-Match` set to the GET's ETag. A GET without an ETag refuses the write
before any POST. A 412 is reported as a concurrent change with nothing written. Delete refuses
zero or several matching disks; create refuses a name the group already holds.

The same contract governs every whole-group `VolumeGroup` write. `create_media_repository`,
`create_optical_media`, `delete_media_repository` and `delete_optical_media` already
read-modify-write the group; each now POSTs with `If-Match` set to its GET's ETag, refuses before
any POST when that GET carried none, and reports a 412 as the same nothing-written concurrent
change (#996). A media operation that finds nothing to change posts nothing and so needs no
ETag.

## Consequences

- An HMC that sends no ETag on the VolumeGroup GET cannot create or delete virtual disks, or
  create or delete a media repository or optical medium, through this path; the error names the
  missing ETag.
- That the HMC enforces a mismatched `If-Match`, and deletes a disk omitted from the POST, is
  not yet live-verified (#879), for either the virtual-disk or the media path.
- Because every VolumeGroup write is conditioned on the ETag it read, a media write racing a
  virtual-disk write fails with 412 rather than posting the group as it was before that write.

## Considered & rejected

- **Fix the sparse document's attributes.** verified: the #936 owner comment (2026-09-23,
  V10R3) records the sparse document rejected at schema validation and read-modify-write
  succeeding.
- **Send `If-Match` only when an ETag is present.** judgment: silently falls back to an
  unconditional whole-group write on the path that holds guest data.
- **POST through `_post` / `_write_uom`.** verified: `_post` in `src/hmcpctl/client/core.py`
  takes no extra headers, so it cannot carry `If-Match` without a `core.py` change (#935).
