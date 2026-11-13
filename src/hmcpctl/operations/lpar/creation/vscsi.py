"""vSCSI client admission and saved-profile comparison at creation (ADR 0376).

The remote VIOS is resolved, typed and authorized before the single native create.
Nothing here writes the VIOS: no server adapter or mapping is created, so the result
never claims pairing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ....documents.lpar_serial import (
    VirtualSerialAdapter,
    serial_integer,
    serial_name,
)
from ....documents.lpar_vscsi import VirtualScsiClientAdapter, validate_vscsi_input
from ....errors import HMCError, HMCTLSFingerprintError
from ....resource_identity import is_uuid
from ....ssh.lpar import resolve_system_cli_name
from ....ssh.lpar_vscsi import read_vscsi_profile
from ..workflow_contract import WorkflowStep
from .serial import (
    SerialTarget,
    _inventory,
    _target,
    confirm_serial_created,
    read_owned_target,
)

if TYPE_CHECKING:
    from ....client.core import HMCClient

VSCSI_FAMILY = "virtual_scsi_adapters"


@dataclass(frozen=True)
class VscsiAdmission:
    system_uuid: str
    system_name: str
    pending: SerialTarget
    adapters: tuple[VirtualScsiClientAdapter, ...]
    servers: tuple[SerialTarget, ...]
    preexisting: frozenset[str]
    # Virtual tagged roles bound to declared local slots (ADR 0382).
    roles: tuple[tuple[str, int], ...] = ()


def _selected(
    adapter: VirtualScsiClientAdapter, inventory: tuple[SerialTarget, ...]
) -> SerialTarget:
    """Resolve the one selector; the pending partition is absent from the inventory."""
    if adapter.remote_lpar_name is not None:
        matches = [t for t in inventory if t.name == adapter.remote_lpar_name]
    else:
        matches = [t for t in inventory if t.partition_id == adapter.remote_lpar_id]
    if len(matches) != 1:
        raise ValueError(
            "vSCSI remote partition is not contained in the selected system"
        )
    if matches[0].kind != "VirtualIOServer":
        raise ValueError("vSCSI remote partition is not a VIOS on the selected system")
    return matches[0]


async def admit_vscsi_creation(
    hmc: HMCClient,
    system_uuid: str,
    name: str,
    partition_id: int | None,
    maximum: int | None,
    adapters: tuple[VirtualScsiClientAdapter, ...],
    serial: tuple[VirtualSerialAdapter, ...] = (),
    roles: tuple[tuple[str, int], ...] = (),
) -> VscsiAdmission:
    """Resolve and authorize every remote VIOS before the creation owner writes once."""
    validate_vscsi_input(adapters, maximum, serial)
    serial_name(name)
    if not is_uuid(system_uuid):
        raise ValueError("vSCSI creation requires a managed-system UUID")
    if partition_id is not None:
        serial_integer(partition_id, "partition_id")
    inventory = await _inventory(hmc, system_uuid)
    if any(
        t.name == name or partition_id is not None and t.partition_id == partition_id
        for t in inventory
    ):
        raise ValueError("vSCSI pending partition name or ID already exists")
    system_name = await resolve_system_cli_name(hmc.config, system_uuid)
    pending = SerialTarget(
        "LogicalPartition", "", name, partition_id, maximum, "default_profile"
    )
    cache: dict[str, SerialTarget] = {}
    servers = []
    for adapter in adapters:
        selected = _selected(adapter, inventory)
        key = selected.uuid.casefold()
        if key not in cache:
            cache[key] = await read_owned_target(hmc, system_name, selected)
        server = cache[key]
        if server.maximum is None or adapter.remote_slot >= server.maximum:
            raise ValueError("vSCSI remote slot must be below the VIOS maximum")
        servers.append(server)
    return VscsiAdmission(
        system_uuid,
        system_name,
        pending,
        adapters,
        tuple(servers),
        frozenset(t.uuid.casefold() for t in inventory),
        roles,
    )


def _matches(
    request: VirtualScsiClientAdapter, fields: tuple[str, ...], server: SerialTarget
) -> bool:
    _, role, native_id, name, remote_slot, required = fields
    if native_id and (
        not native_id.isdecimal() or int(native_id) != server.partition_id
    ):
        return False
    return (
        role == "client"
        and bool(native_id or name)
        and (not name or name == server.name)
        and remote_slot == str(request.remote_slot)
        and required == str(int(request.is_required))
    )


def _record(slot: int, roles: tuple[tuple[str, int], ...]) -> dict[str, Any]:
    record: dict[str, Any] = {"slot": slot, "status": "unverified"}
    names = [name for name, bound in roles if bound == slot]
    if names:
        record["roles"] = names
    return record


async def vscsi_creation_step(
    hmc: HMCClient,
    admission: VscsiAdmission,
    created: object,
    *,
    controller_mode: bool = False,
) -> WorkflowStep:
    """Compare the saved client records; state what creation never did to the VIOS."""
    result: dict[str, Any] = {
        "configuration": "unverified",
        "plane": "current-unavailable" if controller_mode else "saved-default-profile",
        "vios_server_adapters": "not-created",
        "vios_mappings": "not-created",
        "pairing": "unproven",
        "records": [_record(a.local_slot, admission.roles) for a in admission.adapters],
    }
    if controller_mode:
        return WorkflowStep(VSCSI_FAMILY, "error", result)
    try:
        entry = await confirm_serial_created(hmc, admission, created)
        local = _target(admission.pending.kind, entry)
        if local.partition_id is None:
            raise ValueError("vSCSI created partition ID is unavailable")
        observed = await read_vscsi_profile(
            hmc.config,
            admission.system_name,
            local.name,
            local.partition_id,
            "default_profile",
        )
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, TypeError, KeyError) as error:
        result["observation_error"] = type(error).__name__
        return WorkflowStep(VSCSI_FAMILY, "error", result)
    for record, request, server in zip(
        result["records"], admission.adapters, admission.servers, strict=True
    ):
        fields = observed.records.get(request.local_slot)
        bound = all(
            observed.attributes.get(role) == str(request.local_slot)
            for role in record.get("roles", ())
        )
        record["status"] = (
            "verified"
            if fields is not None and _matches(request, fields, server) and bound
            else "mismatch"
        )
    requested = {a.local_slot for a in admission.adapters}
    verified = set(observed.records) == requested and all(
        r["status"] == "verified" for r in result["records"]
    )
    result["configuration"] = "verified" if verified else "unverified"
    return WorkflowStep(VSCSI_FAMILY, "ok" if verified else "error", result)
