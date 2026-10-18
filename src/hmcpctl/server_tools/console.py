"""MCP tool for bounded, non-interactive LPAR console capture (issue #385)."""

from __future__ import annotations

import base64
from typing import Any

from .._app import with_client
from ..operations.lpar.console import capture_lpar_console_by_selector
from ..tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="mutate", operation="lpar.capture_console", target_kind="lpar")
def hmc_capture_lpar_console(
    lpar_name_or_uuid: str,
    system_name_or_uuid: str,
    duration_seconds: float = 30.0,
    max_bytes: int = 65_536,
    idle_timeout_seconds: float = 10.0,
    profile: str | None = None,
) -> dict[str, Any]:
    """Capture a bounded snapshot of an LPAR's virtual console (mkvterm).

    Runs the HMC ``mkvterm`` CLI over SSH for at most ``duration_seconds``,
    returning the raw console bytes base64-encoded in ``data_base64`` (the
    stream is binary — escape sequences, partial UTF-8 — so no decoding is
    imposed), plus ``stop_reason`` (``duration`` / ``max_bytes`` / ``idle`` /
    ``remote-close`` / ``error``), a bounded ``error`` diagnostic populated
    only for error stops, and an honest ``released`` flag.

    This is a capture, not a terminal: stdin is sealed by construction, so no
    byte can reach the partition — a partition at an SMS menu, firmware
    prompt, or installer prompt cannot act on stray input. The partition's
    single vterm slot is held for the duration of the capture; if another
    session already holds it, the call fails with a distinct contention error
    and never force-closes that session. On every other exit path the capture
    releases the console by closing its stdin, which ends only its own
    ``mkvterm`` (``rmvterm`` if the stream had already ended or does not end in
    time), and then *proves* the release by opening a fresh ``mkvterm`` from an
    independent session; ``released`` is true only when that proof
    succeeds. The exception: when the HMC's report that another client ended
    the capture's hold arrives before the capture closes, the capture stops with ``stop_reason`` ``error``, an ``error`` naming
    ``ConsoleHoldLostError``, ``released`` false, and no ``rmvterm``; leave the
    other client's session alone. For any other false
    ``released``, the partition's console may remain held — run
    ``rmvterm -m <system> -p <lpar>`` deliberately, or use the HMC UI, to
    recover it.

    Useful when a NIM install fails and the only diagnosis lives on the
    console: boot messages, SMS menus, open-firmware output, BOS install
    error screens.

    Requires HMC authority for mkvterm/rmvterm (typically hmcsuperadmin,
    e.g. hscroot).

    Args:
        lpar_name_or_uuid: Partition name or UUID whose console to capture.
        system_name_or_uuid: Managed-system name or UUID hosting the
            partition.
        duration_seconds: Wall-clock cap on the whole capture (max 3600).
        max_bytes: Cap on captured bytes (max 1048576); truncation never
            splits a multi-byte UTF-8 sequence or an incomplete ANSI escape.
        idle_timeout_seconds: Client-side cap on silence — time since the
            last received byte; an idle HMC vterm stream never closes by
            itself and carries no keepalives.
        profile: Optional TOML profile name; uses environment defaults when
            omitted.
    """

    async def capture_console_result(hmc) -> dict[str, Any]:
        capture = await capture_lpar_console_by_selector(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            duration_seconds=duration_seconds,
            max_bytes=max_bytes,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        return {
            "system": capture.system,
            "partition": capture.lpar,
            "stop_reason": capture.stop_reason,
            "released": capture.released,
            "error": capture.error,
            "bytes_captured": len(capture.data),
            "data_base64": base64.b64encode(capture.data).decode("ascii"),
        }

    return with_client(capture_console_result, profile=profile)
