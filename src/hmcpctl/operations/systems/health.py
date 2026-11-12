"""Presentation-neutral fleet health issue reporting."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from hmcpctl.client.core import HMCClient

from ...errors import HMCError, HMCTLSFingerprintError

_SYSTEM_WORKERS = 8
_MAX_SYSTEMS = 256
_MAX_RESOURCES_PER_SYSTEM = 10_000
_MAX_ISSUES = 10_000
_MAX_SCALAR_LENGTH = 500
_MAX_WARNING_LENGTH = 500


@dataclass(frozen=True)
class FleetHealthResult:
    """Curated unhealthy resources across one HMC-managed estate."""

    systems: tuple[dict[str, Any], ...]
    vios: tuple[dict[str, Any], ...]
    lpars: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


def _bounded_text_or_unknown(value: object) -> str:
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if len(normalized) > _MAX_SCALAR_LENGTH:
            raise ValueError(
                f"Fleet health scalar exceeds the safe limit of {_MAX_SCALAR_LENGTH} characters"
            )
        return normalized
    return "unknown"


def _resource(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("Resource")
    return value if isinstance(value, dict) else {}


def _sorted_records(
    records: list[dict[str, Any]], id_key: str = "uuid"
) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(records, key=lambda record: (record["name"], record[id_key])))


def _check_issue_budget(*categories: Collection[object]) -> None:
    if sum(map(len, categories)) > _MAX_ISSUES:
        raise ValueError(
            f"Fleet health result exceeds the safe limit of {_MAX_ISSUES} issues"
        )


def _system_issue(summary: dict[str, Any]) -> dict[str, Any] | None:
    state = _bounded_text_or_unknown(summary.get("State")).lower()
    if state == "operating":
        return None
    return {
        "uuid": _bounded_text_or_unknown(summary.get("UUID")),
        "name": _bounded_text_or_unknown(summary.get("SystemName")),
        "state": state,
    }


def _vios_issue(
    vios: dict[str, Any], system_uuid: str, system_name: str
) -> dict[str, Any] | None:
    resource = _resource(vios)
    state = _bounded_text_or_unknown(resource.get("PartitionState")).lower()
    if state == "running":
        return None
    return {
        "uuid": _bounded_text_or_unknown(vios.get("UUID")),
        "name": _bounded_text_or_unknown(resource.get("PartitionName")),
        "state": state,
        "system_uuid": system_uuid,
        "system_name": system_name,
    }


def _lpar_issue(
    lpar: dict[str, Any], system_uuid: str, system_name: str
) -> dict[str, Any] | None:
    resource = _resource(lpar)
    state = _bounded_text_or_unknown(resource.get("PartitionState")).lower()
    rmc_state = _bounded_text_or_unknown(
        resource.get("ResourceMonitoringControlState")
    ).lower()
    # A partition that is not activated has no RMC connection to report.
    if state == "not activated" or rmc_state in {"active", "busy"}:
        return None
    return {
        "uuid": _bounded_text_or_unknown(lpar.get("UUID")),
        "name": _bounded_text_or_unknown(resource.get("PartitionName")),
        "state": state,
        "rmc_state": rmc_state,
        "system_uuid": system_uuid,
        "system_name": system_name,
    }


async def _vios_or_warning(
    hmc: HMCClient, system_uuid: str, system_name: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Return a system's VIOS entries, or none and a warning on HMC refusal.

    A V11R2 HMC answers a system's VIOS feed with HTTP 500 when a VIOS cannot
    report its storage (#1202); the rest of the estate is still reported.
    """
    try:
        return await hmc.list_vios(system_uuid), None
    except HMCTLSFingerprintError:
        raise
    except HMCError as exc:
        warning = f"VIOS inventory for system {system_name} is unavailable: {exc}"
        return [], warning[:_MAX_WARNING_LENGTH]


async def _lpars_or_warning(
    hmc: HMCClient, system_uuid: str, system_name: str, operating: bool
) -> tuple[list[dict[str, Any]], str | None]:
    """Return a system's partitions, or none and a warning if it is not operating.

    The partition feed is core inventory: for an operating system its failure
    fails the whole result. A system that is not operating refuses the feed
    (#1301) and is already reported among the unhealthy systems.
    """
    try:
        return await hmc.list_logical_partitions(system_uuid), None
    except HMCTLSFingerprintError:
        raise
    except HMCError as exc:
        if operating:
            raise
        warning = f"LPAR inventory for system {system_name} is unavailable: {exc}"
        return [], warning[:_MAX_WARNING_LENGTH]


async def _system_inventory(
    hmc: HMCClient, system_uuid: str, system_name: str, operating: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[str, ...]]:
    lpar_task = asyncio.create_task(
        _lpars_or_warning(hmc, system_uuid, system_name, operating)
    )
    vios_task = asyncio.create_task(_vios_or_warning(hmc, system_uuid, system_name))
    tasks = (lpar_task, vios_task)
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    lpars, lpar_warning = lpar_task.result()
    vioses, vios_warning = vios_task.result()
    if (
        len(lpars) > _MAX_RESOURCES_PER_SYSTEM
        or len(vioses) > _MAX_RESOURCES_PER_SYSTEM
    ):
        raise ValueError(
            f"Fleet health inventory for system {system_uuid} exceeds the safe "
            f"limit of {_MAX_RESOURCES_PER_SYSTEM} resources per category"
        )
    warnings = tuple(
        warning for warning in (lpar_warning, vios_warning) if warning is not None
    )
    return lpars, vioses, warnings


async def fetch_fleet_health(hmc: HMCClient) -> FleetHealthResult:
    """Return curated unhealthy resources from the configured HMC estate.

    The systems and their ``State`` come from ``ManagedSystem/quick/All``: the
    full ``ManagedSystem`` feed times out while a system has no connection (#1501).
    """
    systems = await hmc.list_system_summaries()
    if len(systems) > _MAX_SYSTEMS:
        raise ValueError(
            f"Fleet health inventory exceeds the safe limit of {_MAX_SYSTEMS} "
            "managed systems"
        )
    queue: asyncio.Queue[tuple[dict[str, Any], str, str]] = asyncio.Queue()
    system_issues: list[dict[str, Any]] = []
    for system in systems:
        uuid_value = system.get("UUID") if isinstance(system, dict) else None
        if not isinstance(uuid_value, str) or not uuid_value.strip():
            raise ValueError("Managed system entry must contain a valid UUID")
        system_uuid = _bounded_text_or_unknown(uuid_value)
        system_name = _bounded_text_or_unknown(system.get("SystemName"))
        queue.put_nowait((system, system_uuid, system_name))
        issue = _system_issue(system)
        if issue is not None:
            system_issues.append(issue)
            _check_issue_budget(system_issues)

    vios_issues: list[dict[str, Any]] = []
    lpar_issues: list[dict[str, Any]] = []
    inventory_warnings: list[str] = []

    async def inspect_systems() -> None:
        while not queue.empty():
            try:
                system, system_uuid, system_name = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                lpars, vioses, warnings = await _system_inventory(
                    hmc, system_uuid, system_name, _system_issue(system) is None
                )
                inventory_warnings.extend(warnings)
                lpar_issues.extend(
                    issue
                    for lpar in lpars
                    if (issue := _lpar_issue(lpar, system_uuid, system_name))
                    is not None
                )
                vios_issues.extend(
                    issue
                    for vios in vioses
                    if (issue := _vios_issue(vios, system_uuid, system_name))
                    is not None
                )
                _check_issue_budget(system_issues, vios_issues, lpar_issues)
            finally:
                queue.task_done()

    worker_count = min(_SYSTEM_WORKERS, len(systems))
    worker_tasks = [asyncio.create_task(inspect_systems()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*worker_tasks)
    except BaseException:
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        raise
    return FleetHealthResult(
        _sorted_records(system_issues),
        _sorted_records(vios_issues),
        _sorted_records(lpar_issues),
        tuple(sorted(inventory_warnings)),
    )
