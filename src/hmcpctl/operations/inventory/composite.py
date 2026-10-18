"""Presentation-neutral composite inventory operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.systems.fleet import operating_system_entry

from ...errors import HMCError
from ...resource_identity import resolve_lpar_uuid, resolve_system_uuid
from ...xmlutil import leaf_text, render_mtms
from .capacity import system_capacity


@dataclass(frozen=True)
class LparSummary:
    uuid: object | None
    name: object | None
    state: object | None
    rmc_state: object | None
    partition_type: object | None
    partition_id: object | None
    current_memory_mib: object | None
    desired_memory_mib: object | None
    current_proc_units: object | None
    desired_proc_units: object | None
    desired_vcpus: object | None
    dedicated_procs: object | None
    os_version: object | None
    os_type: object | None
    client_network_adapter_count: int
    description: object | None
    mapped_storage: None


@dataclass(frozen=True)
class SystemSummary:
    """One managed system's state, capacity, and partition counts.

    Total is the system's configurable memory (MiB) or processor units and free
    is what it currently reports available. A partition or VIOS count is ``None``
    when the HMC refused that inventory read; ``warnings`` names each one.
    """

    uuid: object | None
    name: object | None
    state: object | None
    mtms: str | None
    firmware_version: str | None
    total_memory_mib: int
    free_memory_mib: int
    total_proc_units: float
    free_proc_units: float
    lpar_count: int | None
    lpar_states: dict[str, int]
    vios_count: int | None
    warnings: tuple[str, ...] = ()


def optional_int(value: object) -> int | None:
    text = optional_text(value)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def optional_float(value: object) -> float | None:
    text = optional_text(value)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def resource_container(resource: dict[str, Any], name: str) -> dict[str, Any]:
    value = resource.get(name)
    return value if isinstance(value, dict) else {}


def processor_figures(resource: dict[str, Any]) -> dict[str, Any]:
    """Read processors from the container that ``HasDedicatedProcessors`` selects.

    V10R3 nests them in ``PartitionProcessorConfiguration``. A partition whose mode is
    not ``true`` or ``false`` reads as shared, and a missing container yields ``None``.
    """
    config = resource_container(resource, "PartitionProcessorConfiguration")
    mode = config.get("HasDedicatedProcessors")
    if mode == "true":
        desired = resource_container(config, "DedicatedProcessorConfiguration")
        current = resource_container(config, "CurrentDedicatedProcessorConfiguration")
        return {
            "current_proc_units": current.get("CurrentProcessors"),
            "desired_proc_units": desired.get("DesiredProcessors"),
            "desired_vcpus": None,
            "dedicated_procs": True,
        }
    desired = resource_container(config, "SharedProcessorConfiguration")
    current = resource_container(config, "CurrentSharedProcessorConfiguration")
    return {
        "current_proc_units": current.get("CurrentProcessingUnits"),
        "desired_proc_units": desired.get("DesiredProcessingUnits"),
        "desired_vcpus": desired.get("DesiredVirtualProcessors"),
        "dedicated_procs": False if mode == "false" else None,
    }


def _lpar_summary(
    lpar: dict[str, Any],
    adapters: list[dict[str, Any]],
) -> LparSummary:
    res = lpar.get("Resource") or {}
    memory = resource_container(res, "PartitionMemoryConfiguration")
    return LparSummary(
        uuid=lpar.get("UUID"),
        name=res.get("PartitionName"),
        state=res.get("PartitionState"),
        rmc_state=res.get("ResourceMonitoringControlState"),
        partition_type=res.get("PartitionType"),
        partition_id=res.get("PartitionID"),
        current_memory_mib=memory.get("CurrentMemory"),
        desired_memory_mib=memory.get("DesiredMemory"),
        **processor_figures(res),
        os_version=res.get("OperatingSystemVersion"),
        os_type=leaf_text(res.get("OperatingSystemType")),
        client_network_adapter_count=len(adapters),
        description=leaf_text(res.get("Description")),
        # Note: mapped vSCSI storage requires VIOS UUID resolution
        # (vSCSI adapter → vios_partition_id → VIOS UUID → mapping groups
        #  filtered by LPAR link) and is not included here. List VIOS resources to
        #  retrieve per-VIOS storage mappings, then filter by the LPAR's
        #  partition ID.
        mapped_storage=None,
    )


async def fetch_lpar_summary(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
) -> LparSummary:
    """Compose partition details and adapter inventory into one summary.

    Raises ``ValueError`` when the partition cannot be found. ``mapped_storage``
    remains unset because resolving it requires a separate VIOS inventory hop.
    """
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar, adapters = await _fetch_lpar_data(hmc, lpar_uuid)
    return _lpar_summary(lpar, adapters)


async def _fetch_lpar_data(
    hmc, lpar_uuid: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch an LPAR, then fetch its client network adapters."""
    lpar = await hmc.get_logical_partition(lpar_uuid)
    if lpar is None:
        raise ValueError(
            f"LPAR {lpar_uuid!r} not found after resolution. "
            "List logical partitions to inspect the available partitions."
        )
    adapters = await hmc.list_child(
        "LogicalPartition", lpar_uuid, "ClientNetworkAdapter"
    )
    return lpar, adapters


_MAX_WARNING_LENGTH = 500


async def _inventory_or_warning(
    read: Awaitable[list[dict]], source: str
) -> list[dict] | str:
    """Return one inventory collection, or the warning for an HMC refusal.

    A V11R2 HMC answers the system's VIOS feed with HTTP 500 when a VIOS cannot
    report its storage (#1202); the rest of the summary stays useful.
    """
    try:
        return await read
    except HMCError as exc:
        return f"{source} inventory is unavailable: {exc}"[:_MAX_WARNING_LENGTH]


async def _fetch_system_summary_data(
    hmc,
    system_uuid: str,
) -> tuple[dict, list[dict] | str, list[dict] | str]:
    """Fetch a managed system, then its LPAR and VIOS collections concurrently.

    Each collection is the entries read, or a warning when the HMC refused it.
    """
    system = await operating_system_entry(hmc, system_uuid, "the summary")
    if system is None:
        raise ValueError(
            f"Managed system {system_uuid!r} not found after resolution. "
            "List managed systems to inspect the available systems."
        )
    async with asyncio.TaskGroup() as tasks:
        lpars_task = tasks.create_task(
            _inventory_or_warning(hmc.list_logical_partitions(system_uuid), "LPAR")
        )
        vios_task = tasks.create_task(
            _inventory_or_warning(hmc.list_vios(system_uuid), "VIOS")
        )
    return system, lpars_task.result(), vios_task.result()


def optional_text(value: object) -> str | None:
    text = leaf_text(value)
    return text if isinstance(text, str) else None


def _system_summary(
    system: dict[str, Any],
    lpars: list[dict[str, Any]] | str,
    vios_list: list[dict[str, Any]] | str,
) -> SystemSummary:
    res = system.get("Resource") or {}
    warnings = tuple(value for value in (lpars, vios_list) if isinstance(value, str))

    lpar_states: dict[str, int] = {}
    for lpar in lpars if isinstance(lpars, list) else []:
        lr = lpar.get("Resource") or {}
        state = lr.get("PartitionState") or "unknown"
        lpar_states[state] = lpar_states.get(state, 0) + 1

    capacity = system_capacity(system)

    return SystemSummary(
        uuid=system.get("UUID"),
        name=res.get("SystemName"),
        state=res.get("State"),
        mtms=render_mtms(res),
        firmware_version=optional_text(res.get("SystemFirmware")),
        total_memory_mib=capacity.total_memory_mib,
        free_memory_mib=capacity.free_memory_mib,
        total_proc_units=capacity.total_proc_units,
        free_proc_units=capacity.free_proc_units,
        lpar_count=len(lpars) if isinstance(lpars, list) else None,
        lpar_states=lpar_states,
        vios_count=len(vios_list) if isinstance(vios_list, list) else None,
        warnings=warnings,
    )


async def fetch_system_summary(
    hmc: HMCClient, system_name_or_uuid: str
) -> SystemSummary:
    """Compose system, partition, and VIOS inventory into one summary.

    Raises ``ValueError`` when the managed system cannot be found.
    """
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    system, lpars, vios_list = await _fetch_system_summary_data(hmc, system_uuid)
    return _system_summary(system, lpars, vios_list)
