"""MCP tools for shared processor pool reads (ADR 0375)."""

from __future__ import annotations

from typing import Any

from pydantic import StrictFloat, StrictInt, StrictStr

from hmcpctl.operations.systems.processor_pool_update import (
    update_shared_processor_pool,
)

from ..._app import serialize_tool_result, with_client
from ...operations.systems.processor_pools import (
    get_shared_processor_pool,
    list_shared_processor_pools,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="processor_pool.list", target_kind="managed_system")
def hmc_list_shared_processor_pools(
    system_name_or_uuid: str,
    include_unconfigured: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """List a managed system's shared processor pools.

    The HMC reports every firmware pool slot (64 on POWER9); by default only
    configured pools are returned and ``unconfigured_omitted`` counts the rest.
    ``configured_rule`` names the ADR 0375 rule: the default pool (ID 0), a pool
    with nonzero maximum or reserved units, or a slot renamed from its
    ``SharedPoolNN`` placeholder. A null unit is absent from the HMC answer (not
    applicable), never zero. ``assigned_partition_uuids`` is null when the HMC
    reports no membership. The default pool's ``available_processing_units`` is
    the HMC's own value, not free capacity.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        include_unconfigured: Return every slot, each with its ``configured`` flag.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def read(hmc):
        return serialize_tool_result(
            await list_shared_processor_pools(
                hmc, system_name_or_uuid, include_unconfigured=include_unconfigured
            )
        )

    return with_client(read, profile=profile)


@tool(effect="read", operation="processor_pool.get", target_kind="managed_system")
def hmc_get_shared_processor_pool(
    system_name_or_uuid: str, pool: int | str, profile: str | None = None
) -> dict[str, Any]:
    """Get one shared processor pool slot, configured or not.

    Fields and null semantics match ``hmc_list_shared_processor_pools``.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        pool: An integer selects by pool ID (0 is the default pool); a string
            selects by exact, case-sensitive pool name.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def read(hmc):
        return serialize_tool_result(
            await get_shared_processor_pool(hmc, system_name_or_uuid, pool)
        )

    return with_client(read, profile=profile)


@tool(effect="mutate", operation="processor_pool.update", target_kind="managed_system")
def hmc_update_shared_processor_pool(
    system_name_or_uuid: str,
    pool: StrictInt | StrictStr,
    new_name: str | None = None,
    maximum_units: StrictInt | StrictFloat | StrictStr | None = None,
    reserved_units: StrictInt | StrictFloat | StrictStr | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Update a non-default shared processor pool and read back its configuration.

    A verified outcome matches the requested name, maximum and pending reservation.
    The after object reports current reservation separately. A partial or unverified
    outcome requires inspection before another write. The native route has no If-Match.

    Args:
        system_name_or_uuid: Managed system name or UUID.
        pool: Integer pool ID or exact pool name.
        new_name: Unique pool name, up to 14 characters. Comma, equals and double quote are native record delimiters.
        maximum_units: Nonnegative whole processor units, including member entitlements.
        reserved_units: Nonnegative pending reservation, exactly representable in hundredths.
        profile: Optional configured HMC connection profile.
    """

    async def update(hmc):
        result = await update_shared_processor_pool(
            hmc,
            system_name_or_uuid,
            pool,
            new_name=new_name,
            maximum_units=maximum_units,
            reserved_units=reserved_units,
        )
        return serialize_tool_result(result)

    return with_client(update, profile=profile)
