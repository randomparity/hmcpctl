"""The installer boot's refcode baseline and wait (ADR 0191 Decision 2, ADR 0326).

Rows are ordered by the HMC's own timestamp and row order; hmcpctl's clock only
bounds how long the wait lasts and is never compared with an HMC timestamp.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hmcpctl.ssh.refcodes import MAX_REFCODE_COUNT
from hmcpctl.ssh.transport import HMCCLIError

# Codes that mean the installer boot has started, observed in both native windows
# (ADR 0328): CA00E105, partition firmware passing control to the boot image. Only
# native evidence adds a code (ADR 0191 Decision 2).
BOOT_STARTED_CODES: frozenset[str] = frozenset({"CA00E105"})
POLL_SECONDS = 15.0
REFCODE_ROWS = 20
_TIMESTAMP = "%m/%d/%Y %H:%M:%S"
_READ_ERRORS = (HMCCLIError, ValueError, TypeError)

Row = Mapping[str, Any]


@dataclass(frozen=True)
class BootWait:
    """How a wait ended: the qualifying row, or the newer rows seen and why not."""

    started: dict[str, Any] | None
    observed: list[dict[str, Any]]
    reason: str | None = None


def row_identity(row: Row | None) -> dict[str, Any]:
    """A refcode row as the baseline effect's identity; nulls when there is no row."""
    row = row or {}
    return {
        "time_stamp": row.get("time_stamp") or None,
        "refcode": row.get("refcode") or None,
    }


def _when(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        # Both sides are HMC timestamps in one unrecorded zone (ADR 0191 Decision 2).
        return datetime.strptime(text.strip(), _TIMESTAMP)  # noqa: DTZ007
    except ValueError:
        return None


def newer_rows(
    rows: Sequence[Row], baseline: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The rows of one newest-first read that came after *baseline*.

    A row is newer when it precedes the baseline's row in the HMC's order (or the
    baseline is no longer in the read) and its timestamp, where both parse, is not
    earlier than the baseline's.
    """
    base = row_identity(baseline)
    if base == row_identity(None):
        return [row_identity(row) for row in rows]
    cut = next(
        (i for i, row in enumerate(rows) if row_identity(row) == base), len(rows)
    )
    base_when = _when(base["time_stamp"])
    newer = []
    for row in rows[:cut]:
        when = _when(row.get("time_stamp"))
        if base_when is not None and when is not None and when < base_when:
            continue
        newer.append(row_identity(row))
    return newer


def _history_may_be_incomplete(
    rows: Sequence[Row], baseline: Mapping[str, Any], count: int
) -> bool:
    return len(rows) >= count and not any(
        row_identity(row) == row_identity(baseline) for row in rows
    )


async def _read_boot_window(
    read: Callable[[int], Awaitable[Sequence[Row]]], baseline: Mapping[str, Any]
) -> BootWait:
    for count in (REFCODE_ROWS, MAX_REFCODE_COUNT):
        rows = await read(count)
        observed = newer_rows(rows, baseline)
        for row in observed:
            if row["refcode"] in BOOT_STARTED_CODES:
                return BootWait(row, observed)
        if not _history_may_be_incomplete(rows, baseline, count):
            return BootWait(None, observed)
    return BootWait(
        None,
        observed,
        "reference-code history is incomplete: the baseline is absent "
        f"from the newest {MAX_REFCODE_COUNT} rows; boot evidence may be missing",
    )


async def wait_for_boot(
    read: Callable[[int], Awaitable[Sequence[Row]]],
    baseline: Mapping[str, Any],
    wait_seconds: float,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BootWait:
    """Read refcodes at least once, then every ``POLL_SECONDS`` until *wait_seconds*.

    Honor newer boot evidence before widening a saturated missing-baseline window.
    End with an explicit gap reason when the maximum read cannot establish coverage.
    """
    deadline = clock() + wait_seconds
    observed: list[dict[str, Any]] = []
    while True:
        try:
            poll = await _read_boot_window(read, baseline)
        except _READ_ERRORS as exc:
            reason = f"a refcode read failed: {type(exc).__name__}: {exc}"[:512]
            return BootWait(None, observed, reason)
        observed = poll.observed
        if poll.started is not None or poll.reason is not None:
            return poll
        if clock() + POLL_SECONDS > deadline:
            break
        await sleep(POLL_SECONDS)
    reason = "no newer reference code reached the boot-started set before wait_seconds"
    return BootWait(None, observed, reason)
