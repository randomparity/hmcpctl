"""Fresh C12 creation admission and contained native-profile postconditions."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from ...documents.lpar_io import LparPhysicalIO
from ...errors import HMCError
from ...ssh.io_inventory import list_io_slots
from ...ssh.lpar import complete_create_resources, resolve_system_cli_name
from ...ssh.lpar_io import creation_io_record
from ...ssh.profiles import parse_profile_io_slots, read_profile_io_slot_rows
from ...ssh.sriov import list_sriov_unconfigured_logical_port_rows
from ...xmlutil import leaf_text
from ..systems.fleet import read_system_entry
from .assignments import (
    LparPcieAssignments,
    SriovLogicalPortAssignment,
    prevalidate_lpar_pcie_assignments,
)
from .profiles import read_partition_profile
from .workflow_contract import WorkflowStep

if TYPE_CHECKING:
    from ...client.core import HMCClient
    from .core import LparCreation

_TAGS = {
    "load_source_slot": "LoadSource",
    "alt_restart_device_slot": "AlternateLoadSource",
    "console_slot": "Console",
    "alt_console_slot": "AlternateConsole",
    "op_console_slot": "OperationsConsole",
}


async def admit_creation_io(
    hmc: HMCClient, system_uuid: str, creation: LparCreation
) -> None:
    """Reject unknown or occupied identities before native creation is dispatched."""
    io = creation.physical_io
    if not isinstance(creation.creation_sriov, tuple) or any(
        not isinstance(p, SriovLogicalPortAssignment) for p in creation.creation_sriov
    ):
        raise TypeError("creation_sriov must be a tuple of SriovLogicalPortAssignment")
    creation_io_record(io, creation.creation_sriov)
    if not io.requested:
        if creation.creation_sriov:
            raise ValueError("creation_sriov requires explicit physical_io fields")
        return
    if (
        creation.keylock is not None
        or complete_create_resources(creation.resources) is None
    ):
        raise ValueError(
            "physical_io creation requires explicit resources and no keylock; nothing created"
        )
    resource: dict[str, Any] = {}
    if io.tagged() or io.virtual_opti_pool_id is not None:
        if creation.partition_type != "OS400":
            raise ValueError("tagged I/O and virtual OptiConnect require OS400")
        system = await read_system_entry(hmc, system_uuid)
        resource = (system or {}).get("Resource", {})
        if not isinstance(resource, dict):
            raise ValueError("managed-system capability resource is malformed")
        capabilities = resource.get("AssociatedSystemCapabilities", {})
        if not isinstance(capabilities, dict):
            raise ValueError("managed-system capability container is malformed")
        if leaf_text(capabilities.get("IBMiCapable")) != "true":
            raise ValueError("fresh IBM i capability is unavailable; nothing created")
    system = await resolve_system_cli_name(hmc.config, system_uuid)
    if io.io_slots or io.lpar_io_pool_ids:
        await _admit_physical(hmc, system, io)
    if creation.creation_sriov:
        await prevalidate_lpar_pcie_assignments(
            hmc, system_uuid, LparPcieAssignments(sriov=creation.creation_sriov)
        )
        await _admit_sriov(hmc, system, creation.creation_sriov)
    physical = {s.drc_index: s for s in io.io_slots}
    logical = {s.logical_port_id for s in creation.creation_sriov}
    for name, value in io.tagged():
        if name == "console_slot" and value == "hmc":
            continue
        if value in physical:
            _require_tagged_role(resource, name, value)
            if not physical[value].is_required:
                raise ValueError(
                    f"tagged physical slot {name} must be declared required"
                )
        elif value not in logical or name in {"alt_console_slot", "op_console_slot"}:
            raise ValueError(
                f"{name} must reference a declared eligible creation device"
            )


def _require_tagged_role(resource: dict[str, Any], name: str, index: str) -> None:
    capabilities = resource.get("AssociatedSystemCapabilities", {})
    if (
        not isinstance(capabilities, dict)
        or leaf_text(capabilities.get("IBMiNativeIOCapable")) != "true"
    ):
        raise ValueError("IBM i native I/O capability is unavailable")
    io = resource.get("AssociatedSystemIOConfiguration", {})
    slots = io.get("IOSlots", {}) if isinstance(io, dict) else {}
    rows = _objects(slots.get("IOSlot")) if isinstance(slots, dict) else []
    matches = [
        r
        for r in rows
        if _integer(r.get("SlotDynamicReconfigurationConnectorIndex")) == int(index, 16)
    ]
    if len(matches) != 1:
        raise ValueError("tagged physical device capability is missing or ambiguous")
    ibmi = matches[0].get("RelatedIBMiIOSlot", {})
    role = {
        "load_source_slot": "LoadSourceCapable",
        "alt_restart_device_slot": "LoadSourceCapable",
        "console_slot": "ConsoleCapable",
        "alt_console_slot": "ConsoleCapable",
        "op_console_slot": "OperationsConsoleCapable",
    }[name]
    if (
        not isinstance(ibmi, dict)
        or leaf_text(ibmi.get("IOPInfoStale")) != "false"
        or leaf_text(ibmi.get(role)) != "true"
    ):
        raise ValueError(f"fresh tagged device {role} capability is unavailable")


async def _admit_physical(hmc: HMCClient, system: str, io: LparPhysicalIO) -> None:
    rows = await list_io_slots(hmc.config, system)
    if len(rows) > 10000:
        raise ValueError("physical slot inventory exceeds 10000 rows")
    pools = {r.get("slot_io_pool_id") for r in rows if isinstance(r, dict)}
    if not set(io.lpar_io_pool_ids) <= pools:
        raise ValueError(
            "partition pool membership is not established by fresh system inventory"
        )
    profiles = await read_profile_io_slot_rows(hmc.config, system)
    held = {
        s.drc_index for p in profiles for s in parse_profile_io_slots(p["io_slots"])
    }
    for slot in io.io_slots:
        matches = [r for r in rows if r.get("drc_index") == slot.drc_index]
        if len(matches) != 1:
            raise ValueError("creation slot is missing or ambiguous on selected system")
        row = matches[0]
        if (
            row.get("lpar_id") != "none"
            or row.get("lpar_name") not in {None, "none", ""}
            or slot.drc_index in held
        ):
            raise ValueError("creation slot is in use or listed by an existing profile")
        if row.get("lpar_assignment_capable") != "1" or row.get("pci_vendor_id") in {
            None,
            "FFFF",
        }:
            raise ValueError("creation slot is empty or not assignment capable")
        expected_pool = slot.pool_id if slot.pool_id is not None else "none"
        if row.get("slot_io_pool_id") != expected_pool:
            raise ValueError("creation slot pool differs from fresh system membership")
        if slot.pool_id is not None and slot.pool_id not in io.lpar_io_pool_ids:
            raise ValueError(
                "slot pool must be included in partition I/O pool membership"
            )


async def _admit_sriov(
    hmc: HMCClient, system: str, ports: tuple[SriovLogicalPortAssignment, ...]
) -> None:
    rows = await list_sriov_unconfigured_logical_port_rows(hmc.config, system)
    for port in ports:
        matches = [
            r
            for r in rows
            if r.get("adapter_id") == port.adapter_id
            and r.get("logical_port_id") == port.logical_port_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "SR-IOV logical DRC is not uniquely unconfigured on selected adapter"
            )
        # Existing normalized inventory derives physical identity from the captured
        # logical-port location and rejects ambiguous parents.
        from ..virtualization.pcie import list_sriov_logical_ports

        inventory = await list_sriov_logical_ports(
            hmc, system, port.adapter_id, port.physical_port_id
        )
        if (
            inventory.capability != "available"
            or sum(
                p.logical_port_id == port.logical_port_id
                and p.availability == "unconfigured"
                for p in inventory.items
            )
            != 1
        ):
            raise ValueError(
                "SR-IOV logical DRC does not belong to selected physical port"
            )


def _objects(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if len(values) > 10000 or any(not isinstance(v, dict) for v in values):
        raise ValueError("native profile collection is malformed or oversized")
    return values


def _integer(value: object) -> int:
    text = leaf_text(value)
    if not isinstance(text, str) or not text.isascii() or not text.isdecimal():
        raise ValueError("native profile integer is unavailable or malformed")
    return int(text)


def _boolean(value: object) -> bool:
    text = leaf_text(value)
    if not isinstance(text, str) or text not in {"true", "false", "1", "0"}:
        raise ValueError("native profile boolean is unavailable or malformed")
    return text in {"true", "1"}


def compare_creation_io(
    io: LparPhysicalIO,
    ports: tuple[SriovLogicalPortAssignment, ...],
    profile: dict[str, Any],
) -> bool:
    """Compare explicit fields against their retained native XSD representations."""
    config = profile.get("IOConfigurationInstance")
    if not isinstance(config, dict):
        return False
    if io.io_slots:
        container = config.get("ProfileIOSlots")
        if not isinstance(container, dict):
            return False
        observed = {}
        for item in _objects(container.get("ProfileIOSlot")):
            associated = item.get("AssociatedIOSlot")
            if not isinstance(associated, dict):
                return False
            index = _integer(associated.get("SlotDynamicReconfigurationConnectorIndex"))
            if index in observed:
                raise ValueError("native profile repeats a physical DRC")
            observed[index] = (
                _integer(item["PoolID"]) if "PoolID" in item else None,
                _boolean(item.get("IsRequired")),
            )
        if observed != {
            int(s.drc_index, 16): (
                int(s.pool_id) if s.pool_id is not None else None,
                s.is_required,
            )
            for s in io.io_slots
        }:
            return False
    if io.lpar_io_pool_ids:
        pools = config.get("PoolIDs")
        values = pools if isinstance(pools, list) else [pools]
        if sorted(_integer(p) for p in values) != sorted(
            int(p) for p in io.lpar_io_pool_ids
        ):
            return False
    tagged = config.get("TaggedIO", {})
    if not isinstance(tagged, dict):
        return False
    for name, value in io.tagged():
        actual = leaf_text(tagged.get(_TAGS[name]))
        if actual is None:
            return False
        if value == "hmc":
            if actual != "HMC":
                return False
        elif _integer(actual) != int(value, 16):
            return False
    if io.virtual_opti_pool_id is not None and _boolean(
        config.get("VirtualOpticonnectPool")
    ) != bool(io.virtual_opti_pool_id):
        return False
    if ports:
        container = profile.get("ProfileSRIOVEthernetLogicalPorts")
        if not isinstance(container, dict):
            return False
        observed_ports = []
        for p in _objects(container.get("ProfileSRIOVEthernetLogicalPort")):
            observed_ports.append(
                (
                    _integer(p.get("LogicalPortID")),
                    _integer(p.get("AdapterID")),
                    _integer(p.get("PhysicalPortID")),
                    Decimal(str(leaf_text(p.get("ConfiguredCapacityPercentage")))),
                )
            )
        if sorted(observed_ports) != sorted(
            (
                int(p.logical_port_id, 16),
                int(p.adapter_id),
                int(p.physical_port_id),
                p.capacity_percent,
            )
            for p in ports
        ):
            return False
    return True


async def created_io_step(
    hmc: HMCClient, system_uuid: str, creation: LparCreation, created: dict[str, Any]
) -> WorkflowStep | None:
    if not creation.physical_io.requested:
        return None
    try:
        profile = await read_partition_profile(
            hmc, system_uuid, created["UUID"], "default_profile"
        )
        matched = compare_creation_io(
            creation.physical_io, creation.creation_sriov, profile.fields
        )
    except (HMCError, ValueError, TypeError, KeyError, InvalidOperation) as error:
        return WorkflowStep(
            "physical_io",
            "error",
            f"Partition may exist; native profile I/O unverified: {error}. Inspect before retrying; no rollback performed.",
        )
    return WorkflowStep(
        "physical_io",
        "ok" if matched else "error",
        "Configured profile I/O verified; activation/guest behavior unproven"
        if matched
        else "Partition exists; native profile I/O mismatch. Inspect before retrying; no rollback performed.",
    )
