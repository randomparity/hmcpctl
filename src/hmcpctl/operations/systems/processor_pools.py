"""Typed shared processor pool reads over the ``SharedProcessorPool`` child feed.

The feed returns one entry per firmware pool slot, configured or not. ADR 0375 decides
which slots are configured and how absent fields read; pool update (#1976) and
partition pool assignment (#1977) consume this projection.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from hmcpctl.client.core import HMCClient

from ...errors import HMCError
from ...resource_identity import ResourceNotFoundError, is_uuid, resolve_system_uuid
from ...xmlutil import leaf_text

#: ADR 0375: the default pool, a pool with nonzero units, or a renamed slot.
CONFIGURED_RULE = "default-or-nonzero-units-or-renamed"

_POOL_ID = re.compile(r"\d+")
_PARTITION_MARKERS = ("/LogicalPartition/", "/VirtualIOServer/")
_NOT_FOUND_ADVICE = "List the system's shared processor pools for valid IDs and names."


@dataclass(frozen=True)
class SharedProcessorPool:
    """One pool slot. ``None`` units are absent (not applicable), never zero."""

    uuid: str
    pool_id: int
    name: str
    configured: bool
    maximum_processing_units: float | None
    reserved_processing_units: float | None
    pending_reserved_processing_units: float | None
    available_processing_units: float | None
    #: ``None`` when the HMC reports no ``AssignedPartitions``; ``()`` when it is empty.
    assigned_partition_uuids: tuple[str, ...] | None


@dataclass(frozen=True)
class SharedProcessorPoolListing:
    system_uuid: str
    configured_rule: str
    slot_count: int
    unconfigured_omitted: int
    pools: tuple[SharedProcessorPool, ...]


@dataclass(frozen=True)
class SharedProcessorPoolResult:
    system_uuid: str
    configured_rule: str
    pool: SharedProcessorPool


def _units(resource: Mapping[str, Any], field: str, label: str) -> float | None:
    if field not in resource:
        return None
    text = leaf_text(resource[field])
    try:
        value = Decimal(text) if isinstance(text, str) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0:
        raise HMCError(
            f"Shared processor pool {label} has a malformed {field}: {text!r}"
        )
    return float(value)


def _partition_uuid(href: object, label: str) -> str:
    path = urlparse(href).path if isinstance(href, str) else ""
    for marker in _PARTITION_MARKERS:
        _, found, tail = path.rpartition(marker)
        if found and is_uuid(tail):
            return tail
    raise HMCError(
        f"Shared processor pool {label} has an AssignedPartitions link that names "
        f"no partition: {href!r}"
    )


def _assigned_partitions(
    resource: Mapping[str, Any], label: str
) -> tuple[str, ...] | None:
    if "AssignedPartitions" not in resource:
        return None
    assigned = resource["AssignedPartitions"]
    if assigned == "":
        return ()
    if not isinstance(assigned, Mapping):
        raise HMCError(
            f"Shared processor pool {label} has a malformed AssignedPartitions element"
        )
    links = assigned.get("link", [])
    links = links if isinstance(links, list) else [links]
    return tuple(
        _partition_uuid(link.get("href") if isinstance(link, Mapping) else None, label)
        for link in links
    )


def _is_configured(pool_id: int, name: str, units: Sequence[float | None]) -> bool:
    return (
        pool_id == 0
        or any(unit for unit in units)
        or name != f"SharedPool{pool_id:02d}"
    )


def project_shared_processor_pool(entry: Mapping[str, Any]) -> SharedProcessorPool:
    """Project one parsed feed entry, raising ``HMCError`` on any malformed field."""
    uuid = entry.get("UUID")
    resource = entry.get("Resource")
    if (
        not isinstance(uuid, str)
        or not is_uuid(uuid)
        or not isinstance(resource, Mapping)
    ):
        raise HMCError("Shared processor pool entry has no UUID or resource")
    raw_id = leaf_text(resource.get("PoolID"))
    if not isinstance(raw_id, str) or not _POOL_ID.fullmatch(raw_id):
        raise HMCError(
            f"Shared processor pool {uuid} has a malformed PoolID: {raw_id!r}"
        )
    pool_id = int(raw_id)
    label = f"ID {pool_id}"
    name = leaf_text(resource.get("PoolName"))
    if not isinstance(name, str) or not name:
        raise HMCError(f"Shared processor pool {label} has no PoolName")
    maximum = _units(resource, "MaximumProcessingUnits", label)
    reserved = _units(resource, "CurrentReservedProcessingUnits", label)
    pending = _units(resource, "PendingReservedProcessingUnits", label)
    return SharedProcessorPool(
        uuid=uuid,
        pool_id=pool_id,
        name=name,
        configured=_is_configured(pool_id, name, (maximum, reserved, pending)),
        maximum_processing_units=maximum,
        reserved_processing_units=reserved,
        pending_reserved_processing_units=pending,
        available_processing_units=_units(resource, "AvailableProcUnits", label),
        assigned_partition_uuids=_assigned_partitions(resource, label),
    )


async def _read_pools(
    hmc: HMCClient, system_name_or_uuid: str
) -> tuple[str, tuple[SharedProcessorPool, ...]]:
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    entries = await hmc.list_shared_processor_pools(system_uuid)
    pools = sorted(
        (project_shared_processor_pool(entry) for entry in entries),
        key=lambda pool: pool.pool_id,
    )
    for earlier, later in itertools.pairwise(pools):
        if earlier.pool_id == later.pool_id:
            raise HMCError(f"Shared processor pool feed repeats PoolID {later.pool_id}")
    return system_uuid, tuple(pools)


async def list_shared_processor_pools(
    hmc: HMCClient, system_name_or_uuid: str, *, include_unconfigured: bool = False
) -> SharedProcessorPoolListing:
    """List a system's configured pools, or every slot with *include_unconfigured*."""
    system_uuid, pools = await _read_pools(hmc, system_name_or_uuid)
    shown = pools if include_unconfigured else tuple(p for p in pools if p.configured)
    return SharedProcessorPoolListing(
        system_uuid=system_uuid,
        configured_rule=CONFIGURED_RULE,
        slot_count=len(pools),
        unconfigured_omitted=len(pools) - len(shown),
        pools=shown,
    )


def _selector_matches(pool: SharedProcessorPool, selector: int | str) -> bool:
    if isinstance(selector, str):
        return pool.name == selector
    return pool.pool_id == selector


async def get_shared_processor_pool(
    hmc: HMCClient, system_name_or_uuid: str, pool: int | str
) -> SharedProcessorPoolResult:
    """Get one pool slot by PoolID (an int, 0 included) or exact PoolName (a str)."""
    if isinstance(pool, bool) or not isinstance(pool, (int, str)):
        raise TypeError("pool must be an integer PoolID or a pool name")
    if isinstance(pool, int) and pool < 0:
        raise ValueError(f"pool ID must be >= 0, not {pool}")
    if isinstance(pool, str) and not pool:
        raise ValueError("pool name must not be empty")
    system_uuid, pools = await _read_pools(hmc, system_name_or_uuid)
    matches = [candidate for candidate in pools if _selector_matches(candidate, pool)]
    if not matches:
        kind = "name" if isinstance(pool, str) else "ID"
        # An MCP client may send an ID as a JSON string, which selects by name.
        hint = (
            " A string selects by name; pass an integer to select by pool ID."
            if isinstance(pool, str) and _POOL_ID.fullmatch(pool)
            else ""
        )
        raise ResourceNotFoundError(
            "shared processor pool",
            str(pool),
            f"No shared processor pool with {kind} {pool!r} on managed system "
            f"{system_uuid}.{hint} {_NOT_FOUND_ADVICE}",
        )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous shared processor pool name {pool!r}: pool IDs "
            f"{', '.join(str(match.pool_id) for match in matches)}"
        )
    return SharedProcessorPoolResult(system_uuid, CONFIGURED_RULE, matches[0])
