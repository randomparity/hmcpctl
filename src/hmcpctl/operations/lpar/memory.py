"""Guarded native current-memory quantities and independent outcomes (ADR 0285)."""

from __future__ import annotations

import re
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict

import asyncssh

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_memory import LparMemoryChange
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.composite import resource_container
from hmcpctl.operations.lpar.profiles.sync import ChangeLocation, change_location_of
from hmcpctl.resource_identity import (
    guard_partition,
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.memory import change_lpar_memory_quantity
from hmcpctl.xmlutil import leaf_text

from .ownership import authorize_lpar_mutation

_AIX_TYPES = frozenset({"AIX/Linux", "Virtual IO Server"})
_MEMORY_FIELDS = {
    "current_memory_mib": "CurrentMemory",
    "current_minimum_mib": "CurrentMinimumMemory",
    "current_maximum_mib": "CurrentMaximumMemory",
    "desired_memory_mib": "DesiredMemory",
    "minimum_memory_mib": "MinimumMemory",
    "maximum_memory_mib": "MaximumMemory",
    "runtime_memory_mib": "RuntimeMemory",
}


class _MemoryQuantities(TypedDict):
    current_memory_mib: int
    current_minimum_mib: int
    current_maximum_mib: int
    desired_memory_mib: int
    minimum_memory_mib: int
    maximum_memory_mib: int
    runtime_memory_mib: int


class _MemorySnapshot(_MemoryQuantities):
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


class _MemoryTargets(TypedDict):
    quantity_mib: int | None
    entitled_mib: int | Literal["auto"] | None
    reset: bool
    deferred: bool
    noop: bool


def _integer(value: object, label: str) -> int:
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


def _boolean(value: object, label: str) -> bool:
    text = leaf_text(value)
    if text is True or text in ("true", "1"):
        return True
    if text is False or text in ("false", "0"):
        return False
    raise ValueError(f"Memory change requires reported boolean {label}")


def _cpu_vector(resource: dict[str, Any]) -> dict[str, str | None]:
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


def _require_cpu_vector(vector: dict[str, str | None]) -> list[Decimal]:
    if not vector or any(value is None for value in vector.values()):
        raise ValueError(
            "Cannot verify coupled shutdown memory/processor reset: CPU assignments are missing"
        )
    try:
        values = [Decimal(value) for value in vector.values() if value is not None]
    except InvalidOperation as exc:
        raise ValueError(
            "Cannot verify coupled reset: malformed CPU assignments"
        ) from exc
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("Cannot verify coupled reset: invalid CPU assignments")
    return values


def _snapshot(entry: dict[str, Any] | None, uuid: str) -> _MemorySnapshot:
    if not entry or str(entry.get("UUID", "")).lower() != uuid.lower():
        raise ValueError("Memory readback has missing or different partition identity")
    resource = resource_container(entry, "Resource")
    memory = resource_container(resource, "PartitionMemoryConfiguration")
    partition_id = _integer(resource.get("PartitionID"), "PartitionID")
    if partition_id < 1:
        raise ValueError("Memory change requires positive PartitionID")
    values: _MemoryQuantities = {
        "current_memory_mib": _integer(memory.get("CurrentMemory"), "CurrentMemory"),
        "current_minimum_mib": _integer(
            memory.get("CurrentMinimumMemory"), "CurrentMinimumMemory"
        ),
        "current_maximum_mib": _integer(
            memory.get("CurrentMaximumMemory"), "CurrentMaximumMemory"
        ),
        "desired_memory_mib": _integer(memory.get("DesiredMemory"), "DesiredMemory"),
        "minimum_memory_mib": _integer(memory.get("MinimumMemory"), "MinimumMemory"),
        "maximum_memory_mib": _integer(memory.get("MaximumMemory"), "MaximumMemory"),
        "runtime_memory_mib": _integer(memory.get("RuntimeMemory"), "RuntimeMemory"),
    }
    entitled = leaf_text(memory.get("CurrentEntitledMemory"))
    runtime_entitled = leaf_text(memory.get("RuntimeEntitledMemory"))
    shared = _boolean(memory.get("SharedMemoryEnabled"), "SharedMemoryEnabled")
    state: _MemorySnapshot = {
        **values,
        "uuid": uuid,
        "name": resource.get("PartitionName"),
        "partition_id": partition_id,
        "state": resource.get("PartitionState"),
        "partition_type": resource.get("PartitionType"),
        "rmc": resource.get("ResourceMonitoringControlState"),
        "shared": shared,
        "entitled_mib": _integer(entitled, "CurrentEntitledMemory")
        if entitled not in (None, "auto")
        else "auto"
        if entitled == "auto"
        else None,
        "runtime_entitled_mib": _integer(runtime_entitled, "RuntimeEntitledMemory")
        if runtime_entitled not in (None, "auto")
        else "auto"
        if runtime_entitled == "auto"
        else None,
        "automatic_entitlement": _boolean(
            memory["AutoEntitledMemoryEnabled"], "AutoEntitledMemoryEnabled"
        )
        if "AutoEntitledMemoryEnabled" in memory
        else None,
        "dynamic_memory": leaf_text(
            resource_container(resource, "PartitionCapabilities").get(
                "DynamicLogicalPartitionMemoryCapable"
            )
        ),
        "operation_in_progress": _boolean(
            resource["IsOperationInProgress"], "IsOperationInProgress"
        )
        if "IsOperationInProgress" in resource
        else False,
        "processors": _cpu_vector(resource),
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


def _admit_state(before: _MemorySnapshot, change: LparMemoryChange) -> bool:
    if (
        before["state"] not in ("running", "not activated")
        or before["operation_in_progress"]
    ):
        raise ValueError(
            "Memory change requires a stable running or not activated partition"
        )
    kind = before["partition_type"]
    if kind not in _AIX_TYPES and kind != "OS400":
        raise ValueError("Memory change requires a supported reported partition type")
    if change.force and kind not in _AIX_TYPES:
        raise ValueError("force is supported only for AIX/Linux/VIOS memory changes")
    if before["state"] == "not activated":
        return True
    if before["current_memory_mib"] == 0:
        raise ValueError("Running partition reports no assigned memory")
    if kind in _AIX_TYPES and before["rmc"] != "active":
        if change.force:
            return True
        raise ValueError(
            "Running AIX/Linux/VIOS memory change requires active RMC or explicit force"
        )
    if before["dynamic_memory"] not in (True, "true", "1"):
        raise ValueError("Running partition does not report DLPAR memory capability")
    return False


def _target(current: int, operation: str, quantity: int) -> int:
    result = {"add": current + quantity, "remove": current - quantity, "set": quantity}[
        operation
    ]
    if result < 0:
        raise ValueError("Requested memory subtraction exceeds the current quantity")
    return result


def _targets(
    before: _MemorySnapshot, change: LparMemoryChange, region: int
) -> _MemoryTargets:
    deferred = _admit_state(before, change)
    quantity = change.quantity_mib
    noop = change.operation != "set" and quantity == 0 and change.entitled_mib is None
    target = (
        None
        if quantity is None
        else _target(before["current_memory_mib"], change.operation, quantity)
    )
    minimum, maximum = before["current_minimum_mib"], before["current_maximum_mib"]
    if maximum == 0 and before["state"] == "not activated":
        minimum, maximum = before["minimum_memory_mib"], before["maximum_memory_mib"]
    if (
        quantity is not None
        and target is not None
        and (region < 1 or quantity % region or target % region)
    ):
        raise ValueError(
            "Memory quantity and target must align to the system memory region size"
        )
    if target is not None and target > maximum:
        raise ValueError("Requested memory target exceeds the partition maximum")
    if (
        target is not None
        and 0 < target < minimum
        and target >= before["current_memory_mib"]
    ):
        raise ValueError("Requested memory growth is below the partition minimum")
    reset = target is not None and (
        (change.operation == "set" and target == 0)
        or target < before["current_memory_mib"]
        and target < minimum
    )
    if reset:
        if before["state"] != "not activated":
            raise ValueError("Running memory cannot be reduced below its minimum")
        _require_cpu_vector(before["processors"])
        target = 0
    entitled = change.entitled_mib
    if entitled is not None and not before["shared"]:
        raise ValueError("I/O entitlement is valid only for shared memory partitions")
    if type(entitled) is int:
        current = before["entitled_mib"]
        if change.operation != "set" and type(current) is not int:
            raise ValueError(
                "Numeric entitlement delta requires reported current entitlement"
            )
        entitled = _target(
            current if type(current) is int else 0, change.operation, entitled
        )
    return {
        "quantity_mib": target,
        "entitled_mib": entitled,
        "reset": reset,
        "deferred": deferred,
        "noop": noop,
    }


async def _admit_capacity(
    hmc: HMCClient,
    system_uuid: str,
    system_name: str,
    before: _MemorySnapshot,
    change: LparMemoryChange,
) -> _MemoryTargets:
    system = await hmc.get_managed_system(system_uuid)
    if not system or str(system.get("UUID", "")).lower() != system_uuid.lower():
        raise ValueError(
            "Memory capacity read returned a different or missing system identity"
        )
    resource = resource_container(system, "Resource")
    if (
        resource.get("SystemName") != system_name
        or resource.get("State") != "operating"
    ):
        raise ValueError(
            "Memory change requires the selected operating system identity"
        )
    memory = resource_container(resource, "AssociatedSystemMemoryConfiguration")
    targets = _targets(
        before, change, _integer(memory.get("MemoryRegionSize"), "MemoryRegionSize")
    )
    if before["shared"]:
        await _admit_pool(hmc, system_uuid, before, targets)
    elif targets["quantity_mib"] is not None:
        growth = max(0, targets["quantity_mib"] - before["current_memory_mib"])
        available = _integer(
            memory.get("CurrentAvailableSystemMemory"), "available system capacity"
        )
        total = _integer(
            memory.get("ConfigurableSystemMemory"), "ConfigurableSystemMemory"
        )
        if growth > available or targets["quantity_mib"] > total:
            raise ValueError("Requested memory exceeds available system capacity")
    return targets


async def _admit_pool(
    hmc: HMCClient, system_uuid: str, before: _MemorySnapshot, targets: _MemoryTargets
) -> None:
    pools = await hmc.list_child("ManagedSystem", system_uuid, "SharedMemoryPool")
    if len(pools) != 1:
        raise ValueError(
            "Shared memory change requires exactly one selected-system memory pool"
        )
    pool = resource_container(pools[0], "Resource")
    available = _integer(
        pool.get("CurrentAvailablePoolMemory"), "CurrentAvailablePoolMemory"
    )
    total = _integer(pool.get("CurrentPoolMemory"), "CurrentPoolMemory")
    _integer(pool.get("PoolID"), "PoolID")
    if available > total:
        raise ValueError("Shared memory pool capacity is contradictory")
    if type(targets["entitled_mib"]) is int:
        baseline = before["entitled_mib"]
        if type(baseline) is not int:
            raise ValueError(
                "Numeric entitlement capacity requires current entitlement"
            )
        if targets["entitled_mib"] - baseline > available:
            raise ValueError(
                "Requested I/O entitlement exceeds available pool capacity"
            )


def _postconditions(
    before: _MemorySnapshot, after: _MemorySnapshot, targets: _MemoryTargets
) -> bool:
    if any(
        before.get(key) != after.get(key)
        for key in ("uuid", "name", "partition_id", "state", "partition_type", "shared")
    ):
        raise ValueError("Memory readback target identity, state or mode changed")
    checks = [not after["operation_in_progress"]]
    if targets["quantity_mib"] is not None:
        checks.append(after["current_memory_mib"] == targets["quantity_mib"])
        if not targets["deferred"]:
            checks.append(after["runtime_memory_mib"] == targets["quantity_mib"])
    entitled = targets["entitled_mib"]
    if entitled is not None:
        if (
            after["automatic_entitlement"] is None
            or type(after["entitled_mib"]) is not int
        ):
            raise ValueError(
                "Memory entitlement readback requires numeric current value and automatic mode"
            )
        if not targets["deferred"]:
            if type(after["runtime_entitled_mib"]) is not int:
                raise ValueError(
                    "Memory entitlement readback requires numeric runtime value"
                )
            checks.append(after["runtime_entitled_mib"] == after["entitled_mib"])
        checks.append(after["automatic_entitlement"] is (entitled == "auto"))
        if type(entitled) is int:
            checks.append(after["entitled_mib"] == entitled)
            if not targets["deferred"]:
                checks.append(after["runtime_entitled_mib"] == entitled)
    if targets["reset"]:
        checks.extend(
            (after["current_minimum_mib"] == 0, after["current_maximum_mib"] == 0)
        )
        checks.append(
            all(value == 0 for value in _require_cpu_vector(after["processors"]))
        )
    return all(checks)


def _result(
    before: _MemorySnapshot,
    after: _MemorySnapshot | None,
    targets: _MemoryTargets,
    outcome: str,
    submission_error: str | None = None,
    read_error: str | None = None,
) -> dict[str, Any]:
    location = (after or before)["change_location"]
    warnings = (
        []
        if location.lives_in == "current-configuration-and-profile"
        else [location.summary()]
    )
    if targets["deferred"]:
        if before["state"] == "not activated":
            warnings.append(
                "No running-guest effect was verified; inactive current-configuration readback does not establish a later activation or restart."
            )
        else:
            warnings.append(
                "No running-guest effect was verified; a successful forced no-RMC change requires an operating-system restart."
            )
    if targets["reset"]:
        warnings.append(
            "Shutdown memory reset also clears processor assignments; inspect memory and CPU values before activation."
        )
    if read_error:
        warnings.append(read_error[:2048])
    return {
        "UUID": before["uuid"],
        "outcome": outcome,
        "workflow_completed": outcome in ("applied", "noop"),
        "before": {**before, "change_location": asdict(before["change_location"])},
        "after": {**after, "change_location": asdict(after["change_location"])}
        if after is not None
        else None,
        "expected": targets,
        "submission_error": submission_error,
        "change_location": asdict(location),
        "warnings": warnings,
    }


async def _contained_partition(
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


async def change_lpar_memory(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    change: LparMemoryChange,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Authorize, admit and submit one native quantity change, then independently read it.

    The existing absolute resources setter is a separate If-Match contract. This
    native operation never replays an uncertain submission or compensates partial effects.
    """
    if not isinstance(change, LparMemoryChange):
        raise TypeError("change must be a typed LparMemoryChange")
    if not system_name_or_uuid or not system_name_or_uuid.strip():
        raise ValueError("system_name_or_uuid is required for native memory quantities")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    kind, uuid = await _contained_partition(hmc, system_uuid, lpar_name_or_uuid)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    before = _snapshot(await read(uuid), uuid)
    if not is_uuid(lpar_name_or_uuid) and before["name"] != lpar_name_or_uuid:
        raise ValueError("Memory target name changed after containment resolution")
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if not system_name:
        raise ValueError("Memory target system has no unique CLI name")
    await authorize_lpar_mutation(
        hmc, system_name, before["name"], ownership_override=ownership_override
    )
    targets = await _admit_capacity(hmc, system_uuid, system_name, before, change)
    fresh = _snapshot(await read(uuid), uuid)
    if fresh != before:
        raise ValueError("Partition changed before memory submission; nothing was sent")
    if targets["noop"]:
        return _result(before, fresh, targets, "noop")
    error = None
    native_rejection = False
    try:
        await change_lpar_memory_quantity(
            hmc.config, system_name, before["partition_id"], change
        )
    except HMCError as exc:
        error = str(exc)[:2048]
        native_rejection = isinstance(exc.__cause__, asyncssh.ProcessError) and (
            exc.__cause__.exit_status is not None and exc.__cause__.exit_status != 0
        )
    try:
        if await _contained_partition(hmc, system_uuid, uuid) != (kind, uuid):
            raise ValueError("Memory readback partition kind or identity changed")
        after = _snapshot(await read(uuid), uuid)
        matched = _postconditions(before, after, targets)
    except (HMCError, ValueError) as exc:
        return _result(before, None, targets, "unverified", error, str(exc))
    if matched:
        outcome = "applied"
    elif after != before:
        outcome = "partial"
    else:
        outcome = "rejected" if native_rejection else "unverified"
    return _result(before, after, targets, outcome, error)
