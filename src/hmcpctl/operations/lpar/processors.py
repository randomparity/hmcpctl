"""Guarded native current-processor quantities and independent outcomes (ADR 0296)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import asyncssh

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_processors import LparProcessorChange
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.composite import resource_container
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.processors import (
    change_lpar_processor_quantity,
    read_lpar_processor_quantities,
)
from hmcpctl.xmlutil import leaf_text

from . import memory
from .ownership import authorize_lpar_mutation
from .profile_sync import ChangeLocation

_OS = {
    "AIX/Linux": "AIXOrLinux",
    "OS400": "IBMi",
    "Virtual IO Server": "VirtualIOServer",
}
_MEMORY = ("current_minimum_mib", "current_memory_mib", "current_maximum_mib")


def _number(value: object, label: str, *, whole: bool = False) -> Decimal | int:
    number = LparProcessorChange.exact_units(leaf_text(value))
    if number is None or (whole and number != number.to_integral_value()):
        raise ValueError(f"Processor change requires valid {label}")
    if number.is_zero():
        number = Decimal(0)
    return int(number) if whole else number


def _mode(value: object) -> str:
    value = leaf_text(value)
    if value in (True, "true", "1"):
        return "ded"
    if value in (False, "false", "0"):
        return "shared"
    raise ValueError("Processor change requires explicit processor mode")


def _snapshot(
    entry: dict[str, Any] | None, uuid: str, row: dict[str, str]
) -> dict[str, Any]:
    before = memory._snapshot(entry, uuid)
    resource = resource_container(entry or {}, "Resource")
    progress = leaf_text(resource.get("IsOperationInProgress"))
    if progress not in (True, False, "true", "false", "1", "0"):
        raise ValueError(
            "Processor change requires explicit operation-in-progress evidence"
        )
    before["operation_in_progress"] = progress in (True, "true", "1")
    config = resource_container(resource, "PartitionProcessorConfiguration")
    mode = _mode(config.get("CurrentHasDedicatedProcessors"))
    if (
        row.get("curr_proc_mode") != mode
        or row.get("pend_proc_mode") != mode
        or _mode(config.get("HasDedicatedProcessors")) != mode
    ):
        raise ValueError("Current and pending processor modes disagree")
    if (
        row.get("lpar_id") != str(before["partition_id"])
        or row.get("lpar_name") != before["name"]
    ):
        raise ValueError("Processor read identity differs from contained partition")
    before.update(
        processor_mode=mode,
        dynamic_processor=leaf_text(
            resource_container(resource, "PartitionCapabilities").get(
                "DynamicLogicalPartitionProcessorCapable"
            )
        ),
    )
    dedicated = mode == "ded"
    current = resource_container(
        config,
        "CurrentDedicatedProcessorConfiguration"
        if dedicated
        else "CurrentSharedProcessorConfiguration",
    )
    pending = resource_container(
        config,
        "DedicatedProcessorConfiguration"
        if dedicated
        else "SharedProcessorConfiguration",
    )
    for prefix, native, container in [
        ("current", "curr", current),
        ("pending", "pend", pending),
    ]:
        for quantity, suffix, names in [
            (
                "processors",
                "procs",
                ("MinimumProcessors", "Processors", "MaximumProcessors")
                if dedicated
                else (
                    "MinimumVirtualProcessors",
                    "VirtualProcessors",
                    "MaximumVirtualProcessors",
                ),
            ),
            (
                "processing_units",
                "proc_units",
                ("MinimumProcessingUnits", "ProcessingUnits", "MaximumProcessingUnits"),
            ),
        ]:
            if dedicated and quantity == "processing_units":
                continue
            values = []
            for bound, field in zip(("minimum_", "", "maximum_"), names, strict=True):
                native_field = f"{native}_{'min_' if bound == 'minimum_' else 'max_' if bound else ''}{suffix}"
                rest_field = (
                    "Current" + field
                    if prefix == "current"
                    else field
                    if bound
                    else "Desired" + field
                )
                if (
                    prefix == "current"
                    and not bound
                    and quantity == "processors"
                    and not dedicated
                ):
                    rest_field = "AllocatedVirtualProcessors"
                value = _number(
                    row.get(native_field), native_field, whole=quantity == "processors"
                )
                if value != _number(
                    container.get(rest_field),
                    rest_field,
                    whole=quantity == "processors",
                ):
                    raise ValueError("REST and native processor quantities disagree")
                before[f"{prefix}_{bound}{quantity}"] = value
                values.append(value)
            if not values[0] <= values[1] <= values[2]:
                raise ValueError(
                    "Processor minimum/current/maximum bounds are contradictory"
                )
    before["runtime_processors"] = _number(
        row.get("run_procs"), "run_procs", whole=True
    )
    before["runtime_processing_units"] = (
        None if dedicated else _number(row.get("run_proc_units"), "run_proc_units")
    )
    # The inherited vector participates in equality too; XML decimal spelling
    # must not turn unchanged quantities into a partial outcome.
    before["processors"] = {
        name: f"{_number(value, name):.2f}"
        if "Units" in name
        else str(_number(value, name, whole=True))
        for name, value in before["processors"].items()
    }
    return before


def _capacity(system: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    resource = resource_container(system, "Resource")
    if resource.get("State") != "operating":
        raise ValueError("Processor change requires an operating managed system")
    config = resource_container(resource, "AssociatedSystemProcessorConfiguration")
    kind = _OS.get(before["partition_type"])
    if kind is None:
        raise ValueError(
            "Processor change requires a supported reported partition type"
        )
    dedicated = before["processor_mode"] == "ded"
    maximum = (
        f"CurrentMaximum{'' if dedicated else 'Virtual'}ProcessorsPer{kind}Partition"
    )
    fields = {
        "total": "ConfigurableSystemProcessorUnits",
        "available": "CurrentAvailableSystemProcessorUnits",
        "count_limit": "CurrentMaximumAllowedProcessorsPerPartition"
        if dedicated
        else "MaximumAllowedVirtualProcessorsPerPartition",
        "os_limit": maximum,
    }
    if not dedicated:
        fields["floor"] = "MinimumProcessorUnitsPerVirtualProcessor"
    values = {
        key: _number(config.get(field), field, whole=key in ("count_limit", "os_limit"))
        for key, field in fields.items()
    }
    if (
        values["total"] <= 0
        or values["available"] > values["total"]
        or min(values["count_limit"], values["os_limit"]) <= 0
    ):
        raise ValueError("System processor capacity or limits are contradictory")
    if not dedicated and not 0 < values["floor"] <= 1:
        raise ValueError("System minimum units per virtual processor is invalid")
    return {
        **values,
        "uuid": system.get("UUID"),
        "name": resource.get("SystemName"),
        "state": resource.get("State"),
    }


def _targets(
    before: dict[str, Any], change: LparProcessorChange, system: dict[str, Any]
) -> dict[str, Any]:
    capacity = _capacity(system, before)
    if (
        before["state"] not in ("running", "not activated")
        or before["operation_in_progress"]
    ):
        raise ValueError(
            "Processor change requires a stable running or not activated partition"
        )
    aix = before["partition_type"] in ("AIX/Linux", "Virtual IO Server")
    if change.force and not aix:
        raise ValueError("force is supported only for AIX/Linux/VIOS processor changes")
    inactive = before["state"] == "not activated"
    if not inactive and before["dynamic_processor"] not in (
        True,
        False,
        "true",
        "false",
        "1",
        "0",
    ):
        raise ValueError("Running processor change requires reported DLPAR capability")
    deferred = inactive or aix and before["rmc"] != "active" and change.force
    if (
        not inactive
        and not deferred
        and (
            before["dynamic_processor"] not in (True, "true", "1")
            or aix
            and before["rmc"] != "active"
        )
    ):
        raise ValueError(
            "Running processor change requires DLPAR capability and active RMC, or explicit no-RMC force"
        )
    if before["processor_mode"] == "ded" and change.processing_units is not None:
        raise ValueError("Processing units apply only to shared processors")
    targets: dict[str, Any] = {
        "processors": before["current_processors"],
        "processing_units": before.get("current_processing_units"),
        "reset": False,
        "deferred": deferred,
        "noop": False,
    }
    for quantity in ("processors", "processing_units"):
        amount = getattr(change, quantity)
        if amount is None:
            continue
        current = before[f"current_{quantity}"]
        target = {"add": current + amount, "remove": current - amount, "set": amount}[
            change.operation
        ]
        if target < 0:
            raise ValueError("Processor subtraction exceeds current quantity")
        minimum, maximum = (
            before[f"current_minimum_{quantity}"],
            before[f"current_maximum_{quantity}"],
        )
        if inactive and maximum == 0:
            minimum, maximum = (
                before[f"pending_minimum_{quantity}"],
                before[f"pending_maximum_{quantity}"],
            )
        if target > min(maximum, before[f"pending_maximum_{quantity}"]):
            raise ValueError("Requested processor target exceeds partition maximum")
        reset = (
            change.operation == "set"
            and target == 0
            or target < current
            and target < minimum
        )
        if reset and not inactive:
            raise ValueError("Running processors cannot be reduced below their minimum")
        if not reset and target < minimum and target != current:
            raise ValueError("Requested processor target is below its minimum")
        targets[quantity] = target
        targets["reset"] |= reset
    if targets["reset"]:
        if any(
            targets[key] is not None and targets[key] > before[f"current_{key}"]
            for key in ("processors", "processing_units")
        ):
            raise ValueError("Processor reset contradicts simultaneous positive growth")
        targets.update(
            processors=0,
            processing_units=Decimal(0)
            if before["processor_mode"] == "shared"
            else None,
        )
    else:
        count, units = targets["processors"], targets["processing_units"]
        if count > min(capacity["count_limit"], capacity["os_limit"]):
            raise ValueError(
                "Processor target exceeds applicable system or OS count limit"
            )
        if units is not None and not capacity["floor"] * count <= units <= count:
            raise ValueError(
                "Shared processor entitlement violates units per virtual processor bounds"
            )
        allocated = count if units is None else units
        previous = (
            before["current_processors"]
            if units is None
            else before["current_processing_units"]
        )
        if (
            allocated > capacity["total"]
            or allocated - previous > capacity["available"]
        ):
            raise ValueError("Processor target exceeds available system capacity")
        targets["noop"] = all(
            targets[key] == before.get(f"current_{key}")
            for key in ("processors", "processing_units")
        )
    return targets


def _postconditions(
    before: dict[str, Any], after: dict[str, Any], expected: dict[str, Any]
) -> bool:
    if any(
        before[key] != after[key]
        for key in (
            "uuid",
            "name",
            "partition_id",
            "state",
            "partition_type",
            "processor_mode",
            "shared",
        )
    ):
        raise ValueError("Processor readback identity, state or mode changed")
    checks = [not after["operation_in_progress"]]
    for key in ("processors", "processing_units"):
        if expected[key] is None:
            continue
        checks.append(after[f"current_{key}"] == expected[key])
        if not expected["deferred"]:
            checks.append(after[f"runtime_{key}"] == expected[key])
        for bound in ("minimum_", "maximum_"):
            field = f"current_{bound}{key}"
            checks.append(after[field] == (0 if expected["reset"] else before[field]))
    checks.extend(
        after[key] == (0 if expected["reset"] else before[key]) for key in _MEMORY
    )
    return all(checks)


def _result(
    before: dict[str, Any],
    after: dict[str, Any] | None,
    expected: dict[str, Any],
    outcome: str,
    submission_error: str | None = None,
    read_error: str | None = None,
) -> dict[str, Any]:
    location = ChangeLocation(**(after or before)["change_location"])
    warnings = (
        []
        if location.lives_in == "current-configuration-and-profile"
        else [location.summary()]
    )
    if expected["deferred"]:
        warnings.append(
            "Inactive current configuration only; no running-guest effect or later activation is verified."
            if before["state"] == "not activated"
            else "Forced no-RMC processor change requires an operating-system restart; running-guest effect is unverified."
        )
    if expected["reset"]:
        warnings.append(
            "Shutdown processor reset clears CPU and memory assignments; inspect both before activation."
        )
    if read_error:
        warnings.append(read_error[:2048])

    def render(values: dict[str, Any] | None) -> dict[str, Any] | None:
        return (
            None
            if values is None
            else {
                key: f"{value:.2f}" if isinstance(value, Decimal) else value
                for key, value in values.items()
            }
        )

    return {
        "UUID": before["uuid"],
        "outcome": outcome,
        "workflow_completed": outcome in ("applied", "noop"),
        "before": render(before),
        "after": render(after),
        "expected": render(expected),
        "submission_error": submission_error,
        "change_location": (after or before)["change_location"],
        "warnings": warnings,
    }


async def change_lpar_processors(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    change: LparProcessorChange,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Authorize, recheck and submit once; independently classify current CPU/memory state."""
    if not isinstance(change, LparProcessorChange):
        raise TypeError("change must be a typed LparProcessorChange")
    if not system_name_or_uuid or not system_name_or_uuid.strip():
        raise ValueError(
            "system_name_or_uuid is required for native processor quantities"
        )
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    kind, uuid = await memory._contained_partition(hmc, system_uuid, lpar_name_or_uuid)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    entry = await read(uuid)
    identity = memory._snapshot(entry, uuid)
    if not is_uuid(lpar_name_or_uuid) and identity["name"] != lpar_name_or_uuid:
        raise ValueError("Processor target name changed after containment resolution")
    if (identity["partition_type"] == "Virtual IO Server") != (
        kind == "VirtualIOServer"
    ):
        raise ValueError(
            "Processor target partition kind disagrees with contained feed"
        )
    if not isinstance(identity["name"], str) or not identity["name"].strip():
        raise ValueError("Processor target requires a nonempty partition name")
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if (
        not system_name
        or (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower()
    ):
        raise ValueError("Processor target system has no unique CLI name")
    await authorize_lpar_mutation(
        hmc,
        system_name,
        identity["name"],
        ownership_override=ownership_override,
        max_output_bytes=65536,
    )

    async def evidence(current_entry: dict[str, Any] | None) -> dict[str, Any]:
        row = await read_lpar_processor_quantities(
            hmc.config, system_name, identity["partition_id"]
        )
        return _snapshot(current_entry, uuid, row)

    before = await evidence(entry)
    system = await hmc.get_managed_system(system_uuid)
    capacity = _capacity(system or {}, before)
    if (
        str(capacity["uuid"]).lower() != system_uuid.lower()
        or capacity["name"] != system_name
    ):
        raise ValueError("Processor capacity identity differs from selected system")
    expected = _targets(before, change, system or {})
    if await memory._contained_partition(hmc, system_uuid, uuid) != (kind, uuid):
        raise ValueError("Processor containment changed before submission")
    if (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower():
        raise ValueError("Processor system identity changed before submission")
    fresh = await evidence(await read(uuid))
    fresh_capacity = _capacity(await hmc.get_managed_system(system_uuid) or {}, fresh)
    if fresh != before or fresh_capacity != capacity:
        raise ValueError(
            "Processor decision evidence changed before submission; nothing was sent"
        )
    if expected["noop"]:
        return _result(before, fresh, expected, "noop")
    error = None
    rejected = False
    try:
        await change_lpar_processor_quantity(
            hmc.config, system_name, identity["partition_id"], change
        )
    except HMCError as exc:
        error = str(exc)[:2048]
        cause = exc.__cause__
        rejected = (
            isinstance(cause, asyncssh.ProcessError)
            and cause.exit_status is not None
            and cause.exit_status > 0
            and not cause.exit_signal
        )
    try:
        if await memory._contained_partition(hmc, system_uuid, uuid) != (kind, uuid):
            raise ValueError("Processor readback containment changed")
        after = await evidence(await read(uuid))
        matched = _postconditions(before, after, expected)
    except (HMCError, ValueError) as exc:
        return _result(before, None, expected, "unverified", error, str(exc))
    outcome = (
        "applied"
        if matched
        else "partial"
        if after != before
        else "rejected"
        if rejected
        else "unverified"
    )
    return _result(before, after, expected, outcome, error)
