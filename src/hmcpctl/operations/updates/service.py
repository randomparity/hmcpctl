"""Presentation-neutral update submission and waiting workflows."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from hmcpctl.client.client_contracts import _reject_over_long_path_value
from hmcpctl.client.client_updates import PlatformUpdateJobEntry
from hmcpctl.client.core import HMCClient

from ...errors import HMCError
from ...jobs import (
    TERMINAL_JOB_STATUSES,
    validate_wait_timing,
    vios_stdout,
    wait_for_submitted_job,
)
from ...resource_identity import resolve_system_uuid, resolve_vios_uuid
from ...xmlutil import console_version
from .models import (
    ConsoleUpdateSource,
    PlatformUpdateParameter,
    VIOSUpdateSource,
    VIOSUpgradeSource,
    list_management_console_updates_job,
    platform_update_job,
    update_hmc_job,
    update_vios_job,
    upgrade_vios_job,
)

_MINIMUM_PLATFORM_UPDATE_VERSION = (11, 1, 1111)


def _with_vios_stdout(
    result: dict[str, Any] | None, wait: bool
) -> dict[str, Any] | None:
    """Project completed VIOS job output without altering the raw payload."""
    if not wait or not isinstance(result, dict) or "stdOut" in result:
        return result
    resource = result.get("Resource")
    if (
        not isinstance(resource, dict)
        or resource.get("Status") not in TERMINAL_JOB_STATUSES
    ):
        return result
    output = vios_stdout(result)
    return result if output is None else {**result, "stdOut": output}


async def _wait_for_platform_update(
    hmc: HMCClient,
    job: PlatformUpdateJobEntry | None,
    wait: bool,
    timeout_seconds: int,
    poll_interval: int,
) -> dict[str, Any] | None:
    """Validate and optionally wait for an already-submitted PlatformUpdate job."""
    raw_job = dict(job) if job is not None else None
    if not wait:
        return raw_job
    if job is not None:
        if job["Resource"]["Status"] in TERMINAL_JOB_STATUSES:
            return raw_job
        link = job.get("link")
        if not link:
            raise HMCError(
                "PlatformUpdate was accepted but cannot be polled because the HMC "
                "returned a nonterminal response without a selfLink"
            )
    return await wait_for_submitted_job(
        hmc, raw_job, wait, timeout_seconds, poll_interval
    )


def _require_platform_update_version(console: dict[str, Any] | None) -> None:
    """Require documented PlatformUpdate support before resolving a target."""
    resource = console.get("Resource") if isinstance(console, dict) else None
    parsed = console_version(resource) if isinstance(resource, dict) else None
    if parsed is None or parsed < _MINIMUM_PLATFORM_UPDATE_VERSION:
        classification = "below the minimum" if parsed is not None else "unavailable"
        raise ValueError(
            "PlatformUpdate requires HMC 11.1.1111 or later; "
            f"the connected HMC version is {classification}. "
            "Upgrade the HMC before retrying."
        )


async def update_console_software(
    hmc: HMCClient,
    console_uuid: str,
    repository: ConsoleUpdateSource,
    *,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit a supported management-console software update."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    _reject_over_long_path_value("console_uuid", console_uuid)
    console_path_id = quote(console_uuid, safe="")
    job = await hmc.submit_job(
        f"/rest/api/uom/ManagementConsole/{console_path_id}/do/UpdateManagementConsole",
        update_hmc_job(repository),
    )
    return await wait_for_submitted_job(hmc, job, wait, timeout_seconds, poll_interval)


async def submit_available_hmc_ptfs_query(
    hmc: HMCClient,
    console_uuid: str,
    *,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit the management-console job that lists available PTFs."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    _reject_over_long_path_value("console_uuid", console_uuid)
    console_path_id = quote(console_uuid, safe="")
    job = await hmc.submit_job(
        f"/rest/api/uom/ManagementConsole/{console_path_id}"
        "/do/ListManagementConsoleUpdates",
        list_management_console_updates_job(),
    )
    return await wait_for_submitted_job(hmc, job, wait, timeout_seconds, poll_interval)


async def update_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    repository: VIOSUpdateSource,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit a VIOS software update and project terminal output."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vios_path_id = quote(vios_uuid, safe="")
    job = await hmc.submit_job(
        f"/rest/api/uom/VirtualIOServer/{vios_path_id}/do/UpdateVIOS",
        update_vios_job(repository),
    )
    result = await wait_for_submitted_job(
        hmc, job, wait, timeout_seconds, poll_interval
    )
    return _with_vios_stdout(result, wait)


async def upgrade_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    repository: VIOSUpgradeSource,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit a VIOS version upgrade and project terminal output."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vios_path_id = quote(vios_uuid, safe="")
    job = await hmc.submit_job(
        f"/rest/api/uom/VirtualIOServer/{vios_path_id}/do/UpgradeVIOS",
        upgrade_vios_job(repository),
    )
    result = await wait_for_submitted_job(
        hmc, job, wait, timeout_seconds, poll_interval
    )
    return _with_vios_stdout(result, wait)


async def update_firmware(
    hmc: HMCClient,
    system_name_or_uuid: str,
    platform_update: PlatformUpdateParameter,
    *,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit a supported Power11 PlatformUpdate workflow."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    _require_platform_update_version(await hmc.get_console_info())
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    job = await hmc.submit_platform_update(
        system_uuid, platform_update_job(platform_update)
    )
    return await _wait_for_platform_update(
        hmc, job, wait, timeout_seconds, poll_interval
    )
