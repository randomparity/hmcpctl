# ADR 0374: vFC and vSCSI client adapter reads, pairing check and update disposition

## Status

Accepted (2026-11-13), issue #663, under the operator-confirmed campaign dispatch. It extends
ADR 0121 (adapter domain ownership) and ADR 0039 (the `all-targets` grant for the client
storage adds). Neither is amended.

## Context

The P11 reference lists these methods. The corpus carries no field list.

- **vFC client adapter:** an instance POST ("change state of the entity") and DELETE, plus a
  collection GET and PUT
  (`docs/refs/hmc-rest-api-p11/managed-system/logical-partition/167-virtual-fiber-channel-client-adapter.md:25-45`).
- **vSCSI client adapter:** an instance GET and DELETE, plus a collection PUT
  (`…/169-virtual-scsi-client-adapter.md:14-44`).

A live capture on 2026-11-13 (HMC V10R3 M1060, a POWER9 frame, an inactive owned scratch
partition; the evidence is private and redacted here) established the following:

- **Served schema.** The XSD (`/rest/api/web/schema/VirtualFibreChannelAdapter.xsd`) marks
  `ConnectingPartitionID` and `ConnectingVirtualSlotNumber` as `CUR`. The client type adds
  `WWPNs` (`CUR`) and `NportLoggedInStatus` (`UOD`).
- **vFC add.** A PUT with `kb="CUD"` on those fields, which is what hmcpctl sent, is refused 400
  REST0001 as not facet-valid. The same body with `kb="CUR"` is accepted. The PUT response and
  the readback carry `WWPNs` as two lowercase 16-hex values separated by one space.
- **Instance GET.**
  - It returns 200 with an `ETag` header for both adapter types.
  - vFC's instance GET is not in its method table.
  - A well-formed unknown UUID returns 404 REST000B.
  - Both types carry a `LocalPartitionID` equal to the client's partition ID.
  - Both carry a `ConnectingPartition` link to `ManagedSystem/<uuid>/VirtualIOServer/<uuid>`
    (with `ksv="V1_3_0"` on vSCSI).
- **vFC instance POST.** Seven POSTs all returned 200, and in every case the readback and the
  ETag were unchanged. Two were no-ops, one with `If-Match` and one without. Five changed a
  field:
  - the remote slot
  - the remote partition ID
  - the WWPN pair (reversed)
  - `WWPNs` (omitted)
  - `RequiredAdapter`

  A stale `If-Match` returned 412 and changed nothing.
- **Unpaired add.** The HMC accepted a client add naming a VIOS ID with no matching server slot.
  It reported HSCLB023 per WWPN in `NportLoggedInStatus` and created nothing on the VIOS.

## Decision

1. **No vFC update operation.**
   - hmcpctl exposes no tool over the vFC instance POST, because an operation whose documented
     request applies nothing would report success for a change that did not happen.
   - The vFC row stays a coverage child, re-pointed to #1986, which owns the uncaptured
     active-partition (DLPAR) case.
   - After this change every documented vSCSI method is covered, so the vSCSI row becomes
     `supported`.
2. **One instance read, `adapter.get`, for both client storage types.** It uses the observed vFC
   instance GET rather than filtering the collection.
   - The partition is read first, so a 404 on the instance means the adapter is missing.
   - The entry must name the requested UUID.
   - Its `LocalPartitionID` must equal the partition's `PartitionID`, so a read scoped to one
     partition never returns another partition's adapter.
3. **`paired_identity` is a projection, not a replacement.** The raw resource stays in the
   result. The projection's fields are:
   - `remote_partition_id`
   - `remote_slot`
   - `remote_vios_uuid`, taken only from a `VirtualIOServer` link
   - `wwpns`, vFC only

   Any field whose HMC value does not parse cleanly is `None`.
4. **The client storage adds check the remote end before writing.**
   - The named partition ID must match exactly one VIOS on the client partition's own managed
     system, read from its `AssociatedManagedSystem` link. Otherwise the add is refused before
     the PUT.
   - The check proves the remote is one local VIOS at check time. It does not prove that a server
     adapter exists at `vios_slot`; server-side work belongs to #680.
   - It identifies the VIOS but authorizes nothing on it, because nothing on the VIOS is written.

## Consequences

- `hmc_add_vfc_adapter` works for the first time. Its maturity record stays unevidenced until a
  release window runs it.
- Each storage add now reads the client partition and its system's VIOS feed: two extra GETs,
  and the VIOS feed can be large. The ownership guard's own system resolution is not reused, so
  its shared return contract stays unchanged.
- An add to a real VIOS at an unused server slot still succeeds, as it does on the HMC. vSCSI has
  no HMC-side unpaired signal. vFC reports HSCLB023 only inside the raw `NportLoggedInStatus`
  (#698).
- An active-partition vFC POST may behave differently. Until #1986 captures it, the vFC row
  stays a pending coverage child and blocks a complete-coverage claim, which is the honest
  state.
- hmcpctl never accepts WWPNs as input, so it cannot reuse or collide fabric identities.

## Considered & rejected

- **Expose `hmc_update_vfc_adapter` over the POST.** verified: seven POST probes on V10R3 M1060
  (inactive partition) returned 200 with unchanged readback and ETag. A tool would report false
  success.
- **Expose the update with readback verification that reports `mismatched`.** judgment: cost;
  this is a tool whose only observed outcome is failure.
- **Capture the active-partition POST before deciding.** verified: the authorized lab frame has
  no NPIV-capable FC port (private lab inventory, 2026-11-13), so no mapped, logged-in active
  state is reachable there.
- **Mark the vFC row `unknown`.** judgment: fit; it records an ownerless question when a tracked
  owner exists.
- **Mark the vFC row `proposed-exclusion`.** judgment: fit; it declares the method unwanted,
  while the open question is whether the method works at all on an active partition.
- **Mark the vFC row `supported`.** judgment: fit; a documented method with no implemented or
  explained effect would overstate coverage.
- **Read by filtering the collection feed (`adapter.list`).** judgment: fit; the instance GET is
  the row's documented vSCSI method, and live it answers both types with an ETag.
- **Leave the remote VIOS unchecked and rely on the HMC.** judgment: fit; the HMC accepted an add
  naming an unpaired server slot (Context), so its validation of the remote's kind cannot be
  assumed, and no non-VIOS remote was captured.
- **Reuse the ownership guard's resolved system for the VIOS check.** judgment: cost; it widens a
  guard that every LPAR mutation shares, to save one GET here.
- **Do nothing.** verified: the existing vFC add is refused 400 REST0001 live, so the row's add is
  broken today.
