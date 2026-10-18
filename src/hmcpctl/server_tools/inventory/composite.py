"""MCP adapters for composite inventory operations."""

from __future__ import annotations

from ..._app import with_client
from ...operations.inventory.composite import (
    LparSummary,
    SystemSummary,
    fetch_lpar_summary,
    fetch_system_summary,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="lpar.summary", target_kind="lpar")
def hmc_lpar_summary(
    lpar_name_or_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> LparSummary:
    """Return state, resources, OS details, adapters, and description for one LPAR.

    Memory and processor figures come from the partition's configuration containers.
    ``current_proc_units`` and ``desired_proc_units`` are processing units for a shared
    partition and whole processors for a dedicated one; ``has_dedicated_processors`` says which
    (``desired_vcpus`` is set for shared partitions only). A figure whose container is
    absent is null. On V10R3 an inactive partition reads 0 for every current and desired
    figure, even when its profile holds values.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def summary(hmc):
        return await fetch_lpar_summary(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )

    return with_client(summary, profile=profile)


@tool(effect="read", operation="system.summary", target_kind="managed_system")
def hmc_system_summary(
    system_name_or_uuid: str,
    profile: str | None = None,
) -> SystemSummary:
    """Return state, capacity, partition counts, and VIOS count for one system.

    Total capacity is the system's configurable memory (MiB) or processor units
    and free is what it currently reports available. ``mtms`` reads
    ``type-model*serial``.

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    async def summary(hmc):
        return await fetch_system_summary(hmc, system_name_or_uuid)

    return with_client(summary, profile=profile)
