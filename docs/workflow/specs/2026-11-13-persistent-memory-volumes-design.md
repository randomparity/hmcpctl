# Virtual persistent-memory volumes (#643)

Decision: [ADR 0373](../../adr/0373-persistent-memory-volume-operations.md).

## Problem

The POWER10/POWER11 REST references document a `VirtualPersistentMemoryVolume`
resource under a managed system (capability row
`rest:managed-system/virtual-persistent-memory-volume`): a collection GET, a
create-and-assign PUT, a `?target=<partition id>` GET, a single-volume GET, a
rename POST and a DELETE. Nothing in `src/` implements it; the only use is
`operations/lpar/linux_roles.py` reading `CurrentPersistentMemoryVolumes`.

## Scope

One REST resource family, exposed through the existing operation, MCP and CLI
paths. Transport is REST only (ADR 0373). Excluded: pool lifecycle (#642),
DLPAR memory (#638), attaching to an active partition, the CLI `-r pmem`
transport.

Model, `documents/persistent_memory.py`:

- `PersistentMemoryVolume` (frozen): `uuid`, `name`, `size_mib`,
  `current_size_mib`, `volume_id`, `affinity`, `partition_id`,
  `partition_name`, `partition_uuid` (last three `None` when unassigned).
  `parse_volume(entry)` raises `ValueError` naming the field when `uuid`, `name`
  or `size_mib` is missing or malformed.
- `validate_volume_name(name)`: 1–64 characters of `[A-Za-z0-9_.-]`, a
  conservative subset applied to new names only (create, rename target), never
  to selectors of existing volumes; the HMC stays the final arbiter.
- `build_volume_document(name, size_mib, *, affinity, partition_id)`: the body of
  both create (PUT) and rename (POST). The live capture (Validation 9) pinned it:
  every element carries its `ksv` level or the HMC answers REST0001; the partition
  is referenced by `AssociatedPartitionId` (the `AssociatedPartition` link is
  read-only); and a rename must resend `Size` and `AssociatedPartitionId`, so rename
  sends the read volume's size, affinity and partition ID with the new name.

Operations, `operations/lpar/memory/persistent.py`. Every volume selector is a
UUID or an exact name; a name matching 0 or more than 1 volume raises.

| Operation | Function | Effect | Checks before any write |
|---|---|---|---|
| `pmem.list_volumes` | `list_persistent_memory_volumes(hmc, system, lpar=None)` | read | with `lpar`: the system collection filtered to the partition's `PartitionID` (a typed-Accept `?target=` was observed only on an empty collection) |
| `pmem.get_volume` | `get_persistent_memory_volume(hmc, system, volume)` | read | GET by UUID; a name resolves through the collection |
| `pmem.create_volume` | `create_persistent_memory_volume(hmc, system, lpar, name, size_mib, affinity=False, ownership_override=False)` | mutate | ADR 0092 guard; system `PersistentMemoryCapable` true and `dram` in `SupportedPersistentMemoryDeviceTypes`; `size_mib` a positive multiple of `DramPersistentMemoryVolumeBlockSize`; partition type `AIX/Linux`, state `not activated`; `CurrentDramPersistentMemoryVolumes` < `MaximumDramPersistentMemoryVolumes`; name valid and unused on the system |
| `pmem.rename_volume` | `rename_persistent_memory_volume(hmc, system, lpar, volume, new_name, ownership_override=False)` | mutate | ADR 0092 guard; volume assigned to `lpar`; new name valid and unused |
| `pmem.delete_volume` | `delete_persistent_memory_volume(hmc, system, lpar, volume, acknowledge_data_loss=False, ownership_override=False)` | destructive | `acknowledge_data_loss` is `True`; ADR 0092 guard; volume assigned to `lpar`; partition state `not activated` |

Admission reads system `AssociatedSystemCapabilities/PersistentMemoryCapable`
and `AssociatedPersistentMemoryConfiguration/{SupportedPersistentMemoryDeviceTypes,
DramPersistentMemoryVolumeBlockSize}`, and partition `PartitionID`,
`PartitionType`, `PartitionState` and
`AssociatedPersistentMemoryConfiguration/{Current,Maximum}DramPersistentMemoryVolumes`.
An absent, non-integer or non-positive value among these refuses as "not
advertised". A failed check raises `ValueError` ending "nothing was changed".
Each mutation submits once, never retries, then reads back (also after a write
error, so `observed` is filled when readable) and returns
`PersistentMemoryVolumeResult(status, requested, observed, warnings)`.
`status` is `verified`, `mismatch`, `readback-unavailable` or
`write-outcome-unknown` (vTPM vocabulary, ADR 0276). Create verifies exactly one
volume of that name, assigned to the partition, with the requested size and
affinity; rename verifies the UUID carries the new name; delete verifies the
UUID is absent from the collection. A readback that does not yet show the
change reports `mismatch` with a warning that an accepted (202) write may still
be completing, so the caller re-reads before retrying. The transport is five
`SystemsMixin` client methods over the captured paths: the collection, the single
volume, PUT create, POST rename and DELETE.

MCP tools in `server_tools/lpar/pmem.py`: `hmc_list_persistent_memory_volumes`
(`limit`), `hmc_get_persistent_memory_volume`,
`hmc_create_persistent_memory_volume`, `hmc_rename_persistent_memory_volume`,
`hmc_delete_persistent_memory_volume` (effect `destructive`; its docstring states
the volume's data is destroyed). CLI `lpars pmem-list|pmem-show|pmem-create|
pmem-rename|pmem-delete`; mutations confirm unless `--yes`; delete also requires
`--acknowledge-data-loss`. All five operations record `implemented` /
`unevidenced` maturity.

## Failure model

1. Actors and deployments
   - An MCP client under the server access policy; a local CLI operator; a
     `hmcpctl` Python caller. Against HMC V10R3/V11 REST on POWER10/POWER11.
2. Invariants and assets at stake
   - Volume contents: a delete is irreversible.
   - The partition's ownership token (ADR 0011/0092): no write to a
     foreign-owned partition without the audited override.
   - The inactive-partition precondition on create and delete.
   - Result honesty: no `verified` without matching readback.
3. Accepted failure classes
   - A check-then-write race (state, name, ownership changes between read and
     write): the HMC enforces inactivity and uniqueness itself; bounded.
   - Unassigned volumes cannot be renamed or deleted here: fail closed with a
     message; no ownership token exists to authorize against.
   - Rename on an active partition is not refused client-side; the reference
     does not forbid it, so the HMC decides.
   - A per-system volume maximum is not pre-checked; the HMC refuses.
4. Covered elsewhere
   - Access-policy dispatch and audit: ADR 0039, `tool_registry`.
   - Write headers and 406/415 handling: ADR 0178, `client/core.py`.
   - HMC error body sanitizing: ADR 0369.

### Threat model

- Boundaries added: five tool/CLI entry points taking a system, partition,
  volume selector, name and size; HMC XML responses parsed into the model.
  Widened: none.
- Actors: an MCP client bounded by the access policy; the HMC as a trusted but
  fallible peer.
- Controls: names validated by `validate_volume_name` and XML-escaped by
  `escapes_string_arguments`; UUIDs through `_request_with_uuid_path_arguments`;
  the partition filter compares an integer read from the HMC; sizes are
  strict integers; parsing fails closed; list output is bounded by `limit`.
- Out of scope: HMC-side authorization of the REST user (HMC owns it).

## Validation

Each entry is `focused-test` unless marked.

1. `parse_volume` maps a captured entry; a missing `uuid`/`name`/`size_mib`
   raises (`tests/lpar/test_persistent_memory.py`).
2. `validate_volume_name` accepts the grammar and rejects empty, 65-character
   and `<`/`,`/space names.
3. Builders emit the captured element shapes with escaped names.
4. Create refuses each precondition in the table with no PUT sent (respx
   asserts zero write calls), and returns each of the four statuses.
5. Rename and delete refuse a volume assigned to another partition, an
   unassigned volume, an ambiguous name; delete refuses without
   `acknowledge_data_loss` and on an active partition.
6. Listing by partition returns only volumes whose `AssociatedPartitionId` matches.
7. Tool security: effects, operations and target kinds
   (`tests/app/test_tool_security.py`); CLI mirrors and confirmation
   (`tests/app/test_cli_commands.py`); ADR 0092 inventory
   (`tests/unit/test_adr_0092_citations.py`).
8. Generated docs and capability ledgers: `just tool-docs-check`, `just test`.
9. `task-test-not-applicable`: the live capture of PUT, `?target=`, POST and
   DELETE wire shapes on a scratch inactive partition is evidence gathering,
   not a repository contract; its redacted bodies become the fixtures in 1–6.
   The repeatable procedure is `docs/workflow/persistent-memory-volume-live-procedure.md`.
