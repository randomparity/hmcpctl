# vSCSI and vFC client adapter options (#663)

Decision record: [ADR 0374](../../adr/0374-vfc-vscsi-client-adapter-reads-and-update-disposition.md).

## Problem

Two ledger rows bind #663: `rest:managed-system/logical-partition/virtual-scsi-client-adapter`
(collection PUT; instance GET and DELETE) and
`rest:managed-system/logical-partition/virtual-fiber-channel-client-adapter` (collection GET and
PUT; instance POST and DELETE). Add, list and delete already exist. Three gaps remain:

- No operation reads one client storage adapter by UUID.
- Callers get no typed paired identity. Today they must parse the raw `WWPNs` string and the
  `ConnectingPartition` link themselves.
- Add trusts any `vios_partition_id`. Nothing checks that the ID names a VIOS on the client
  partition's own managed system.

A live capture (V10R3 M1060, POWER9, an inactive scratch partition) found two more facts:

- `hmc_add_vfc_adapter` has never worked. Its document sends `kb="CUD"` on
  `ConnectingPartitionID` and `ConnectingVirtualSlotNumber`. The HMC rejects that with
  400 REST0001 ("Value 'CUD' is not facet-valid with respect to enumeration '[CUR]'"). The same
  document with `kb="CUR"` is accepted.
- The vFC instance POST returns 200 and applies nothing. Five fields were probed (remote slot,
  remote partition ID, WWPN order, WWPN omission and RequiredAdapter); every readback was
  unchanged and the ETag stayed the same. A stale `If-Match` returns 412.

## Scope

1. **vFC create document.** `build_vfc_adapter_document` emits `kb="CUR"` on both connecting
   fields, matching the served XSD and the live acceptance.
2. **Instance read.** Add `HMCClient.get_storage_adapter(lpar_uuid, adapter_type, adapter_uuid)`
   for `VirtualSCSIClientAdapter` and `VirtualFibreChannelClientAdapter`, and the
   `adapter.get` operation behind MCP `hmc_get_adapter` and CLI `adapters get`. The read is
   read-effect and needs no ownership.
   - HTTP 404 raises `ResourceNotFoundError("adapter", uuid, …)`.
   - An entry whose UUID differs from the requested UUID raises `HMCError`.
3. **Paired identity.** `paired_identity(resource, adapter_type)` returns these keys:
   - `remote_partition_id` (int or None)
   - `remote_slot` (int or None)
   - `remote_vios_uuid`: the UUID from a `ConnectingPartition` href whose second-to-last path
     segment is `VirtualIOServer`, else None
   - `wwpns` (vFC only): the two values lowercased, or None unless `WWPNs` holds exactly two
     16-hex tokens

   An unparseable field becomes None; it is never an error. Add, get, and list of the two
   storage types attach the projection as `paired_identity`. Network and vNIC listings are
   unchanged.
4. **Remote VIOS check.** Before the PUT, `add_vscsi_adapter` and `add_vfc_adapter` read the
   client partition's `AssociatedManagedSystem`, list that system's VIOSes, and require exactly
   one VIOS whose `PartitionID` equals `vios_partition_id`. Otherwise they raise `ValueError`
   and send no PUT. The check runs after the existing ownership authorization, which is
   unchanged.
5. **No vFC update surface.** Per ADR 0374, the vFC row's disposition becomes `unknown`, with
   its question citing the no-effect capture. The vSCSI row becomes `supported`.

No ownership transition is needed: every change extends its current owner in the adapter
modules.

### Failure model

- **Actors and deployments:** a local operator through the CLI, and an MCP client under the
  existing tool authorization (ADR 0039 `all-targets` for the adds).
- **Invariants and assets:**
  - No client adapter is paired to a non-VIOS or to a VIOS on another frame.
  - Adds still refuse an unowned client partition.
  - A read never mutates.
- **Accepted failure classes:**
  - A VIOS removed or renumbered between the check and the PUT. That window is bounded and
    tolerated. The HMC's own pairing status (HSCLB023) still reports it.
  - An HMC that changes behaviour across releases for the vFC instance GET (not in the method
    table). The read then fails as an `HMCError`; nothing is mutated.
- **Covered elsewhere:**
  - N_Port login/status (#698)
  - VIOS-side server adapters and mappings (#680)
  - REST0269/HSCLB021 add classification (#1895)

### Threat model

- **Boundaries:**
  - Added: the `hmc_get_adapter` tool arguments. `adapter_uuid` and `lpar_uuid` enter the URL
    path through the existing UUID path-argument validation.
  - Widened: the HMC response parse that `paired_identity` reads. It parses only and builds no
    command or URL.
- **Actors:** an MCP client is untrusted for authorization. The HMC is trusted for data, but its
  values are still bounded by type checks.
- **Controls:**
  - The existing `@tool` read-effect authorization and `target_kind="lpar"`.
  - `_reject_unknown_uom_type` and UUID path checks.
  - The adds keep `resolve_and_authorize_lpar_mutation`.
- **Out of scope:** authorizing the VIOS itself. Nothing on the VIOS is written, and ADR 0039
  already scopes these tools.

## Success

1. The vFC create document carries `kb="CUR"` on both connecting fields.
2. `hmc_get_adapter` returns the entry plus `paired_identity` for both storage types. A 404
   raises `ResourceNotFoundError`, and a UUID mismatch raises `HMCError`.
3. The add, get and list results for the two storage types carry `paired_identity`. vFC
   `wwpns` is the WWPN pair, or None when the HMC value is malformed.
4. An add naming an ID that matches no VIOS, or more than one, on the client's system is refused
   before any PUT.
5. The vSCSI row is `supported` and the vFC row is `unknown`. `adapter.get` has a maturity
   record, operations entry, tool docs, authorization-audit row and changelog entry.
   `just verify` is green.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| vFC kb values | focused-test | `tests/unit/test_documents_v10r3.py` kb map expects CUR; red before the fix |
| get client path, 404, UUID mismatch | focused-test | `tests/unit/test_client_adapters.py` respx cases |
| paired_identity parsing | focused-test | `tests/vios/test_adapters.py` cases built from the captured vSCSI/vFC shapes: valid, malformed WWPNs, non-VIOS href |
| remote VIOS check | focused-test | `tests/vios/test_adapters.py`: no match, duplicate, match; assert no PUT on refusal |
| MCP/CLI wiring and tool security | focused-test | `tests/storage/test_storage_tools.py`, `tests/app/test_tool_security.py`, `tests/app/test_cli_commands.py` |
| ledger and generated docs | focused-test | `just capability-inventory`, `just tool-docs-check`, `just doc-freshness` |
| live behaviour | task-test-not-applicable | live runs are not a merge gate (ADR 0212); the maturity record stays unevidenced with its gap |
