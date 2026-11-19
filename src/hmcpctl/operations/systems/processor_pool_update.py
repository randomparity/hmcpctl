"""Validate a shared processor pool change against its managed system."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_processors import bounded_processor_number
from hmcpctl.documents.native_records import RECORD_DELIMITERS, build_attribute_record
from hmcpctl.errors import HMCError
from hmcpctl.operations.resource_reading import resource_container
from hmcpctl.operations.systems.processor_pools import (
    SharedProcessorPool,
    get_shared_processor_pool,
    read_selected_pool,
)
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.install import validate_hmc_name
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text, render_mtms


@dataclass(frozen=True)
class PoolCapacity:
    member_count: int
    entitlement: Decimal
    available: Decimal


def _member_units(entry: dict[str, Any], pool_id: int) -> Decimal | None:
    resource = resource_container(entry, "Resource")
    config = resource_container(resource, "PartitionProcessorConfiguration")
    dedicated = leaf_text(config.get("CurrentHasDedicatedProcessors"))
    if dedicated in ("true", "1", True):
        return None
    if dedicated not in ("false", "0", False):
        raise ValueError(
            "Pool update requires an explicit current processor mode for each partition"
        )
    shared = resource_container(config, "CurrentSharedProcessorConfiguration")
    current_id = bounded_processor_number(
        leaf_text(shared.get("CurrentSharedProcessorPoolID")), whole=True
    )
    if current_id != pool_id:
        return None
    return Decimal(
        bounded_processor_number(leaf_text(shared.get("CurrentProcessingUnits")))
    )


async def _system_resource(hmc: HMCClient, system_uuid: str) -> dict[str, Any]:
    system = await hmc.get_managed_system(system_uuid)
    if not system or str(system.get("UUID")).casefold() != system_uuid.casefold():
        raise ValueError("Pool update could not confirm the managed system identity")
    resource = resource_container(system, "Resource")
    if leaf_text(resource.get("State")) != "operating":
        raise ValueError("Pool update requires an operating managed system")
    return resource


async def capacity_evidence(
    hmc: HMCClient,
    system_uuid: str,
    pool_id: int,
    reported_members: tuple[str, ...] | None,
    *,
    resource: dict[str, Any] | None = None,
) -> PoolCapacity:
    """Read entitlement from both partition feeds and available units from the managed system."""
    if resource is None:
        resource = await _system_resource(hmc, system_uuid)
    processors = resource_container(resource, "AssociatedSystemProcessorConfiguration")
    available = bounded_processor_number(
        leaf_text(processors.get("CurrentAvailableSystemProcessorUnits"))
    )
    entries = await hmc.list_pool_partition_entries(system_uuid)
    seen: set[str] = set()
    members: dict[str, Decimal] = {}
    for entry in entries:
        uuid = entry.get("UUID")
        if not isinstance(uuid, str) or not is_uuid(uuid) or uuid.casefold() in seen:
            raise ValueError(
                "Pool update received a missing or repeated partition identity"
            )
        seen.add(uuid.casefold())
        units = _member_units(entry, pool_id)
        if units is not None:
            members[uuid.casefold()] = units
    if reported_members is not None and set(members) != {
        uuid.casefold() for uuid in reported_members
    }:
        raise ValueError(
            "Pool membership disagrees with current partition assignments; reread the pool"
        )
    return PoolCapacity(
        len(members), sum(members.values(), Decimal(0)), Decimal(available)
    )


@dataclass(frozen=True)
class PoolChange:
    name: str | None
    maximum: int | None
    reserved: Decimal | None

    @classmethod
    def from_values(
        cls, *, name: str | None = None, maximum: object = None, reserved: object = None
    ) -> PoolChange:
        if name is None and maximum is None and reserved is None:
            raise ValueError("Request a new pool name, maximum or reserved units")
        if name is not None:
            validate_hmc_name(name, "new pool name")
            if any(delimiter in name for delimiter in RECORD_DELIMITERS):
                raise ValueError(
                    "Pool name contains a native attribute delimiter; choose another name"
                )
            if (
                name != name.strip()
                or not name
                or not name.isprintable()
                or len(name) > 14
            ):
                raise ValueError(
                    "Pool name must have 1–14 printable characters without outer whitespace"
                )
        return cls(
            name,
            None
            if maximum is None
            else int(bounded_processor_number(maximum, whole=True)),
            None if reserved is None else Decimal(bounded_processor_number(reserved)),
        )


async def submit_native(
    config: HMCConfig, system_name: str, pool_id: int, change: PoolChange
) -> None:
    """Submit one documented chhwres command to a non-default slot."""
    if type(pool_id) is not int or pool_id <= 0:
        raise ValueError("Pool update requires a positive non-default pool ID")
    parts: list[tuple[str, object]] = []
    if change.name is not None:
        parts.append(("new_name", change.name))
    if change.maximum is not None:
        parts.append(("max_pool_proc_units", change.maximum))
    if change.reserved is not None:
        parts.append(("reserved_pool_proc_units", f"{change.reserved:.2f}"))
    attributes = build_attribute_record(parts, surface="`chhwres -a`")
    system = shlex.quote(validate_hmc_name(system_name, "system name"))
    await run_hmc_command(
        config,
        f"chhwres -r procpool -m {system} -o s --poolid {pool_id} "
        f"-a {shlex.quote(attributes)}",
        max_output_bytes=65536,
    )


@dataclass(frozen=True)
class PoolUpdateResult:
    system_uuid: str
    before: SharedProcessorPool
    after: SharedProcessorPool | None
    outcome: str
    submission_error: str | None
    readback_error: str | None
    route: str = "chhwres"
    if_match: str = "not-applicable"


async def _validate_capacity(
    hmc: HMCClient,
    system: str,
    target: SharedProcessorPool,
    change: PoolChange,
    resource: dict[str, Any],
) -> None:
    if change.maximum is None and change.reserved is None:
        return
    maximum = bounded_processor_number(
        target.maximum_processing_units if change.maximum is None else change.maximum,
        whole=True,
    )
    reservation = bounded_processor_number(
        target.pending_reserved_processing_units
        if change.reserved is None
        else change.reserved
    )
    if reservation > maximum:
        raise ValueError("Maximum pool units must cover the pending reservation")
    evidence = await capacity_evidence(
        hmc, system, target.pool_id, target.assigned_partition_uuids, resource=resource
    )
    if maximum < reservation + evidence.entitlement:
        raise ValueError(
            "Maximum pool units must cover member entitlements and the pending reservation"
        )
    if change.reserved is not None and change.reserved > evidence.available:
        raise ValueError(
            "Requested reservation exceeds available managed-system processor units"
        )


def _matches(change: PoolChange, after: SharedProcessorPool) -> bool:
    return (
        (change.name is None or change.name == after.name)
        and (change.maximum is None or change.maximum == after.maximum_processing_units)
        and (
            change.reserved is None
            or (
                after.pending_reserved_processing_units is not None
                and change.reserved
                == Decimal(str(after.pending_reserved_processing_units))
            )
        )
    )


async def update_shared_processor_pool(
    hmc: HMCClient,
    system_name_or_uuid: str,
    pool: int | str,
    *,
    new_name: str | None = None,
    maximum_units: object = None,
    reserved_units: object = None,
) -> PoolUpdateResult:
    """Validate and submit the selected fields once, then reread the original slot."""
    change = PoolChange.from_values(
        name=new_name, maximum=maximum_units, reserved=reserved_units
    )
    system, target, slots = await read_selected_pool(hmc, system_name_or_uuid, pool)
    if target.pool_id == 0:
        raise ValueError("Cannot update the default shared processor pool (ID 0)")
    if change.name is not None and any(
        slot.name == change.name and slot.pool_id != target.pool_id for slot in slots
    ):
        raise ValueError("New pool name already exists on this managed system")
    resource = await _system_resource(hmc, system)
    system_name = render_mtms(resource)
    if system_name is None:
        raise ValueError(
            "Shared processor pool update requires the selected system's full MTMS"
        )
    await _validate_capacity(hmc, system, target, change, resource)
    submit_error = None
    try:
        await submit_native(hmc.config, system_name, target.pool_id, change)
    except HMCError as error:
        submit_error = str(error)
    try:
        result = await get_shared_processor_pool(hmc, system, target.pool_id)
        if result.system_uuid != system or result.pool.uuid != target.uuid:
            raise ValueError("Shared processor pool identity changed during readback")
    except (HMCError, ValueError) as error:
        return PoolUpdateResult(
            system, target, None, "unverified", submit_error, str(error)
        )
    if _matches(change, result.pool) and submit_error is None:
        outcome = "verified"
    else:
        requested_changed = (
            (change.name is not None and result.pool.name != target.name)
            or (
                change.maximum is not None
                and result.pool.maximum_processing_units
                != target.maximum_processing_units
            )
            or (
                change.reserved is not None
                and result.pool.pending_reserved_processing_units
                != target.pending_reserved_processing_units
            )
        )
        outcome = "partial" if requested_changed else "unverified"
    return PoolUpdateResult(system, target, result.pool, outcome, submit_error, None)
