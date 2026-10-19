"""LPAR inventory operations."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.partition_state import PARTITION_STATES, PartitionState
from hmcpctl.operations.systems.fleet import FleetListing, read_fleet_quick_all

from ...resource_identity import (
    is_uuid,
    optional_system_selector,
    resolve_lpar_uuid,
    resolve_system_uuid,
)


async def list_lpars(
    hmc: HMCClient,
    system_name_or_uuid: str | None = None,
    state: PartitionState | None = None,
) -> FleetListing:
    """List LPARs, optionally scoped to one system and partition state.

    Without a system, partitions come from one LogicalPartition/quick/All read
    when every managed system is operating or powered off; otherwise each
    operating system is read in turn and the others are named in
    ``unreadable_systems`` (ADR 0197, ADR 0248).
    """
    if state is not None and state not in PARTITION_STATES:
        allowed = ", ".join(sorted(PARTITION_STATES))
        raise ValueError(f"state must be one of: {allowed}")
    selector = optional_system_selector(system_name_or_uuid)
    system_uuid = (
        await resolve_system_uuid(hmc, selector) if selector is not None else None
    )
    # No PartitionState search: a V10R3 HMC answers a value with a space
    # ("not activated") with 500 "Unable to parse expression" (#1202).
    listing = (
        FleetListing(await hmc.list_logical_partitions(system_uuid))
        if system_uuid is not None
        else await read_fleet_quick_all(
            hmc, "LogicalPartition", hmc.list_logical_partitions, "LPARs"
        )
    )
    if state is None:
        return listing
    return replace(
        listing,
        entries=[
            entry
            for entry in listing.entries
            if (entry.get("Resource") or {}).get("PartitionState") == state
        ],
    )


async def get_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get one LPAR by UUID or by an optionally system-scoped exact name.

    Return None when an exact name has no match or the UUID read has no entry.

    Raises:
        ResourceNotFoundError: If a supplied managed-system name cannot be resolved.
        ValueError: If a name is ambiguous or a UUID lies outside the supplied system.
        HMCError: If the HMC request fails or absence cannot be established.
    """
    if is_uuid(lpar_name_or_uuid):
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
        return await hmc.get_logical_partition(lpar_uuid)
    selector = optional_system_selector(system_name_or_uuid)
    system_uuid = (
        await resolve_system_uuid(hmc, selector) if selector is not None else None
    )
    return await hmc.find_partition_by_name(lpar_name_or_uuid, system_uuid=system_uuid)


async def get_lpar_state(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> str | None:
    """Return one LPAR's state by UUID or an optionally system-scoped name.

    System scope disambiguates duplicate partition names; UUID selectors do not
    require it. Return None when the quick-property response is empty (including
    HTTP 204), rather than when a partition name cannot be resolved.

    Raises:
        ResourceNotFoundError: If the LPAR name or supplied system name cannot be resolved.
        ValueError: If a name is ambiguous or a UUID lies outside the supplied system.
        HMCError: If the HMC request fails or absence cannot be established.
    """
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    return await hmc.get_quick_property("LogicalPartition", lpar_uuid, "PartitionState")
