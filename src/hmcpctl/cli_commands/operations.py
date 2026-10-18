"""Local operator access to durable logical operations (ADR 0190)."""

from __future__ import annotations

from dataclasses import asdict

import typer

from ..operations.logical import store
from .output import fail, print_json


def operations_list(
    limit: int = typer.Option(
        50, "--limit", min=1, max=50, help="Records per page (1–50)"
    ),
    cursor: str | None = typer.Option(
        None, "--cursor", help="next_cursor from the previous page"
    ),
) -> None:
    """List all agents' local records as JSON, newest first, with at most 200 events each.

    No HMC connection or credentials are needed. Pass next_cursor unchanged for the next page.
    An unreadable, corrupt, non-private or lost store refuses the command.
    """
    try:
        page = store.operator_status(limit=limit, cursor=cursor)
    except ValueError as exc:
        fail(exc)
    print_json(asdict(page))


def operations_abandon(
    operation_id: str = typer.Argument(
        ..., help="32 lower-case hex digits from operations list"
    ),
) -> None:
    """Abandon an interrupted or paused operation under any agent id; output its JSON record.

    Releases the partition guard and preserves the resource ledger. Writes nothing to the HMC.
    Running operations refuse; terminal operations return unchanged. Store failures refuse.
    """
    try:
        record = store.operator_abandon(operation_id)
    except ValueError as exc:
        fail(exc)
    print_json(asdict(record))


def register_commands(group: typer.Typer) -> None:
    """Register the local operator commands."""
    group.command("list")(operations_list)
    group.command("abandon")(operations_abandon)
