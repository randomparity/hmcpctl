"""Resolve public resource selectors for HMC SSH commands."""

from __future__ import annotations

from typing import overload

from hmcpctl.client.core import HMCClient

from ..config import HMCConfig
from ..errors import HMCTransportError
from ..resource_identity import is_uuid, lpar_name_from_uuid, system_name_from_uuid
from .lpar import resolve_lpar_cli_name, resolve_system_cli_name


async def _system_name_from_rest(hmc: HMCClient, system_uuid: str) -> str:
    name = await system_name_from_uuid(hmc, system_uuid)
    if name is None:
        raise ValueError(
            f"Could not resolve system UUID {system_uuid!r} to a system name. "
            "List managed systems to find the system UUID."
        )
    return name


async def _lpar_name_from_rest(hmc: HMCClient, lpar_uuid: str) -> str:
    name = await lpar_name_from_uuid(hmc, lpar_uuid)
    if name is None:
        raise ValueError(
            f"Could not resolve LPAR UUID {lpar_uuid!r} to a partition name. "
            "List logical partitions to find the partition UUID."
        )
    return name


async def resolve_system_name(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str | None,
) -> str | None:
    """Resolve a system UUID through REST, with SSH transport fallback."""
    system_name, _ = await resolve_ssh_names(config, system_name_or_uuid, None)
    return system_name


async def resolve_lpar_name(
    config: HMCConfig | HMCClient,
    lpar_name_or_uuid: str | None,
    system_name: str | None = None,
) -> str | None:
    """Resolve an LPAR UUID through REST, with SSH transport fallback."""
    _, lpar_name = await _resolve_ssh_names(
        config, system_name, lpar_name_or_uuid, system_is_name=True
    )
    return lpar_name


@overload
async def resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
) -> tuple[str, str]: ...


@overload
async def resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str | None,
) -> tuple[str, str | None]: ...


@overload
async def resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
) -> tuple[str | None, str]: ...


@overload
async def resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str | None,
) -> tuple[str | None, str | None]: ...


async def resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str | None,
) -> tuple[str | None, str | None]:
    return await _resolve_ssh_names(config, system_name_or_uuid, lpar_name_or_uuid)


async def _resolve_ssh_names(
    config: HMCConfig | HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str | None,
    *,
    system_is_name: bool = False,
) -> tuple[str | None, str | None]:
    if (system_is_name or not is_uuid(system_name_or_uuid or "")) and not is_uuid(
        lpar_name_or_uuid or ""
    ):
        return system_name_or_uuid, lpar_name_or_uuid
    if not isinstance(config, HMCConfig):
        return await _resolve_names(
            config,
            system_name_or_uuid,
            lpar_name_or_uuid,
            system_is_name=system_is_name,
        )
    entered = False
    try:
        async with HMCClient(config) as hmc:
            entered = True
            return await _resolve_names(
                hmc,
                system_name_or_uuid,
                lpar_name_or_uuid,
                system_is_name=system_is_name,
            )
    except HMCTransportError:
        if entered:
            raise
        system_name = system_name_or_uuid
        if not system_is_name and system_name is not None and is_uuid(system_name):
            system_name = await resolve_system_cli_name(config, system_name)
        lpar_name = lpar_name_or_uuid
        if lpar_name is not None and is_uuid(lpar_name):
            lpar_name = await resolve_lpar_cli_name(config, lpar_name, system_name)
        return system_name, lpar_name


async def _resolve_names(
    hmc: HMCClient,
    system_name: str | None,
    lpar_name: str | None,
    *,
    system_is_name: bool,
) -> tuple[str | None, str | None]:
    if not system_is_name and system_name is not None and is_uuid(system_name):
        try:
            system_name = await _system_name_from_rest(hmc, system_name)
        except HMCTransportError:
            system_name = await resolve_system_cli_name(hmc.config, system_name)
    if lpar_name is not None and is_uuid(lpar_name):
        try:
            lpar_name = await _lpar_name_from_rest(hmc, lpar_name)
        except HMCTransportError:
            lpar_name = await resolve_lpar_cli_name(hmc.config, lpar_name, system_name)
    return system_name, lpar_name
