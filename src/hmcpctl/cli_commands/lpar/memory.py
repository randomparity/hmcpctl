"""Native memory-resource commands."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

import typer

from ...documents.lpar_memory import (
    LparMemoryChange,
    LparMemoryMove,
    LparMemoryStatisticsReset,
)
from ...operations.lpar.memory import change_lpar_memory
from ...operations.lpar.memory_move import move_lpar_memory
from ...operations.lpar.memory_statistics import reset_lpar_memory_statistics
from ...operations.lpar.paging import switch_lpar_paging_vios
from ..output import print_json, terminal_field, usage_error
from ..runtime import with_client


def lpars_paging_switchover(
    name_or_uuid: str = typer.Argument(..., help="Contained partition name or UUID"),
    system: str = typer.Option(..., "--system", "-s", help="Containing managed system"),
    expected_current_paging_vios: str = typer.Option(
        ...,
        "--expected-current-paging-vios",
        help="Expected current paging VIOS name or UUID",
    ),
    exclusive_writer_window: bool = typer.Option(
        False,
        "--exclusive-writer-window",
        help="Acknowledge a coordinated writer window",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Switch the current redundant paging provider once and verify independently."""
    if not exclusive_writer_window:
        usage_error("Paging switchover requires --exclusive-writer-window")
    if any(
        not value.strip()
        for value in (name_or_uuid, system, expected_current_paging_vios)
    ):
        usage_error(
            "Partition, --system and --expected-current-paging-vios must be nonblank"
        )
    if not yes and not typer.confirm(
        f"Switch paging provider once for '{terminal_field(name_or_uuid)}'? Inspect uncertain outcomes before another attempt"
    ):
        return
    result = with_client(
        lambda hmc: switch_lpar_paging_vios(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            expected_current_paging_vios_name_or_uuid=expected_current_paging_vios,
            exclusive_writer_window=exclusive_writer_window,
        )
    )
    print_json(asdict(result))
    if not result.workflow_completed:
        raise typer.Exit(code=1)


def lpars_memory(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Managed system name or UUID"
    ),
    operation: Literal["add", "remove", "set"] = typer.Option(..., "--operation"),
    quantity: int | None = typer.Option(
        None, "--quantity", help="Current logical memory quantity (MiB)"
    ),
    entitled: str | None = typer.Option(
        None, "--entitled", help="Shared-memory I/O entitlement: positive MiB or auto"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Admit AIX/Linux/VIOS without RMC for later activation/restart",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval",
    ),
) -> None:
    """Add, remove or set current memory and/or shared I/O entitlement.

    This uses current assigned quantities, independently verifies the result,
    and reports where it persists. Inactive changes do not verify a running guest.
    """
    try:
        entitlement: int | Literal["auto"] | None = None
        if entitled == "auto":
            entitlement = "auto"
        elif entitled is not None:
            entitlement = int(entitled)
        change = LparMemoryChange(
            operation=operation,
            quantity_mib=quantity,
            entitled_mib=entitlement,
            force=force,
        )
        if not system.strip():
            raise ValueError("--system must name a managed system")
    except ValueError as error:
        usage_error(str(error))
    if not yes and not typer.confirm(
        f"Apply memory change to '{terminal_field(name_or_uuid)}'? "
        "On a shutdown partition, reduction below minimum clears memory AND processor assignments; "
        "forced changes may require an operating-system restart"
    ):
        return
    result = with_client(
        lambda hmc: change_lpar_memory(
            hmc,
            name_or_uuid,
            change,
            system_name_or_uuid=system,
            ownership_override=ownership_override,
        )
    )
    print_json(result)
    if not result["workflow_completed"]:
        raise typer.Exit(code=1)


def lpars_memory_move(
    source: str = typer.Argument(..., help="Source partition name or UUID"),
    destination: str = typer.Argument(..., help="Destination partition name or UUID"),
    system: str = typer.Option(..., "--system", "-s", help="Containing managed system"),
    quantity: int = typer.Option(..., "--quantity", help="MiB to transfer"),
    force: bool = typer.Option(
        False, "--force", help="Allow documented no-RMC deferred effect"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Explicit operator override for both ownership checks",
    ),
) -> None:
    """Move dedicated memory once; report both actual outcomes and persistence."""
    try:
        move = LparMemoryMove(quantity_mib=quantity, force=force)
        if any(not value.strip() for value in (source, destination, system)):
            raise ValueError("Source, destination and --system must be nonblank")
    except ValueError as error:
        usage_error(str(error))
    if not yes and not typer.confirm(
        f"Move {terminal_field(quantity)} MiB from '{terminal_field(source)}' to '{terminal_field(destination)}'? "
        "Partial/uncertain effects require inspection; force may require an OS restart"
    ):
        return
    result = with_client(
        lambda hmc: move_lpar_memory(
            hmc,
            source,
            destination,
            move,
            system_name_or_uuid=system,
            ownership_override=ownership_override,
        )
    )
    print_json(result)
    if not result["workflow_completed"]:
        raise typer.Exit(code=1)


def lpars_memory_statistics_reset(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Managed system name or UUID"
    ),
    wait_minutes: int | None = typer.Option(
        None,
        "--wait-minutes",
        help="Native guest wait; zero disables only the guest timer",
    ),
    detail_level: int | None = typer.Option(
        None, "--detail-level", help="Native detail level, 1–4"
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Use the existing audited ownership override",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Reset statistics history once and report independent observations.

    Reset verification remains unavailable; counter values do not prove causality.
    An incomplete outcome must be inspected before another attempt.
    """
    try:
        reset = LparMemoryStatisticsReset(
            wait_minutes=wait_minutes, detail_level=detail_level
        )
        if not name_or_uuid.strip() or not system.strip():
            raise ValueError("Partition and --system must be nonblank")
    except ValueError as error:
        usage_error(terminal_field(str(error)))
    if not yes and not typer.confirm(
        f"Reset memory statistics history on '{terminal_field(name_or_uuid)}'? History cannot be restored"
    ):
        return
    result = with_client(
        lambda hmc: reset_lpar_memory_statistics(
            hmc,
            name_or_uuid,
            reset,
            system_name_or_uuid=system,
            ownership_override=ownership_override,
        )
    )
    print_json(result)
    if not result["workflow_completed"]:
        raise typer.Exit(code=1)


def register_commands(group: typer.Typer) -> None:
    """Register native memory changes on the existing LPAR group."""
    group.command("memory", short_help="Adjust.")(lpars_memory)
    group.command("paging-switchover", short_help="Switch paging VIOS once.")(
        lpars_paging_switchover
    )
    group.command("memory-move", short_help="Move.")(lpars_memory_move)
    group.command("memory-statistics-reset", short_help="Reset statistics history.")(
        lpars_memory_statistics_reset
    )
