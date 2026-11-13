# ADR 0376: Typed vSCSI client adapters at partition creation

## Status

Accepted (2026-11-13) for #1980, subject to implementation verification.

## Context

An IBM i virtual load source must be an adapter in the partition's own creation record
(#1830, #1981). ADR 0274 rejects post-create assignment for initial native records and
arbitrary adapter records as an unbounded surface. `mksyscfg` accepts
`virtual_scsi_adapters` client records naming a remote VIOS; a 2026-11-13 V10R3 capture showed
the HMC stores such a record with no server adapter on the VIOS and creates none.

## Decision

Add a finite typed client-only record, `VirtualScsiClientAdapter`, to LPAR creation
(AIX/Linux and OS400) beside the serial records of ADR 0303. Resolve the named VIOS in the
selected system's contained inventory, require it to be a VIOS, and authorize it through the
existing partition ownership policy before the single native create. Render the record in
that create; never write the VIOS. Compare the created `default_profile` record by record and
state in the result that no server adapter or mapping was created and that pairing is unproven.

## Consequences

Creation with vSCSI records always takes the native path and defers apply until the
comparison passes. Declaring serial and vSCSI together requires the serial readback to stop
treating a declared vSCSI family as foreign configuration. A client record pointing at an
absent server slot is a configured, unpaired adapter until #680/#663 create the server side;
the caller learns that from the result, not from a refusal.

## Considered & rejected

- **Accept server-role records too.** judgment: a server record belongs on the VIOS, whose
  adapters and mappings #680/#663 own; on a client LPAR it serves no #1830 purpose.
- **Create the matching VIOS server adapter in the same call.** verified: the 2026-11-13
  capture (`lshwres -r virtualio --rsubtype scsi` unchanged after create) shows creation does
  not do it, so it would be a second mutation of another partition with its own failure states.
- **Refuse when the remote server slot does not exist.** verified: the same capture shows the
  HMC accepts the record without one; refusing would block the intended order (client first,
  server via #663) without protecting anything.
- **Generalise the serial record with a family discriminator.** judgment: the grammars differ
  (six vs seven components, no HMC flag, client-only), and a shared record would weaken both
  validators.
- **Do nothing.** judgment: #1981 cannot bind a virtual load source without a declared slot.
