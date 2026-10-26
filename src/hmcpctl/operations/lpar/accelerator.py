"""Admit one partition GZIP QoS submission and independently observe its result."""

from __future__ import annotations

import re
from typing import Any

import asyncssh

from ...client.core import HMCClient
from ...documents.lpar_accelerator import LparAcceleratorQos
from ...errors import HMCError
from ...resource_identity import is_uuid, resolve_system_uuid, system_name_from_uuid
from ...ssh.accelerator import read_accelerator_qos, set_accelerator_qos
from ...xmlutil import leaf_text
from ..inventory.composite import resource_container
from ..partition_state import PARTITION_STATES
from .ownership import authorize_lpar_mutation


def _integer(value: object, name: str) -> int:
    text = leaf_text(value)
    if not isinstance(text, str) or re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError(f"Accelerator QoS requires a nonnegative decimal {name}")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"Accelerator QoS cannot read decimal {name}") from None


async def _contained(
    hmc: HMCClient, system_uuid: str, selector: str
) -> tuple[str, str]:
    matches = []
    for kind, entries in (
        ("LogicalPartition", await hmc.list_logical_partitions(system_uuid)),
        ("VirtualIOServer", await hmc.list_vios(system_uuid)),
    ):
        for entry in entries:
            uuid = entry.get("UUID")
            name = leaf_text(resource_container(entry, "Resource").get("PartitionName"))
            if (
                is_uuid(selector)
                and isinstance(uuid, str)
                and uuid.casefold() == selector.casefold()
            ) or (not is_uuid(selector) and name == selector):
                if not isinstance(uuid, str) or not is_uuid(uuid):
                    raise ValueError("Accelerator target has missing or malformed UUID")
                matches.append((kind, uuid))
    if len(matches) != 1:
        raise ValueError(
            "Accelerator target is missing, ambiguous or outside selected system"
        )
    return matches[0]


async def _partition(hmc: HMCClient, kind: str, uuid: str) -> dict[str, Any]:
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    entry = await read(uuid)
    if not entry or str(entry.get("UUID", "")).casefold() != uuid.casefold():
        raise ValueError("Accelerator partition identity is missing or changed")
    resource = resource_container(entry, "Resource")
    name = leaf_text(resource.get("PartitionName"))
    partition_id = _integer(resource.get("PartitionID"), "PartitionID")
    environment = leaf_text(resource.get("PartitionType"))
    allowed = (
        ("Virtual IO Server",) if kind == "VirtualIOServer" else ("AIX/Linux", "OS400")
    )
    if not isinstance(name, str) or not name.strip() or partition_id < 1:
        raise ValueError(
            "Accelerator target requires a current name and positive PartitionID"
        )
    if environment not in allowed:
        raise ValueError("Accelerator partition kind and type disagree or are unknown")
    busy = leaf_text(resource.get("IsOperationInProgress"))
    return {
        "uuid": uuid,
        "name": name,
        "partition_id": partition_id,
        "partition_type": environment,
        "state": leaf_text(resource.get("PartitionState")),
        "rmc": leaf_text(resource.get("ResourceMonitoringControlState")),
        "operation_in_progress": {"true": True, "false": False}.get(busy)
        if isinstance(busy, str)
        else None,
        "xml_collection": resource.get("HardwareAcceleratorQoS"),
    }


def _admit_state(before: dict[str, Any]) -> None:
    state = before["state"]
    if (
        not isinstance(state, str)
        or state not in PARTITION_STATES
        or state == "Unknown"
    ):
        raise ValueError(
            "Accelerator QoS requires a concrete recognized partition state"
        )
    if before["operation_in_progress"] is not False:
        raise ValueError(
            "Accelerator QoS requires explicit false IsOperationInProgress"
        )


def _admit_options(before: dict[str, Any], change: LparAcceleratorQos) -> None:
    if before["partition_type"] == "OS400":
        if (
            change.force
            or change.wait_minutes is not None
            or change.detail_level is not None
        ):
            raise ValueError(
                "OS400 accelerator QoS does not support force, wait or detail"
            )
    else:
        if before["rmc"] not in ("active", "inactive", "none"):
            raise ValueError("Accelerator QoS requires a recognized RMC observation")
        if before["rmc"] != "active" and not change.force:
            raise ValueError(
                "Accelerator QoS without active RMC requires explicit force"
            )


async def _system(
    hmc: HMCClient, uuid: str, name: str, *, ibmi: bool
) -> dict[str, Any]:
    entry = await hmc.get_managed_system(uuid)
    resource = resource_container(entry or {}, "Resource")
    if (
        not entry
        or str(entry.get("UUID", "")).casefold() != uuid.casefold()
        or leaf_text(resource.get("SystemName")) != name
        or leaf_text(resource.get("State")) != "operating"
    ):
        raise ValueError(
            "Accelerator QoS requires matching operating selected-system identity"
        )
    caps = resource_container(resource, "AssociatedSystemCapabilities")
    general = leaf_text(caps.get("HardwareAcceleratorCapable"))
    ibmi_capable = leaf_text(caps.get("IBMiHardwareAcceleratorCapable"))
    if general != "true" or (ibmi and ibmi_capable != "true"):
        raise ValueError("Selected system lacks explicit accelerator capability")
    advertised = resource_container(resource, "SupportedHardwareAcceleratorTypes").get(
        "HardwareAcceleratorType"
    )
    types = advertised if isinstance(advertised, list) else [advertised]
    type_names = [
        leaf_text(item.get("Type")) if isinstance(item, dict) else None
        for item in types
    ]
    if any(not isinstance(name, str) for name in type_names):
        raise ValueError("Selected system has malformed accelerator type evidence")
    if type_names.count("gzip") != 1:
        raise ValueError(
            "Selected system must advertise exactly one gzip accelerator type"
        )
    return {"name": name, "general": general, "ibmi": ibmi_capable, "types": types}


def _native_row(rows: list[dict[str, Any]], partition_id: int | None) -> dict[str, Any]:
    if len(rows) != 1 or rows[0].get("type") != "gzip":
        raise ValueError("Accelerator inventory requires exactly one gzip row")
    row = rows[0]
    if (
        partition_id is not None
        and _integer(row.get("lpar_id"), "lpar_id") != partition_id
    ):
        raise ValueError("Accelerator inventory partition ID changed")
    return row


async def _baseline(
    hmc: HMCClient, system: str, partition_id: int
) -> tuple[dict, dict]:
    system_row = _native_row(await read_accelerator_qos(hmc.config, system, None), None)
    capacity = {
        key: _integer(system_row.get(key), key)
        for key in ("max_qos", "curr_avail_qos", "pend_avail_qos")
    }
    row = _native_row(
        await read_accelerator_qos(hmc.config, system, partition_id), partition_id
    )
    return capacity, {
        key: _integer(row.get(key), key) for key in ("curr_qos", "pend_qos")
    }


async def _observe(
    hmc: HMCClient,
    system_uuid: str,
    system: dict[str, Any],
    kind: str,
    before: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    uuid = before["uuid"]
    try:
        current_kind, current_uuid = await _contained(hmc, system_uuid, uuid)
        if (current_kind, current_uuid.casefold()) != (kind, uuid.casefold()):
            raise ValueError("Accelerator readback containment or kind changed")
        after = await _partition(hmc, kind, uuid)
        if any(
            after[key] != before[key]
            for key in ("uuid", "name", "partition_id", "partition_type")
        ):
            raise ValueError("Accelerator readback partition identity changed")
        fresh_system = await _system(
            hmc, system_uuid, system["name"], ibmi=before["partition_type"] == "OS400"
        )
        if fresh_system != system:
            raise ValueError("Accelerator readback selected-system evidence changed")
        row = _native_row(
            await read_accelerator_qos(
                hmc.config, system["name"], before["partition_id"]
            ),
            before["partition_id"],
        )
    except (HMCError, ValueError) as exc:
        return None, str(exc)[:2048]
    values, errors = {}, []
    for key in ("curr_qos", "pend_qos"):
        try:
            values[key] = _integer(row.get(key), key)
        except ValueError as exc:
            values[key] = None
            errors.append(str(exc))
    after["native"] = values
    return after, "; ".join(errors)[:2048] or None


def _result(
    before: dict[str, Any],
    after: dict[str, Any] | None,
    change: LparAcceleratorQos,
    submission: str,
    submission_error: str | None,
    observation_error: str | None,
) -> dict[str, Any]:
    outcome = "unverified"
    if after is not None and not observation_error:
        if all(value == change.qos for value in after["native"].values()):
            outcome = "observed"
        elif after["native"] != before["native"]:
            outcome = "partial"
        elif submission == "rejected":
            outcome = "rejected"
    restart = (
        True
        if submission == "completed"
        and change.force
        and before["rmc"] in ("inactive", "none")
        else None
    )
    warnings = [
        "Native current/pending observations do not establish effective guest runtime or profile persistence.",
        "Application timing is unproved; inspect independently before acting.",
        "Never automatically replay incomplete requests; inspect the target first.",
    ]
    if restart:
        warnings.append(
            "Completed forced submission without active RMC requires an operating system restart."
        )
    return {
        "UUID": before["uuid"],
        "requested_qos": change.qos,
        "submission": submission,
        "submission_error": submission_error,
        "before": before,
        "after": after,
        "observation_error": observation_error,
        "outcome": outcome,
        "workflow_completed": outcome == "observed" and submission == "completed",
        "completion_scope": "native-current-and-pending",
        "effective_runtime": "unavailable",
        "profile_persistence": "unverified",
        "restart_required": restart,
        "warnings": warnings,
    }


async def set_lpar_accelerator_qos(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    change: LparAcceleratorQos,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Admit and submit exactly once, without retry, then report independent evidence."""
    if not isinstance(change, LparAcceleratorQos):
        raise TypeError("change must be a typed LparAcceleratorQos")
    if type(ownership_override) is not bool:
        raise TypeError("ownership_override must be a boolean")
    for name, selector in (
        ("partition", lpar_name_or_uuid),
        ("system", system_name_or_uuid),
    ):
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"Accelerator QoS requires a nonblank {name} selector")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if not isinstance(system_uuid, str) or not is_uuid(system_uuid):
        raise ValueError("Accelerator QoS requires a valid selected-system UUID")
    kind, uuid = await _contained(hmc, system_uuid, lpar_name_or_uuid)
    before = await _partition(hmc, kind, uuid)
    if not is_uuid(lpar_name_or_uuid) and before["name"] != lpar_name_or_uuid:
        raise ValueError("Accelerator target name changed after containment resolution")
    _admit_state(before)
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if not system_name:
        raise ValueError("Accelerator target system has no CLI name")
    system = await _system(
        hmc, system_uuid, system_name, ibmi=before["partition_type"] == "OS400"
    )
    _admit_options(before, change)
    capacity, native = await _baseline(hmc, system_name, before["partition_id"])
    await authorize_lpar_mutation(
        hmc,
        system_name,
        before["name"],
        ownership_override=ownership_override,
        max_output_bytes=65536,
    )
    fresh_kind, fresh_uuid = await _contained(hmc, system_uuid, uuid)
    fresh = await _partition(hmc, kind, uuid)
    fresh_system = await _system(
        hmc, system_uuid, system_name, ibmi=before["partition_type"] == "OS400"
    )
    fresh_native = await _baseline(hmc, system_name, before["partition_id"])
    if (
        (fresh_kind, fresh_uuid.casefold()) != (kind, uuid.casefold())
        or fresh != before
        or fresh_system != system
        or fresh_native != (capacity, native)
    ):
        raise ValueError(
            "Accelerator evidence changed before submission; nothing was sent"
        )
    await authorize_lpar_mutation(
        hmc,
        system_name,
        before["name"],
        ownership_override=ownership_override,
        max_output_bytes=65536,
    )
    before.update(native=native, system_native=capacity)
    submission, error = "completed", None
    try:
        await set_accelerator_qos(
            hmc.config, system_name, before["partition_id"], change
        )
    except HMCError as exc:
        cause = exc.__cause__
        rejected = (
            isinstance(cause, asyncssh.ProcessError)
            and cause.exit_status is not None
            and cause.exit_status > 0
            and not cause.exit_signal
        )
        submission, error = "rejected" if rejected else "unknown", str(exc)[:2048]
    after, observation_error = await _observe(hmc, system_uuid, system, kind, before)
    return _result(before, after, change, submission, error, observation_error)
