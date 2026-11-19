"""Validate a shared processor pool change against its managed system."""

from __future__ import annotations

import csv
import io
import shlex
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_processors import bounded_processor_number
from hmcpctl.operations.resource_reading import resource_container
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.install import validate_hmc_name
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text


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


async def capacity_evidence(
    hmc: HMCClient,
    system_uuid: str,
    pool_id: int,
    reported_members: tuple[str, ...] | None,
) -> PoolCapacity:
    """Read entitlement from both partition feeds and available units from the managed system."""
    system = await hmc.get_managed_system(system_uuid)
    if not system or str(system.get("UUID")).casefold() != system_uuid.casefold():
        raise ValueError("Pool update could not confirm the managed system identity")
    resource = resource_container(system, "Resource")
    if leaf_text(resource.get("State")) != "operating":
        raise ValueError("Pool update requires an operating managed system")
    processors = resource_container(resource, "AssociatedSystemProcessorConfiguration")
    available = bounded_processor_number(
        leaf_text(processors.get("CurrentAvailableSystemProcessorUnits"))
    )
    entries = await hmc.list_logical_partitions(system_uuid)
    vios = await hmc.list_vios(system_uuid)
    seen: set[str] = set()
    members: dict[str, Decimal] = {}
    for entry in (*entries, *vios):
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
            if not name.strip() or not name.isprintable() or len(name) > 14:
                raise ValueError(
                    "Pool name must have 1–14 printable characters and contain nonblank text"
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
    parts = []
    if change.name is not None:
        parts.append(f"new_name={change.name}")
    if change.maximum is not None:
        parts.append(f"max_pool_proc_units={change.maximum}")
    if change.reserved is not None:
        parts.append(f"reserved_pool_proc_units={change.reserved:.2f}")
    attributes = io.StringIO()
    csv.writer(attributes, lineterminator="").writerow(parts)
    system = shlex.quote(validate_hmc_name(system_name, "system name"))
    await run_hmc_command(
        config,
        f"chhwres -r procpool -m {system} -o s --poolid {pool_id} "
        f"-a {shlex.quote(attributes.getvalue())}",
        max_output_bytes=65536,
    )
