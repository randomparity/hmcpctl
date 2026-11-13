# ADR 0374: vFC and vSCSI client adapter reads, pairing check and update disposition

## Status

Accepted (2026-11-13), issue #663, under the operator-confirmed campaign dispatch. Extends
ADR 0121 (adapter domain ownership) and ADR 0039 (`all-targets` grant for the client
storage adds); neither is amended.

## Context

The P11 reference lists, for the vFC client adapter, an instance POST ("change state of the
entity") and DELETE plus a collection GET and PUT
(`docs/refs/hmc-rest-api-p11/managed-system/logical-partition/167-virtual-fiber-channel-client-adapter.md:25-45`),
and for the vSCSI client adapter an instance GET and DELETE plus a collection PUT
(`…/169-virtual-scsi-client-adapter.md:14-44`). The corpus carries no field list. A live
capture on 2026-11-13 (HMC V10R3 M1060, POWER9 frame, an inactive owned scratch partition;
private evidence, redacted here) established:

- The served XSD (`/rest/api/web/schema/VirtualFibreChannelAdapter.xsd`) marks
  `ConnectingPartitionID` and `ConnectingVirtualSlotNumber` as `CUR`, and the client type
  adds `WWPNs` (`CUR`) and `NportLoggedInStatus` (`UOD`).
- A vFC PUT with `kb="CUD"` on those fields, which is what hmcpctl sent, is refused 400
  REST0001 as not facet-valid. The same body with `kb="CUR"` is accepted. The PUT response
  and readback carry `WWPNs` as two lowercase 16-hex values separated by one space, and a
  `ConnectingPartition` link to `ManagedSystem/<uuid>/VirtualIOServer/<uuid>`.
- Instance GET answers 200 with an `ETag` header for both adapter types. vFC's instance GET is
  not in its method table. A well-formed unknown UUID returns 404 REST000B.
- Six instance POSTs to the vFC adapter each returned 200: a no-op with and without
  `If-Match`, a remote slot change, a remote partition ID change, a reversed WWPN pair, and an
  omitted `WWPNs`. A seventh changed `RequiredAdapter`. In every case the readback was unchanged
  and the ETag stayed the same. A stale `If-Match` returned 412 and changed nothing.
- The HMC accepted a client add naming a VIOS ID with no matching server slot. It reported
  HSCLB023 per WWPN in `NportLoggedInStatus` and created nothing on the VIOS.

## Decision

1. **No vFC update operation.** hmcpctl exposes no tool over the vFC instance POST, because an
   operation whose documented request applies nothing would report success for a change that
   did not happen. The vFC row's disposition becomes `unknown`, and its question states the
   observed no-effect and the uncaptured active-partition (DLPAR) case. The vSCSI row's
   documented methods are all covered after this change, so it becomes `supported`.
2. **One instance read, `adapter.get`, for both client storage types.** It uses the observed
   vFC instance GET rather than filtering the collection. A 404 is `ResourceNotFoundError`; an
   entry naming a different UUID is an `HMCError`.
3. **`paired_identity`** is a projection, not a replacement. The raw resource stays in the
   result. Its fields are `remote_partition_id`, `remote_slot`, `remote_vios_uuid` (only from a
   `VirtualIOServer` link) and, for vFC, `wwpns`. Any field the HMC value does not parse cleanly
   is `None`.
4. **The client storage adds check the remote end before writing.** The named partition ID must
   match exactly one VIOS on the client partition's own managed system, or the add is refused
   before the PUT. The HMC does not prove pairing (point 5 of Context), so this check is
   hmcpctl's own. It identifies the VIOS but authorizes nothing on it, because nothing on the
   VIOS is written.

## Consequences

- `hmc_add_vfc_adapter` works for the first time. Its maturity record stays unevidenced until a
  release window runs it.
- Each storage add now reads the client partition and its system's VIOS feed: two extra GETs,
  and the VIOS feed can be large.
- An active-partition vFC POST may behave differently. Until it is captured, the vFC row stays
  `unknown` and blocks a complete-coverage claim, which is honest.
- WWPNs are never accepted as input, so hmcpctl cannot reuse or collide fabric identities.

## Considered & rejected

- **Expose `hmc_update_vfc_adapter` over the POST.** verified: seven POST probes on V10R3 M1060
  (inactive partition) returned 200 with unchanged readback and ETag. A tool would report false
  success.
- **Expose the update with readback verification that reports `mismatched`.** judgment: cost;
  this is a tool whose only observed outcome is failure.
- **Read by filtering the collection feed (`adapter.list`).** judgment: fit; the instance GET is
  the row's documented vSCSI method, and live it answers both types with an ETag.
- **Leave the remote VIOS unchecked and rely on the HMC.** verified: the vFC add naming VIOS slot
  40, with no server adapter, was accepted (200) on V10R3 M1060. The HMC does not prove pairing.
- **Mark the vFC row `supported`.** judgment: fit; a documented method with no implemented or
  explained effect would overstate coverage.
- **Do nothing.** verified: the existing vFC add is refused 400 REST0001 live, so the row's add is
  broken today.
