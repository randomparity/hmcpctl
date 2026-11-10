"""Shared contained partition observations for memory and processor operations."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.profiles.sync import ChangeLocation, change_location_of
from hmcpctl.operations.resource_reading import resource_container
from hmcpctl.resource_identity import guard_partition, is_uuid
from hmcpctl.xmlutil import leaf_text

MEMORY_FIELDS = {
    "current_memory_mib": "CurrentMemory",
    "current_minimum_mib": "CurrentMinimumMemory",
    "current_maximum_mib": "CurrentMaximumMemory",
    "desired_memory_mib": "DesiredMemory",
    "minimum_memory_mib": "MinimumMemory",
    "maximum_memory_mib": "MaximumMemory",
    "runtime_memory_mib": "RuntimeMemory",
}


class MemoryQuantities(TypedDict):
    current_memory_mib: int
    current_minimum_mib: int
    current_maximum_mib: int
    desired_memory_mib: int
    minimum_memory_mib: int
    maximum_memory_mib: int
    runtime_memory_mib: int


class PartitionSnapshot(MemoryQuantities):
    uuid: str
    name: Any
    partition_id: int
    state: Any
    partition_type: Any
    rmc: Any
    shared: bool
    entitled_mib: int | Literal["auto"] | None
    runtime_entitled_mib: int | Literal["auto"] | None
    automatic_entitlement: bool | None
    dynamic_memory: Any
    operation_in_progress: bool
    processors: dict[str, str | None]
    change_location: ChangeLocation


def integer(value: object, label: str) -> int:
    text = leaf_text(value)
    if isinstance(text, bool) or text is None:
        raise ValueError(f"Memory change requires a valid {label}")
    if not re.fullmatch(r"\+?[0-9]+(?:\.0+)?", str(text)):
        raise ValueError(f"Memory change has malformed integral {label}")
    try:
        number = Decimal(str(text))
    except InvalidOperation as exc:
        raise ValueError(f"Memory change has malformed {label}") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError(f"Memory change requires nonnegative integral {label}")
    return int(number)


def boolean(value: object, label: str) -> bool:
    text = leaf_text(value)
    if text is True or text in ("true", "1"):
        return True
    if text is False or text in ("false", "0"):
        return False
    raise ValueError(f"Memory change requires reported boolean {label}")


def processor_assignments(resource: dict[str, Any]) -> dict[str, str | None]:
    config = resource_container(resource, "PartitionProcessorConfiguration")
    dedicated = leaf_text(
        config.get(
            "CurrentHasDedicatedProcessors", config.get("HasDedicatedProcessors")
        )
    )
    if dedicated in ("true", True):
        container = resource_container(config, "CurrentDedicatedProcessorConfiguration")
        names = (
            "CurrentMinimumProcessors",
            "CurrentProcessors",
            "CurrentMaximumProcessors",
        )
    elif dedicated in ("false", False):
        container = resource_container(config, "CurrentSharedProcessorConfiguration")
        names = (
            "CurrentMinimumProcessingUnits",
            "CurrentProcessingUnits",
            "CurrentMaximumProcessingUnits",
            "CurrentMinimumVirtualProcessors",
            "AllocatedVirtualProcessors",
            "CurrentMaximumVirtualProcessors",
        )
    else:
        return {}
    return {
        name: str(leaf_text(container[name])) if name in container else None
        for name in names
    }


def partition_snapshot(entry: dict[str, Any] | None, uuid: str) -> PartitionSnapshot:
    if not entry or str(entry.get("UUID", "")).lower() != uuid.lower():
        raise ValueError("Memory readback has missing or different partition identity")
    resource = resource_container(entry, "Resource")
    memory = resource_container(resource, "PartitionMemoryConfiguration")
    partition_id = integer(resource.get("PartitionID"), "PartitionID")
    if partition_id < 1:
        raise ValueError("Memory change requires positive PartitionID")
    values: MemoryQuantities = {
        "current_memory_mib": integer(memory.get("CurrentMemory"), "CurrentMemory"),
        "current_minimum_mib": integer(
            memory.get("CurrentMinimumMemory"), "CurrentMinimumMemory"
        ),
        "current_maximum_mib": integer(
            memory.get("CurrentMaximumMemory"), "CurrentMaximumMemory"
        ),
        "desired_memory_mib": integer(memory.get("DesiredMemory"), "DesiredMemory"),
        "minimum_memory_mib": integer(memory.get("MinimumMemory"), "MinimumMemory"),
        "maximum_memory_mib": integer(memory.get("MaximumMemory"), "MaximumMemory"),
        "runtime_memory_mib": integer(memory.get("RuntimeMemory"), "RuntimeMemory"),
    }
    entitled = leaf_text(memory.get("CurrentEntitledMemory"))
    runtime_entitled = leaf_text(memory.get("RuntimeEntitledMemory"))
    shared = boolean(memory.get("SharedMemoryEnabled"), "SharedMemoryEnabled")
    state: PartitionSnapshot = {
        **values,
        "uuid": uuid,
        "name": resource.get("PartitionName"),
        "partition_id": partition_id,
        "state": resource.get("PartitionState"),
        "partition_type": resource.get("PartitionType"),
        "rmc": resource.get("ResourceMonitoringControlState"),
        "shared": shared,
        "entitled_mib": integer(entitled, "CurrentEntitledMemory")
        if entitled not in (None, "auto")
        else "auto"
        if entitled == "auto"
        else None,
        "runtime_entitled_mib": integer(runtime_entitled, "RuntimeEntitledMemory")
        if runtime_entitled not in (None, "auto")
        else "auto"
        if runtime_entitled == "auto"
        else None,
        "automatic_entitlement": boolean(
            memory["AutoEntitledMemoryEnabled"], "AutoEntitledMemoryEnabled"
        )
        if "AutoEntitledMemoryEnabled" in memory
        else None,
        "dynamic_memory": leaf_text(
            resource_container(resource, "PartitionCapabilities").get(
                "DynamicLogicalPartitionMemoryCapable"
            )
        ),
        "operation_in_progress": boolean(
            resource["IsOperationInProgress"], "IsOperationInProgress"
        )
        if "IsOperationInProgress" in resource
        else False,
        "processors": processor_assignments(resource),
        "change_location": change_location_of(entry),
    }
    for minimum, desired, maximum in [
        (
            values["current_minimum_mib"],
            values["current_memory_mib"],
            values["current_maximum_mib"],
        ),
        (
            values["minimum_memory_mib"],
            values["desired_memory_mib"],
            values["maximum_memory_mib"],
        ),
    ]:
        if not minimum <= desired <= maximum:
            raise ValueError(
                "Memory readback has contradictory minimum/current/maximum bounds"
            )
    return state


async def contained_partition(
    hmc: HMCClient, system_uuid: str, selector: str
) -> tuple[str, str]:
    matches = []
    for kind, entries in (
        ("LogicalPartition", await hmc.list_logical_partitions(system_uuid)),
        ("VirtualIOServer", await hmc.list_vios(system_uuid)),
    ):
        for entry in entries:
            uuid = entry.get("UUID")
            name = resource_container(entry, "Resource").get("PartitionName")
            if (
                is_uuid(selector)
                and isinstance(uuid, str)
                and uuid.lower() == selector.lower()
            ) or (not is_uuid(selector) and name == selector):
                if not isinstance(uuid, str) or not is_uuid(uuid):
                    raise ValueError(
                        "Memory target has missing or malformed partition UUID"
                    )
                matches.append((kind, uuid))
    if len(matches) != 1:
        raise ValueError(
            "Memory target is missing, ambiguous or outside the selected system"
        )
    if matches[0][0] == "LogicalPartition":
        guard_partition(matches[0][1])
    return matches[0]
