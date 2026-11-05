"""MCP tools for HMC/VIOS software update and firmware update jobs."""

from __future__ import annotations

from typing import Any

from .._app import (
    with_client,
)
from ..operations.updates.models import (
    ConsoleUpdateSource,
    PlatformUpdateParameter,
    VIOSUpdateSource,
    VIOSUpgradeSource,
)
from ..operations.updates.service import (
    submit_available_hmc_ptfs_query,
    update_console_software,
    update_firmware,
    update_vios,
    upgrade_vios,
)
from ..tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="destructive", operation="update.console", target_kind="console")
def hmc_update_console_software(
    console_uuid: str,
    repository: ConsoleUpdateSource,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Submit a documented HMC software update job.

    repository uses the documented UpdateManagementConsole parameter names::

        {"MediaType": "NFS", "ServerHostOrIP": "repo.example.com",
         "Directory": "/images/hmc", "RestartConsole": "False"}

    Submits UpdateManagementConsole to ManagementConsole; poll hmc_get_job
    for status. console_uuid is the ManagementConsole UUID (from
    hmc_console_info).

    Set wait=True to block until the job reaches a terminal state. A job the HMC
    rejects in parameter validation ends ``FAILED_TO_START``. A job still
    ``NOT_STARTED`` when the wait times out is queued or stalled, not rejected:
    check it with hmc_get_job before submitting again.

    Args:
        console_uuid: Management-console UUID returned by ``hmc_console_info``.
        repository: Documented ``UpdateManagementConsole`` job parameters.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: update_console_software(
            hmc,
            console_uuid,
            repository,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="update.list_ptfs", target_kind="console")
def hmc_submit_available_hmc_ptfs_query(
    console_uuid: str,
    profile: str | None = None,
    *,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict[str, Any] | None:
    """Submit the documented job that lists available HMC PTFs.

    The HMC obtains the list from the IBM website. With ``wait=False``, returns
    the submitted job so callers can poll it with ``hmc_get_job``. With
    ``wait=True``, polls until the job reaches a terminal state or the timeout
    expires; a completed job's response contains the available PTF objects. A job
    the HMC rejects in parameter validation ends ``FAILED_TO_START``. A job still
    ``NOT_STARTED`` at the timeout is queued or stalled, not rejected: check it
    with hmc_get_job before submitting again.

    Args:
        console_uuid: Management-console UUID returned by ``hmc_console_info``.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: submit_available_hmc_ptfs_query(
            hmc,
            console_uuid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="update.vios", target_kind="vios")
def hmc_vios_update(
    vios_name_or_uuid: str,
    repository: VIOSUpdateSource,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Submit a VIOS software update job.

    repository uses the documented UpdateVIOS parameter names. Poll hmc_get_job
    for status.

    Set wait=True to block until the job reaches a terminal state.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        repository: Documented VIOS update job parameters.
        system_name_or_uuid: Optional managed system used to disambiguate a VIOS name.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: update_vios(
            hmc,
            vios_name_or_uuid,
            repository,
            system_name_or_uuid=system_name_or_uuid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="upgrade.vios", target_kind="vios")
def hmc_vios_upgrade(
    vios_name_or_uuid: str,
    repository: VIOSUpgradeSource,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Submit a VIOS version upgrade job.

    repository uses the documented UpgradeVIOS parameter names. Poll hmc_get_job
    for status.

    Set wait=True to block until the job reaches a terminal state.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        repository: Documented VIOS upgrade job parameters.
        system_name_or_uuid: Optional managed system used to disambiguate a VIOS name.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: upgrade_vios(
            hmc,
            vios_name_or_uuid,
            repository,
            system_name_or_uuid=system_name_or_uuid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(
    effect="destructive",
    operation="update.firmware",
    system_partition_write=True,
    target_kind="managed_system",
)
def hmc_update_firmware(
    system_name_or_uuid: str,
    platform_update: PlatformUpdateParameter,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Submit a documented Power11 PlatformUpdate job.

    Requires HMC 11.1.1111 or later. ``platform_update`` explicitly selects
    system firmware, SR-IOV, VIOS, and IO-adapter work using IBM's nested JSON
    shape. Poll hmc_get_job for status when the HMC supplies a self link.

    Set wait=True to block until the job reaches a terminal state.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        platform_update: Strict documented PlatformUpdate parameter object.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: update_firmware(
            hmc,
            system_name_or_uuid,
            platform_update,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )
