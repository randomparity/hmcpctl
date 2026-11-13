# vSCSI and vFC client adapter options (#663)

Decision record: [ADR 0374](../../adr/0374-vfc-vscsi-client-adapter-reads-and-update-disposition.md).

## Problem

Two ledger rows bind #663:

- `rest:managed-system/logical-partition/virtual-scsi-client-adapter`: collection PUT, plus
  instance GET and DELETE.
- `rest:managed-system/logical-partition/virtual-fiber-channel-client-adapter`: collection GET
  and PUT, plus instance POST and DELETE.

Add, list and delete already exist. Three gaps remain:

- No operation reads one client storage adapter by UUID.
- There is no typed paired identity. Callers must parse the raw `WWPNs` string and the
  `ConnectingPartition` link themselves.
- Add trusts any `vios_partition_id`.

A live capture (V10R3 M1060, POWER9, an inactive scratch partition; ADR 0374 Context) found two
more facts:

- `hmc_add_vfc_adapter` has never worked. Its `kb="CUD"` on `ConnectingPartitionID` and
  `ConnectingVirtualSlotNumber` is refused with 400 REST0001, and `kb="CUR"` is accepted.
- The vFC instance POST applies nothing. Seven probes all returned 200 with unchanged readback
  and ETag: two no-ops, and five field changes. A stale `If-Match` returns 412.

## Scope

1. **vFC create document.** Emit `kb="CUR"` on both connecting fields.
2. **Instance read**, the `adapter.get` operation behind MCP `hmc_get_adapter` and CLI
   `adapters get`, for `VirtualSCSIClientAdapter` and `VirtualFibreChannelClientAdapter`. It is
   read-effect and needs no ownership check. It runs in this order:
   1. Resolve the partition and read it with `get_logical_partition`. A 404 or an empty result
      raises `ResourceNotFoundError("partition", …)`.
   2. GET the instance path, built per literal type so the live-vocabulary gate sees each
      endpoint. A 404 raises `ResourceNotFoundError("adapter", uuid, …)`.
   3. Check the entry. A different UUID, an empty response, or a `LocalPartitionID` that does
      not equal the partition's `PartitionID` raises `HMCError`, and nothing is returned.
3. **Paired identity.** `paired_identity(resource, adapter_type)` returns these keys:
   - `remote_partition_id`: `RemoteLogicalPartitionID` (vSCSI) or `ConnectingPartitionID` (vFC)
   - `remote_slot`: `RemoteSlotNumber` or `ConnectingVirtualSlotNumber`
   - `remote_vios_uuid`: from the `ConnectingPartition` href when its second-to-last segment is
     `VirtualIOServer`. Both client types carry that link live (ADR 0374 Context). Otherwise it
     is None.
   - `wwpns` (vFC only): the lowercased pair, or None unless `WWPNs` holds exactly two 16-hex
     tokens

   A value that does not parse becomes None. Add, get and list attach the projection for the two
   storage types only. The raw resource is kept. Tests load a fixture with the captured element
   set and synthetic identifiers.
4. **Remote VIOS check.** Before the PUT, after the unchanged ownership authorization, the two
   storage adds take these steps:
   1. Read the client partition and take its `AssociatedManagedSystem` UUID. A None entry, a
      missing link or a malformed link raises `ValueError` and sends nothing.
   2. List that system's VIOSes.
   3. Require exactly one VIOS whose `PartitionID` equals `vios_partition_id`. Otherwise raise
      `ValueError` and send no PUT.

   The check does not prove that a server adapter exists at `vios_slot`. That belongs to #680.
5. **Ledger.** The vSCSI row becomes `supported`. The vFC row stays a coverage child,
   re-pointed to #1986 (active-partition capture). There is no vFC update surface.

No ownership transition is needed: each change extends its current owner in the adapter
modules.

## Failure model

- **Actors and deployments:** a local operator through the CLI, and an MCP client under the
  existing tool authorization (ADR 0039 `all-targets` for the adds).
- **Invariants and assets:**
  - At the time of the pre-add check, the named remote is exactly one VIOS on the client's frame.
  - Adds still refuse an unowned client partition.
  - A read returns only an adapter the named partition owns, and never mutates.
- **Accepted failure classes:**
  - A VIOS removed, or its ID reused, between the check and the PUT. The window is two REST calls
    and needs an operator renumbering action. vSCSI has no HMC-side signal for it. vFC shows
    HSCLB023 only in the raw `NportLoggedInStatus` (#698).
  - A later HMC dropping the vFC instance GET (it is not in the method table). The read then
    fails as `HMCError`, and nothing is mutated.
- **Covered elsewhere:**
  - N_Port login/status (#698)
  - VIOS server adapters and mappings (#680)
  - REST0269/HSCLB021 classification (#1895)

### Threat model

- **Boundaries:**
  - Added: the `hmc_get_adapter` arguments. UUIDs enter the path through the existing UUID
    path-argument checks.
  - Widened: the `paired_identity` parse of HMC data. It parses only and builds no command or
    URL.
  - Added: the pre-add VIOS check takes the managed-system UUID from the HMC's
    `AssociatedManagedSystem` href and uses it in a GET path. It passes `is_uuid` and then
    `_reject_non_uuid_path_argument` first, so an href cannot inject path or query content.
- **Actors:** an MCP client is untrusted for authorization. The HMC is trusted for data, but its
  values are bounded by type checks.
- **Controls:**
  - The `@tool` read-effect authorization with `target_kind="lpar"`.
  - The `LocalPartitionID` parent check, so a caller scoped to partition A cannot read B's
    adapter through A's path.
  - The adds keep `resolve_and_authorize_lpar_mutation`.
- **Out of scope:** authorizing the VIOS. Nothing on it is written, and ADR 0039 scopes the adds.

## Success

1. The vFC create document carries `kb="CUR"` on both connecting fields.
2. `hmc_get_adapter` returns the entry plus `paired_identity` for both storage types. An unknown
   partition, an unknown adapter, a UUID mismatch and a foreign `LocalPartitionID` each raise
   the error named in Scope 2.
3. The add, get and list results for the two storage types carry `paired_identity`.
4. An add naming an ID that matches no VIOS, or more than one, on the client's frame is refused
   with no PUT sent.
5. The ledger rows are updated as Scope 5. `adapter.get` has a maturity record, operations
   entry, tool docs and changelog entry. `just verify` is green.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| vFC kb | focused-test | `tests/unit/test_documents_v10r3.py` RECORDED and `tests/storage/test_storage_tools.py` vFC body expect CUR |
| client instance path and type guard | focused-test | `tests/unit/test_client_adapters.py` |
| get errors, parent check, paired_identity, VIOS check | focused-test | new `tests/vios/test_storage_adapter_pairing.py` |
| MCP/CLI wiring, tool security | focused-test | `tests/storage/test_storage_tools.py`, `tests/app/test_tool_security.py`, `tests/app/test_cli_commands.py` |
| ledger, docs, vocabulary | focused-test | `just capability-inventory`, `just tool-docs-check`, `just doc-freshness`, `just live-vocabulary` |
| live behaviour | task-test-not-applicable | live runs are not a merge gate (ADR 0212); the procedure below defines the run |

### Live procedure (adapter.get; adds after the VIOS check)

**Prerequisites:**
- An owned, inactive test partition whose profile is applied.
- The VIOS's partition ID.
- A free client slot.
- Run only within the `docs/live-testing.md` boundary.

**Steps:**
1. Run `hmc_add_vscsi_adapter` and `hmc_add_vfc_adapter` against the VIOS ID. Each should
   succeed and return `paired_identity` with the remote ID and slot. The vFC result should carry
   two `wwpns`.
2. `hmc_get_adapter` on each new UUID should return the same identity.
3. `hmc_get_adapter` on a synthetic UUID should raise not-found.
4. Repeat the add naming the test partition's own ID. It should be refused, and a listing should
   show no new adapter.
5. Delete both adapters, and confirm both feeds and the VIOS `lshwres virtualio` rows match the
   baseline.

The existing ST9 arm still runs, because it passes the real VIOS ID.

**Gaps:**
- The active-partition vFC POST.
- A mapped or logged-in vFC.
- The vSCSI pairing signal.
