"""Local operator access to external-management holds (ADR 0193 Decision 7)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import typer

from ..operations.logical import holds
from ..resource_identity import is_uuid
from .output import fail, print_json


def _public(hold: holds.Hold) -> dict[str, Any]:
    """A hold as the operator sees it: everything but the ``hold_id``."""
    fields = asdict(hold)
    del fields["hold_id"]
    return fields


def holds_list() -> None:
    """List every hold on this host as JSON, oldest first, without their hold_ids.

    No HMC connection or credentials are needed. An unreadable, corrupt, non-private or lost
    store refuses the command.
    """
    try:
        listed = holds.list_holds()
    except ValueError as exc:
        fail(exc)
    print_json({"holds": [_public(hold) for hold in listed]})


def holds_release(
    partition_uuid: str = typer.Argument(..., help="Partition UUID from holds list"),
) -> None:
    """Release the hold on one partition under any agent id; output what was released.

    Writes nothing to the HMC and never deletes the partition. The consumer that was handed
    the partition is not told: release only once it no longer manages the partition. A
    partition with no hold releases nothing.
    """
    if not is_uuid(partition_uuid):
        fail(ValueError(f"{partition_uuid!r} is not a partition UUID; see holds list"))
    try:
        released = holds.operator_release(partition_uuid)
    except ValueError as exc:
        fail(exc)
    print_json({"released": [_public(hold) for hold in released]})


def register_commands(group: typer.Typer) -> None:
    """Register the local hold operator commands."""
    group.command("list")(holds_list)
    group.command("release")(holds_release)
