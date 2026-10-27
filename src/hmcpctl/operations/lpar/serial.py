"""Contained serial admission and independent saved configuration observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ...documents.lpar_serial import (
    VirtualSerialAdapter,
    serial_integer,
    serial_name,
    validate_serial_input,
)
from ...errors import HMCError
from ...resource_identity import is_uuid
from ...ssh.lpar import resolve_system_cli_name
from ...ssh.lpar_serial import SerialProfile, read_serial_profile
from ...xmlutil import leaf_text
from .ownership import authorize_lpar_ownership_description
from .workflow_contract import WorkflowStep

if TYPE_CHECKING:
    from ...client.core import HMCClient

PartitionKind = Literal["LogicalPartition", "VirtualIOServer"]


@dataclass(frozen=True)
class SerialTarget:
    kind: PartitionKind
    uuid: str
    name: str
    partition_id: int | None
    maximum: int | None
    profile_name: str | None


@dataclass(frozen=True)
class SerialAdmission:
    system_uuid: str
    system_name: str
    pending: SerialTarget
    adapters: tuple[VirtualSerialAdapter, ...]
    peers: tuple[SerialTarget | None, ...]
    preexisting: frozenset[str]


def _number(value: object, label: str) -> int:
    text = leaf_text(value)
    if not isinstance(text, str) or not text.isascii() or not text.isdecimal():
        raise ValueError(f"serial {label} is unavailable or malformed")
    return int(text)


def _target(kind: PartitionKind, entry: object) -> SerialTarget:
    if not isinstance(entry, dict) or entry.get("ResourceType") != kind:
        raise ValueError(
            "serial partition resource kind is unavailable or inconsistent"
        )
    uuid, resource = entry.get("UUID"), entry.get("Resource")
    if not isinstance(uuid, str) or not is_uuid(uuid) or not isinstance(resource, dict):
        raise ValueError("serial partition identity is unavailable or malformed")
    name = serial_name(leaf_text(resource.get("PartitionName")))
    native = _number(resource.get("PartitionID"), "partition ID")
    io = resource.get("PartitionIOConfiguration", {})
    if not isinstance(io, dict):
        raise TypeError("serial partition I/O configuration is malformed")
    maximum = (
        _number(io["MaximumVirtualIOSlots"], "maximum")
        if "MaximumVirtualIOSlots" in io
        else None
    )
    profile = leaf_text(resource.get("DefaultProfileName"))
    if profile is not None and not isinstance(profile, str):
        raise ValueError("serial default profile name is malformed")
    return SerialTarget(kind, uuid, name, native, maximum, profile or None)


async def _inventory(hmc: HMCClient, system: str) -> tuple[SerialTarget, ...]:
    targets: list[SerialTarget] = []
    for kind, rows in (
        ("LogicalPartition", await hmc.list_logical_partitions(system)),
        ("VirtualIOServer", await hmc.list_vios(system)),
    ):
        if not isinstance(rows, list) or len(rows) + len(targets) > 10000:
            raise ValueError(
                "serial partition inventory is malformed or exceeds 10000 rows"
            )
        targets.extend(_target(kind, entry) for entry in rows)
    for values in (
        [t.uuid.casefold() for t in targets],
        [t.name for t in targets],
        [t.partition_id for t in targets],
    ):
        if len(set(values)) != len(values):
            raise ValueError("serial partition inventory has duplicate identity")
    return tuple(targets)


def _same_identity(left: SerialTarget, right: SerialTarget) -> bool:
    return (
        left.kind == right.kind
        and left.uuid.casefold() == right.uuid.casefold()
        and left.name == right.name
        and left.partition_id == right.partition_id
    )


async def _read_target(
    hmc: HMCClient, target: SerialTarget
) -> tuple[SerialTarget, dict[str, Any]]:
    entry = await hmc.get_uom(target.kind, target.uuid)
    observed = _target(target.kind, entry)
    if not _same_identity(target, observed):
        raise ValueError("serial contained partition and resource read disagree")
    if observed.maximum is None or observed.maximum < 2:
        raise ValueError("serial selected partition maximum is unavailable")
    if (
        target.maximum is not None
        and observed.maximum != target.maximum
        or target.profile_name is not None
        and observed.profile_name != target.profile_name
    ):
        raise ValueError("serial selected partition configuration changed")
    assert isinstance(entry, dict)
    return observed, entry


async def admit_serial_creation(
    hmc: HMCClient,
    system_uuid: str,
    name: str,
    partition_id: int | None,
    maximum: int | None,
    adapters: tuple[VirtualSerialAdapter, ...],
    kind: PartitionKind,
) -> SerialAdmission:
    """Authorize every exact contained peer before the creation owner writes once."""
    validate_serial_input(adapters, maximum)
    serial_name(name)
    if kind not in {"LogicalPartition", "VirtualIOServer"} or not is_uuid(system_uuid):
        raise ValueError(
            "serial creation requires a managed-system UUID and partition kind"
        )
    if partition_id is not None:
        serial_integer(partition_id, "partition_id")
    inventory = await _inventory(hmc, system_uuid)
    if any(
        t.name == name or partition_id is not None and t.partition_id == partition_id
        for t in inventory
    ):
        raise ValueError("serial pending partition name or ID already exists")
    system_name = await resolve_system_cli_name(hmc.config, system_uuid)
    pending = SerialTarget(kind, "", name, partition_id, maximum, "default_profile")
    cache: dict[str, SerialTarget] = {}
    peers = []
    for adapter in adapters:
        selectors = []
        if adapter.remote_lpar_name is not None:
            selectors.append(
                pending
                if adapter.remote_lpar_name == name
                else next(
                    (t for t in inventory if t.name == adapter.remote_lpar_name), None
                )
            )
        if adapter.remote_lpar_id is not None:
            selectors.append(
                pending
                if partition_id is not None and adapter.remote_lpar_id == partition_id
                else next(
                    (t for t in inventory if t.partition_id == adapter.remote_lpar_id),
                    None,
                )
            )
        if any(t is None for t in selectors) or len(set(selectors)) > 1:
            raise ValueError(
                "serial endpoint selectors are missing, conflicting or ambiguous"
            )
        peer = selectors[0] if selectors else None
        if peer is not None:
            if peer == pending:
                if adapter.remote_slot == adapter.local_slot:
                    raise ValueError("serial same partition and same slot is invalid")
            else:
                key = peer.uuid.casefold()
                if key not in cache:
                    observed, resource_entry = await _read_target(hmc, peer)
                    description = leaf_text(
                        resource_entry["Resource"].get("Description", "")
                    )
                    if not isinstance(description, str):
                        raise ValueError(
                            "serial peer ownership description is malformed"
                        )
                    authorize_lpar_ownership_description(
                        hmc,
                        system_name,
                        observed.name,
                        description,
                        operation="lpar-mutation",
                    )
                    cache[key] = observed
                peer = cache[key]
            if adapter.remote_slot is not None and (
                peer.maximum is None or adapter.remote_slot >= peer.maximum
            ):
                raise ValueError(
                    "serial remote slot must be below selected endpoint maximum"
                )
        peers.append(peer)
    return SerialAdmission(
        system_uuid,
        system_name,
        pending,
        adapters,
        tuple(peers),
        frozenset(t.uuid.casefold() for t in inventory),
    )


async def confirm_serial_created(
    hmc: HMCClient, admission: SerialAdmission, candidate: object
) -> dict[str, Any]:
    """Confirm fresh contained local identity independently of submission's reply."""
    target = _target(admission.pending.kind, candidate)
    if (
        target.uuid.casefold() in admission.preexisting
        or target.name != admission.pending.name
        or admission.pending.partition_id is not None
        and target.partition_id != admission.pending.partition_id
    ):
        raise ValueError(
            "serial created identity is old or differs from requested identity"
        )
    inventory = await _inventory(hmc, admission.system_uuid)
    matches = [t for t in inventory if t.uuid.casefold() == target.uuid.casefold()]
    if len(matches) != 1 or not _same_identity(target, matches[0]):
        raise ValueError("serial created identity is not contained in selected system")
    _, entry = await _read_target(hmc, matches[0])
    return entry


def _points_at(fields: tuple[str, ...], target: SerialTarget, slot: int | None) -> bool:
    native, name, remote_slot = fields[3:6]
    return (
        (native not in {"", "any"} or bool(name))
        and (native in {"", "any"} or int(native) == target.partition_id)
        and (not name or name == target.name)
        and (slot is None or remote_slot == str(slot))
    )


def _server_accepts(fields: tuple[str, ...], client: SerialTarget, slot: int) -> bool:
    return (
        fields[1] == "server"
        and fields[2] in {"", "0"}
        and (fields[3] in {"", "any"} or int(fields[3]) == client.partition_id)
        and (not fields[4] or fields[4] == client.name)
        and fields[5] in {"", "any", str(slot)}
    )


def _matches_request(
    request: VirtualSerialAdapter, fields: tuple[str, ...], peer: SerialTarget | None
) -> bool:
    expected = request.native().split("/")
    if any(fields[i] != expected[i] for i in (0, 1, 6)):
        return False
    if request.supports_hmc is not None and fields[2] != expected[2]:
        return False
    if fields[2] not in {"", "0"}:
        return False
    if any(expected[i] and fields[i] != expected[i] for i in (3, 4, 5)):
        return False
    if peer is None:
        return fields[3] in {"", "any"} and not fields[4]
    return _points_at(fields, peer, request.remote_slot)


def _paired(
    request: VirtualSerialAdapter,
    local_fields: tuple[str, ...],
    local: SerialTarget,
    remote: SerialProfile,
) -> bool:
    candidates = [
        fields
        for slot, fields in remote.records.items()
        if slot >= 2 and (request.remote_slot is None or slot == request.remote_slot)
    ]
    return any(
        _server_accepts(fields, local, request.local_slot)
        if request.role == "client"
        else (
            fields[1] == "client"
            and fields[2] == ""
            and _points_at(fields, local, request.local_slot)
            and local_fields[5] in {"", "any", fields[0]}
        )
        for fields in candidates
    )


async def _profile(
    hmc: HMCClient,
    admission: SerialAdmission,
    target: SerialTarget,
    profile: str | None,
) -> SerialProfile:
    if not profile or target.partition_id is None:
        raise ValueError("serial saved-profile identity is unavailable")
    observed = await read_serial_profile(
        hmc.config, admission.system_name, target.name, target.partition_id, profile
    )
    if observed.maximum != target.maximum:
        raise ValueError("serial maximum observation is unavailable")
    return observed


async def serial_creation_step(
    hmc: HMCClient,
    admission: SerialAdmission,
    created: object,
    *,
    controller_mode: bool = False,
) -> WorkflowStep:
    """Keep configured, saved pair, current configuration and traffic as separate facts."""
    result: dict[str, Any] = {
        "configuration": "unverified",
        "plane": "current-unavailable" if controller_mode else "saved-default-profile",
        "current_configuration": "unavailable",
        "active_traffic": "unproven",
        "pairs": [
            {"slot": a.local_slot, "status": "unverified"} for a in admission.adapters
        ],
    }
    if controller_mode:
        return WorkflowStep("virtual_serial_adapters", "error", result)
    try:
        entry = await confirm_serial_created(hmc, admission, created)
        local = _target(admission.pending.kind, entry)
        if local.maximum != admission.pending.maximum:
            raise ValueError("serial created maximum differs from requested maximum")
        observed = await _profile(hmc, admission, local, "default_profile")
        if observed.other_virtual_configuration:
            raise ValueError(
                "serial exclusive local virtual-slot observation is unavailable"
            )
        expected_slots = {a.local_slot for a in admission.adapters}
        if {slot for slot in observed.records if slot >= 2} != expected_slots:
            raise ValueError(
                "serial created profile has missing or unexpected custom records"
            )
        resolved = tuple(
            local if peer == admission.pending else peer for peer in admission.peers
        )
        if not all(
            _matches_request(request, observed.records[request.local_slot], peer)
            for request, peer in zip(admission.adapters, resolved, strict=True)
        ):
            raise ValueError(
                "serial requested configuration does not match observation"
            )
        result["configuration"] = "verified"
        inventory = await _inventory(hmc, admission.system_uuid)
        cache = {local.uuid.casefold(): observed}
        for pair, request, peer in zip(
            result["pairs"], admission.adapters, resolved, strict=True
        ):
            status = "unbound"
            if peer is not None:
                key = peer.uuid.casefold()
                if key not in cache:
                    matches = [t for t in inventory if _same_identity(t, peer)]
                    if len(matches) != 1:
                        raise ValueError("serial selected peer is no longer contained")
                    fresh, _ = await _read_target(hmc, peer)
                    cache[key] = await _profile(
                        hmc, admission, fresh, fresh.profile_name
                    )
                status = (
                    "verified-saved-pair"
                    if _paired(
                        request, observed.records[request.local_slot], local, cache[key]
                    )
                    else "unverified"
                )
            pair["status"] = status
    except (HMCError, ValueError, TypeError, KeyError) as error:
        result["observation_error"] = type(error).__name__
        return WorkflowStep("virtual_serial_adapters", "error", result)
    complete = all(
        p["status"] in {"unbound", "verified-saved-pair"} for p in result["pairs"]
    )
    return WorkflowStep(
        "virtual_serial_adapters", "ok" if complete else "error", result
    )
