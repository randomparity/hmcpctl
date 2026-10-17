"""LPAR inventory CLI commands."""

from __future__ import annotations

from dataclasses import asdict

import typer

from ...operations.inventory.composite import fetch_lpar_summary
from ...operations.lpar.core import (
    get_lpar,
    get_lpar_state,
    list_lpars,
)
from ...operations.partition_state import PartitionState
from ...resource_identity import ResourceNotFoundError
from ...ssh.refcodes import MAX_REFCODE_COUNT, list_lpar_refcodes
from ..output import (
    InventoryFormat,
    console,
    inventory_field,
    partition_not_found,
    print_inventory,
    report_unreadable,
    resolve_inventory_format,
    terminal_field,
)
from ..runtime import run_cli_coroutine, ssh_config, with_client


def lpars_summary(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """One-call summary: state, RMC, memory/CPU, OS details, adapter count, description."""
    selected = resolve_inventory_format(as_json, format)
    summary = asdict(
        with_client(lambda hmc: fetch_lpar_summary(hmc, system, name_or_uuid))
    )
    print_inventory(summary, selected, title="LPAR Summary")


def lpars_list(
    system: str | None = typer.Option(
        None, "--system", "-s", help="Restrict to this managed system name or UUID"
    ),
    state: PartitionState | None = typer.Option(
        None, "--state", help="Filter by PartitionState"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """List logical partitions."""
    selected = resolve_inventory_format(as_json, format)
    listing = with_client(lambda hmc: list_lpars(hmc, system, state))
    report_unreadable(listing)
    rows = []
    for entry in listing.entries:
        parent = entry.get("ManagedSystem") or {}
        rows.append(
            (
                parent.get("SystemName"),
                parent.get("UUID"),
                inventory_field(entry, "PartitionName"),
                inventory_field(entry, "PartitionID"),
                entry.get("UUID"),
                inventory_field(entry, "PartitionState"),
                inventory_field(entry, "PartitionType"),
                inventory_field(entry, "OperatingSystemVersion"),
                inventory_field(entry, "ResourceMonitoringControlState", "RMCState"),
            )
        )
    print_inventory(
        asdict(listing),
        selected,
        ("System", "System UUID", "Name", "ID", "UUID", "State", "Type", "OS", "RMC"),
        rows,
        title="Logical Partitions",
        empty_msg="No logical partitions found",
    )


def lpars_show(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Restrict to this managed system name or UUID"
    ),
) -> None:
    """Show one LPAR, looked up by name (exact) or by UUID."""
    selected = resolve_inventory_format(as_json, format)
    lpar = with_client(
        lambda hmc: get_lpar(hmc, name_or_uuid, system_name_or_uuid=system)
    )
    if lpar is None:
        partition_not_found(name_or_uuid)
    print_inventory(lpar, selected, title="Logical Partition")


def lpars_state(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Restrict to this managed system name or UUID"
    ),
) -> None:
    """Print just the current state of an LPAR."""

    async def state_or_none(hmc):
        try:
            return await get_lpar_state(hmc, name_or_uuid, system_name_or_uuid=system)
        except ResourceNotFoundError as exc:
            if exc.resource_kind == "LPAR":
                return None
            raise

    state = with_client(state_or_none)

    if state is None:
        partition_not_found(name_or_uuid)
    console.print(terminal_field(state), markup=False)


def lpars_refcodes(
    system_name: str = typer.Argument(..., help="Managed system name"),
    lpar_name: str = typer.Argument(..., help="Partition name"),
    count: int = typer.Option(
        1, "--count", help=f"Reference codes to return, 1 to {MAX_REFCODE_COUNT}"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Read recent reference codes (SRCs) for one partition (HMC CLI via SSH)."""
    selected = resolve_inventory_format(as_json, format)
    rows = run_cli_coroutine(
        lambda: list_lpar_refcodes(ssh_config(), system_name, lpar_name, count)
    )
    cells = [
        (row["lpar_name"], row["time_stamp"], row["refcode"], row["definition"])
        for row in rows
    ]
    if selected == InventoryFormat.table:
        cells = [
            (name, stamp, code, definition or "Unknown definition")
            for name, stamp, code, definition in cells
        ]
    print_inventory(
        rows,
        selected,
        ("Partition", "Timestamp", "Code", "Definition"),
        cells,
        title="Reference codes",
        empty_msg="No reference codes found",
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("summary")(lpars_summary)
    group.command("list")(lpars_list)
    group.command("show")(lpars_show)
    group.command("state")(lpars_state)
    group.command("refcodes")(lpars_refcodes)
