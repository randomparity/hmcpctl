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

Role matrix from published IBM guidance (IBM support pages "Create a Client Partition
(i, AIX, or Linux) Hosted by an IBMi Server Partition"
<https://www.ibm.com/support/pages/create-client-partition-i-aix-or-linux-hosted-ibmi-server-partition-using-hmc-classic-signon>,
"Creating a Virtual Optical drive in a HMC-Managed VIOS Partition"
<https://www.ibm.com/support/pages/creating-virtual-optical-drive-hmc-managed-vios-partition>,
"C2004130 Unable to load IBM i OS to NPIV SAN Storage"
<https://www.ibm.com/support/pages/c2004130-unable-load-ibm-i-os-npiv-san-storage-vfchost-status-remains-notloggedin-vios>):

| Virtual family | Load source | Alternate restart | Console | Here |
|---|---|---|---|---|
| vSCSI client | yes; must be required to be offered on the HMC tagged-I/O screen | yes (virtual optical) | not named | admitted when declared |
| vFC client | yes (NPIV SAN disk) | not named | not named | no creation input; refused (#1830) |
| virtual serial, virtual ethernet | not named | not named | not named | refused |

The capture's saved-profile readback (`lssyscfg -r prof`) echoed `load_source_slot=2,
alt_restart_device_slot=2,console_slot=hmc` exactly as submitted.

## Decision

hmcpctl owns the role rule. A canonical decimal virtual slot is admitted for
`load_source_slot` and `alt_restart_device_slot` only when it equals the local slot of a
vSCSI client record declared in the same creation request. Every other virtual slot is refused before effects.
Virtual `console_slot`, `alt_console_slot` and `op_console_slot` forms stay refused. IBM i
native creation without `console_slot` is refused before effects; no default is inserted.
`HSCL0DC6` remains a backstop only.

The created profile's virtual tagged fields are compared from the same saved-profile record
the vSCSI step already reads (`lssyscfg -r prof`), together with that slot's client
record; the REST profile comparison keeps its physical, SR-IOV and `hmc` fields.

## Consequences

Virtual-slot tagged I/O always takes the native path with declared vSCSI records, so it
inherits ADR 0376's deferred apply and unproven pairing: a configured load source points at
an adapter whose server side may not exist yet. A caller that relied on the HMC's rc 1 for a
console-less IBM i native create now gets a refusal before any write; because a console is
initial-profile I/O, which controller-mode creation already refuses, native IBM i creation on a
PowerVM management controller is refused before effects too. The REST rendering of a
virtual tagged slot is uncaptured and is not compared.

## Considered & rejected

- **Forward any virtual slot and let the HMC decide.** verified: the issue #1981 capture
  shows the HMC accepted serial slot 0 and ethernet slot 3 as a load source.
- **Admit virtual console slots bound to a declared adapter.** judgment: no cited source names
  an eligible virtual console family, and the HMC validates nothing that would catch a wrong one.
- **Also require the load source's client to be `is_required=true`.** verified: the issue
  #1981 capture's `mksyscfg` accepted a client declared with is-required `0` as load source,
  although the first cited page says the HMC tagged-I/O screen offers only required adapters;
  the issue's criterion admits any declared client, so the refusal would be unrequested.
- **Require `console_slot` on REST creation too.** verified: at cced9acf
  `admit_creation_io` (`operations/lpar/creation/physical_io.py`) refuses `physical_io` with a
  keylock, and `_create_via_cli` refuses keylock, so IBM i keylock creation is REST-only and a
  universal console rule would remove it.
- **Insert `console_slot=hmc` when absent.** judgment: ADR 0274 rejects an implicit console.
- **Compare virtual slots through the REST profile.** judgment: their REST rendering is
  uncaptured, and the CLI readback that echoes them is already read for the same records.
- **Do nothing.** verified: `src/hmcpctl/documents/lpar_io.py` at cced9acf refuses every
  positive virtual-slot form, so #1830's virtual load source stays unreachable.
