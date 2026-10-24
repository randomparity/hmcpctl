"""Native current-memory quantity commands (ADR 0285)."""

from __future__ import annotations

from typing import Literal

import typer

from ...documents.lpar_memory import LparMemoryChange
from ...operations.lpar.memory import change_lpar_memory
from ..output import print_json, terminal_field, usage_error
from ..runtime import with_client


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


def register_commands(group: typer.Typer) -> None:
    """Register native memory changes on the existing LPAR group."""
    group.command("memory", short_help="Change memory.")(lpars_memory)
