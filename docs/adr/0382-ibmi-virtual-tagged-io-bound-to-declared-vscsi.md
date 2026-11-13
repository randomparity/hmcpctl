# ADR 0382: IBM i virtual tagged I/O bound to declared vSCSI clients

## Status

Accepted (2026-11-13) for #1981, subject to implementation verification. Amends
[ADR 0274](0274-lpar-physical-io-creation.md): two of its three refused virtual-slot forms
become admissible.

## Context

ADR 0274 refuses positive virtual-slot forms of `load_source_slot`,
`alt_restart_device_slot` and `console_slot` because creation could not declare the adapter
they name. ADR 0376 added typed vSCSI client records to creation. A 2026-11-13 V10R3 capture
(issue #1981) showed the HMC is no role guard: `mksyscfg` for `os400` accepted any of the
partition's own virtual slots (vSCSI, serial 0, ethernet) as load source or alternate restart,
refused only a slot the partition lacks (`HSCL0DC6`), stored any `console_slot` value
unchecked (including a nonexistent slot), and refused a record without `console_slot`
(rc 1, no HSCL code).

Published IBM i guidance names one virtual family for these roles: a client SCSI adapter is
tagged as the load source of a hosted IBM i partition, and must be marked required to be
offered as one; the same adapter is tagged as the alternate restart device to IPL from
virtual optical (IBM support pages "Create a Client Partition (i, AIX, or Linux) Hosted by
an IBMi Server Partition" and "Creating a Virtual Optical drive in a HMC-Managed VIOS
Partition"). No source in scope establishes a virtual console device family.

## Decision

hmcpctl owns the role rule. A canonical decimal virtual slot is admitted for
`load_source_slot` and `alt_restart_device_slot` only when it equals the local slot of a
vSCSI client record declared in the same creation request; a load source additionally needs
that record's `is_required=true`. Every other virtual slot is refused before effects.
Virtual `console_slot`, `alt_console_slot` and `op_console_slot` forms stay refused. IBM i
native creation without `console_slot` is refused before effects; no default is inserted.
`HSCL0DC6` remains a backstop only.

The created profile's virtual tagged fields are compared from the same saved-profile record
the vSCSI step already reads (`lssyscfg -r prof`, whose readback was captured), together with
that slot's client record; the REST profile comparison keeps its physical, SR-IOV and `hmc`
fields.

## Consequences

Virtual-slot tagged I/O always takes the native path with declared vSCSI records, so it
inherits ADR 0376's deferred apply and unproven pairing: a configured load source points at
an adapter whose server side may not exist yet. A caller that relied on the HMC's rc 1 for a
console-less IBM i native create now gets a refusal before any write. The REST rendering of a
virtual tagged slot is uncaptured and is not compared.

## Considered & rejected

- **Forward any virtual slot and let the HMC decide.** verified: the issue #1981 capture
  shows the HMC accepted serial slot 0 and ethernet slot 3 as a load source.
- **Admit virtual console slots bound to a declared adapter.** judgment: no cited source names
  an eligible virtual console family, and the HMC validates nothing that would catch a wrong one.
- **Require `is_required` for alternate restart too.** judgment: the cited sources state the
  required flag only for the load source; adding it would be an unsourced refusal.
- **Insert `console_slot=hmc` when absent.** judgment: ADR 0274 rejects an implicit console.
- **Compare virtual slots through the REST profile.** judgment: their REST rendering is
  uncaptured, and the CLI readback that echoes them is already read for the same records.
- **Do nothing.** verified: `src/hmcpctl/documents/lpar_io.py` at cced9acf refuses every
  positive virtual-slot form, so #1830's virtual load source stays unreachable.
