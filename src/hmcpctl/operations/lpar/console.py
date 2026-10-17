"""Selector resolution for bounded LPAR console capture (issue #959).

The CLI command ``hmcpctl lpars capture-console`` and the MCP tool
``hmc_capture_lpar_console`` both resolve their selectors here, so one policy
turns a name or UUID into the CLI names ``mkvterm`` needs.
"""

from __future__ import annotations

from collections.abc import Callable

from hmcpctl.client.core import HMCClient
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_system_uuid,
)
from hmcpctl.ssh.console import ConsoleCapture, capture_lpar_console
from hmcpctl.ssh.lpar import resolve_lpar_cli_name


async def capture_lpar_console_by_selector(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    system_name_or_uuid: str,
    *,
    duration_seconds: float,
    max_bytes: int,
    idle_timeout_seconds: float,
    on_chunk: Callable[[bytes], None] | None = None,
    stop_on_cancel: bool = False,
) -> ConsoleCapture:
    """Resolve the selectors to HMC CLI names and capture the partition console.

    *on_chunk* is forwarded to :func:`~hmcpctl.ssh.console.capture_lpar_console`;
    when provided it is called with each raw chunk as it arrives so the CLI can
    write bytes to its sink in real time (issue #1674).
    """
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
    )
    system_name = (
        system_name_or_uuid
        if not is_uuid(system_name_or_uuid)
        else await resolve_system_name(hmc, system_uuid)
    )
    lpar_name = (
        lpar_name_or_uuid
        if not is_uuid(lpar_name_or_uuid)
        else await resolve_lpar_cli_name(hmc.config, lpar_uuid, system_name)
    )
    return await capture_lpar_console(
        hmc,
        system_name,
        lpar_name,
        duration_seconds=duration_seconds,
        max_bytes=max_bytes,
        idle_timeout_seconds=idle_timeout_seconds,
        on_chunk=on_chunk,
        stop_on_cancel=stop_on_cancel,
    )
