"""Native current-processor quantity commands (ADR 0296)."""

from __future__ import annotations

from typing import Literal

import typer

from ...documents.lpar_processors import LparProcessorChange, LparProcessorMove
from ...operations.lpar.processor_move import move_lpar_processors
from ...operations.lpar.processors import change_lpar_processors
from ..output import print_json, terminal_field, usage_error
from ..runtime import with_client


def lpars_processors(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Managed system name or UUID"
    ),
    operation: Literal["add", "remove", "set"] = typer.Option(..., "--operation"),
    procs: int | None = typer.Option(
        None, "--procs", help="Whole dedicated CPUs or shared virtual processors"
    ),
    procunits: str | None = typer.Option(
        None, "--procunits", help="Shared entitlement, at most two decimal places"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Admit AIX/Linux/VIOS without RMC for deferred restart effect",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval",
    ),
) -> None:
    """Add, remove or set current processor quantities and verify independent readback."""
    try:
        change = LparProcessorChange(
            operation=operation,
            processors=procs,
            processing_units=procunits,
            force=force,
        )
        if not system.strip():
            raise ValueError("--system must name a managed system")
    except ValueError as error:
        usage_error(str(error))
    if not yes and not typer.confirm(
        f"Apply processor change to '{terminal_field(name_or_uuid)}'? "
        "On a shutdown partition, reduction below minimum or set zero clears processor AND memory assignments; "
        "forced changes may require an operating-system restart"
    ):
        return
    result = with_client(
        lambda hmc: change_lpar_processors(
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


def lpars_processor_move(
    source: str = typer.Argument(..., help="Source partition name or UUID"),
    destination: str = typer.Argument(..., help="Destination partition name or UUID"),
    system: str = typer.Option(..., "--system", "-s", help="Containing managed system"),
    procs: int | None = typer.Option(
        None, "--procs", help="Whole processors to transfer"
    ),
    procunits: str | None = typer.Option(
        None, "--procunits", help="Entitlement to transfer, at most two decimal places"
    ),
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
    """Move processor quantities once and independently observe both partitions."""
    try:
        move = LparProcessorMove(
            processors=procs, processing_units=procunits, force=force
        )
        if any(not value.strip() for value in (source, destination, system)):
            raise ValueError("Source, destination and --system must be nonblank")
    except ValueError as error:
        usage_error(str(error))
    if not yes and not typer.confirm(
        f"Move processor quantities from '{terminal_field(source)}' to '{terminal_field(destination)}'? "
        "Partial/uncertain effects require inspection; force may require an OS restart"
    ):
        return
    result = with_client(
        lambda hmc: move_lpar_processors(
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


def register_commands(group: typer.Typer) -> None:
    """Register native processor quantities on the existing LPAR group."""
    group.command("processors", short_help="Adjust.")(lpars_processors)
    group.command("processor-move", short_help="Move.")(lpars_processor_move)
