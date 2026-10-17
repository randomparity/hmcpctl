"""CLI commands for Virtual I/O Servers."""

from __future__ import annotations

from dataclasses import asdict

import typer

from ...jobs import validate_wait_timing
from ...operations.partition_state import PartitionState
from ...operations.vios.core import list_vios, power_vios
from ..output import (
    InventoryFormat,
    console,
    inventory_field,
    print_inventory,
    print_json,
    report_unreadable,
    resolve_inventory_format,
    terminal_field,
)
from ..runtime import with_client


def vios_list(
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
    """List Virtual I/O Servers."""
    selected = resolve_inventory_format(as_json, format)
    listing = with_client(lambda hmc: list_vios(hmc, system, state))
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
                inventory_field(entry, "IOSLevel", "VIOSVersion"),
            )
        )
    print_inventory(
        asdict(listing),
        selected,
        ("System", "System UUID", "Name", "ID", "UUID", "State", "Version"),
        rows,
        title="Virtual I/O Servers",
        empty_msg="No VIOS found",
    )


def vios_power_on(
    name_or_uuid: str = typer.Argument(..., help="VIOS name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Power on a VIOS (submits a PowerOn job)."""
    validate_wait_timing(wait, timeout, interval)
    if not yes and not typer.confirm(
        f"Really PowerOn VIOS {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_vios(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            power_on=True,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
    )

    console.print(
        f"Submitted PowerOn for {terminal_field(name_or_uuid)}",
        style="green",
        markup=False,
    )
    print_json(job)


def vios_power_off(
    name_or_uuid: str = typer.Argument(..., help="VIOS name or UUID"),
    immediate: bool = typer.Option(False, "--immediate"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Power off a VIOS (submits a PowerOff job)."""
    validate_wait_timing(wait, timeout, interval)
    op = "Immediate PowerOff" if immediate else "PowerOff"
    if not yes and not typer.confirm(
        f"Really {terminal_field(op)} VIOS {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_vios(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            power_on=False,
            immediate=immediate,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
    )

    console.print(
        f"Submitted {terminal_field(op)} for {terminal_field(name_or_uuid)}",
        style="green",
        markup=False,
    )
    print_json(job)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(vios_list)
    group.command("power-on")(vios_power_on)
    group.command("power-off")(vios_power_off)
