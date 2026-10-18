"""MCP tools for managed-system inventory and lifecycle operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from ..._app import (
    require_valid_limit,
    run_limited_collection,
    with_client,
)
from ...config import (
    config_inventory,
)
from ...documents.system import (
    MemoryMirroringMode,
    PowerOffPolicy,
    PowerOnLparStartPolicy,
)
from ...operations.lpar.inventory import get_lpar, get_lpar_state, list_lpars
from ...operations.partition_state import PartitionState
from ...operations.systems.core import (
    ManagedSystemPatch,
    get_system,
    list_systems,
    modify_system,
    power_system,
)
from ...operations.systems.fleet import FleetListing
from ...operations.vios.core import get_vios_storage_detail, list_vios
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="console.info", target_kind="console")
def hmc_get_console_info(profile: str | None = None) -> dict[str, Any] | None:
    """Get HMC version, network configuration and links to managed systems.

    Useful as a connectivity check — this is the cheapest HMC call.

    Args:
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(lambda hmc: hmc.get_console_info(), profile=profile)


@tool(
    effect="read",
    operation="config.list_hosts",
    target_kind="none",
    connection_argument=None,
)
def hmc_list_configured_hosts() -> dict[str, Any]:
    """List all configured HMC profiles from the platform-native TOML config.

    Returns profile names, hostnames, users, ports, TLS settings, default
    status, and credential-presence booleans. Never returns passwords, resolved
    password_env values, or SSH key contents — only has_password / has_ssh_key
    presence indicators.

    No network calls are made. When no config file exists, returns an empty
    profile list.
    """
    return config_inventory()


@tool(effect="read", operation="system.list", target_kind="console")
def hmc_list_systems(
    state: str | None = None,
    profile: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List managed systems, optionally filtered by state.

    When state is omitted, returns every managed system known to the HMC. An
    operating system's entry is complete: UUID, SystemName, State,
    MachineTypeModelAndSerialNumber, PrimaryIPAddress, etc. A system that is
    not operating (for example ``no connection``) cannot be read in full, so
    its entry carries only UUID and SystemName, State and StateDetail under
    Resource; its State says why.

    When state is provided, returns only systems whose State matches the given
    value exactly. Use hmc_get_system for a single system lookup by name.

    Args:
        state: Optional exact managed-system State value to filter on.
        profile: Optional configured HMC profile name; uses the default when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
    """

    return run_limited_collection(
        lambda hmc: list_systems(hmc, state), limit, profile=profile
    )


@tool(effect="read", operation="lpar.list", target_kind="managed_system")
def hmc_list_lpars(
    system_name_or_uuid: str | None = None,
    state: PartitionState | None = None,
    profile: str | None = None,
    limit: int | None = None,
) -> FleetListing:
    """List LPARs, optionally filtered by system and state.

    Returns ``entries`` (the partitions) and ``unreadable_systems``. Without a
    system, each operating managed system's LPAR feed is read in turn; a system
    that is not operating is not read and is named in ``unreadable_systems``
    instead, so its partitions are absent from ``entries``. With a system,
    ``unreadable_systems`` is empty. A state filter is applied to the feed
    entries locally. Use hmc_get_lpar for a single partition or
    hmc_get_lpar_state for a lightweight state lookup.

    Args:
        system_name_or_uuid: Optional SystemName or UUID whose partitions to list.
        state: Optional exact PartitionState value to filter.
        profile: Optional configured HMC profile name; uses the default when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer, and never truncates
            unreadable_systems.
    """
    return _limited_listing(
        lambda hmc: list_lpars(hmc, system_name_or_uuid, state),
        limit,
        profile=profile,
    )


@tool(effect="read", operation="lpar.get", target_kind="lpar")
def hmc_get_lpar(
    lpar_name_or_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get one logical partition by partition name or UUID.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    return with_client(
        lambda hmc: get_lpar(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_state", target_kind="lpar")
def hmc_get_lpar_state(
    lpar_name_or_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> str | None:
    """Return the current state of one LPAR by partition name or UUID.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    return with_client(
        lambda hmc: get_lpar_state(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="vios.list", target_kind="managed_system")
def hmc_list_vios(
    system_name_or_uuid: str | None = None,
    state: PartitionState | None = None,
    profile: str | None = None,
    limit: int | None = None,
) -> FleetListing:
    """List Virtual I/O Servers, optionally filtered by system or state.

    Returns ``entries`` (the VIOSes) and ``unreadable_systems``. Without a
    system, each operating managed system's VIOS feed is read in turn; a system
    that is not operating is not read and is named in ``unreadable_systems``
    instead, so its VIOSes are absent from ``entries``. With a system,
    ``unreadable_systems`` is empty. A state filter is applied to the feed
    entries locally. Use hmc_get_vios_storage_detail for the storage-detail
    mappings of one VIOS.

    Args:
        system_name_or_uuid: Optional SystemName or UUID whose VIOSes to list.
        state: Optional exact PartitionState value to filter.
        profile: Optional configured HMC profile name; uses the default when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer, and never truncates
            unreadable_systems.
    """
    return _limited_listing(
        lambda hmc: list_vios(hmc, system_name_or_uuid, state),
        limit,
        profile=profile,
    )


@tool(effect="read", operation="vios.get", target_kind="vios")
def hmc_get_vios_storage_detail(
    vios_name_or_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get storage-detail mappings for one VIOS by partition name or UUID.

    Args:
        vios_name_or_uuid: PartitionName or UUID of the Virtual I/O Server.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates a VIOS name.
    """

    return with_client(
        lambda hmc: get_vios_storage_detail(
            hmc,
            vios_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="console.list_resources", target_kind="console")
def hmc_list_resources(
    resource_type: str,
    profile: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List any root uom resource type exposed by the HMC.

    Reads ``/rest/api/uom/{resource_type}``, so only root types are listable:
    for example ManagedSystem, LogicalPartition, VirtualIOServer and Cluster.
    Child types such as VirtualSwitch, VirtualNetwork, SharedProcessorPool and
    HostEthernetAdapter exist only under ``ManagedSystem/{uuid}``, and the HMC
    refuses a root read of a non-root type such as SRIOVAdapter with HTTP 400.

    Args:
        resource_type: Exact HMC UOM resource type to list.
        profile: Optional configured HMC profile name; uses the default when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
    """

    return run_limited_collection(
        lambda hmc: hmc.list_uom(resource_type), limit, profile=profile
    )


@tool(effect="read", operation="system.get", target_kind="managed_system")
def hmc_get_system(
    system_name_or_uuid: str, profile: str | None = None
) -> dict[str, Any] | None:
    """Get a managed system by exact SystemName or UUID.

    Returns the full system dict if found, or None if no system with that
    name is known to the HMC. A system that is not operating returns a partial
    dict: UUID, and SystemName, State and StateDetail under Resource.

    Args:
        system_name_or_uuid: Exact SystemName or UUID of the managed system.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: get_system(hmc, system_name_or_uuid), profile=profile
    )


@tool(effect="mutate", operation="system.modify", target_kind="managed_system")
def hmc_modify_system(
    system_name_or_uuid: str,
    new_name: str | None = None,
    power_off_policy: PowerOffPolicy | None = None,
    power_on_lpar_start_policy: PowerOnLparStartPolicy | None = None,
    pend_mem_region_size: int | None = None,
    requested_num_sys_huge_pages: int | None = None,
    mem_mirroring_mode: MemoryMirroringMode | None = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Modify a managed system's configuration, leaving omitted fields unchanged.

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system to modify.
        new_name: Replacement SystemName, or null to leave it unchanged.
        power_off_policy: Power-off policy, or null to leave it unchanged.
        power_on_lpar_start_policy: LPAR auto-start policy, or null to leave unchanged.
        pend_mem_region_size: Pending memory-region size in MiB, or null for unchanged.
        requested_num_sys_huge_pages: Requested system huge-page count, or null.
        mem_mirroring_mode: Memory-mirroring mode, or null to leave it unchanged.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: modify_system(
            hmc,
            system_name_or_uuid,
            ManagedSystemPatch(
                new_name=new_name,
                power_off_policy=power_off_policy,
                power_on_lpar_start_policy=power_on_lpar_start_policy,
                pend_mem_region_size=pend_mem_region_size,
                requested_num_sys_huge_pages=requested_num_sys_huge_pages,
                mem_mirroring_mode=mem_mirroring_mode,
            ),
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="system.power_on", target_kind="managed_system")
def hmc_power_on_system(
    system_name_or_uuid: str,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Power on a managed system, optionally waiting for a terminal job state.

    Returns the submitted job immediately by default. With ``wait=true``,
    returns the last polled job after it reaches a terminal state or the timeout.

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system to power on.
        wait: Whether to poll the submitted job until terminal or timed out.
        timeout_seconds: Maximum polling duration in seconds when waiting.
        poll_interval: Seconds between job polls when waiting; must be positive.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: power_system(
            hmc,
            system_name_or_uuid,
            power_on=True,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="system.power_off", target_kind="managed_system")
def hmc_power_off_system(
    system_name_or_uuid: str,
    immediate: bool = False,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Power off a managed system, optionally waiting for a terminal job state.

    Returns the submitted job immediately by default. With ``wait=true``,
    returns the last polled job after it reaches a terminal state or the timeout.

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system to power off.
        immediate: Whether to request immediate shutdown instead of a graceful shutdown.
        wait: Whether to poll the submitted job until terminal or timed out.
        timeout_seconds: Maximum polling duration in seconds when waiting.
        poll_interval: Seconds between job polls when waiting; must be positive.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: power_system(
            hmc,
            system_name_or_uuid,
            power_on=False,
            immediate=immediate,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


def _limited_listing(
    fn: Callable[[Any], Awaitable[FleetListing]],
    limit: int | None,
    *,
    profile: str | None,
) -> FleetListing:
    """Run a fleet listing, then cap its entries; skipped systems stay named."""
    require_valid_limit(limit)
    listing = with_client(fn, profile=profile)
    return (
        listing if limit is None else replace(listing, entries=listing.entries[:limit])
    )
