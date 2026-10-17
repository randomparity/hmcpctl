"""CLI commands for managed systems."""

from __future__ import annotations

from dataclasses import asdict

import typer

from ...jobs import validate_wait_timing
from ...operations.inventory.capacity import fetch_capacity_report, find_placement
from ...operations.inventory.composite import fetch_system_summary
from ...operations.systems.core import (
    get_system,
    list_systems,
    power_system,
)
from ...operations.systems.health import fetch_fleet_health
from ...xmlutil import render_mtms
from ..output import (
    InventoryFormat,
    _resource,
    console,
    err_console,
    inventory_field,
    print_inventory,
    print_json,
    resolve_inventory_format,
    terminal_field,
)
from ..runtime import with_client


def systems_health(
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Show issue-only health across the managed estate."""
    selected = resolve_inventory_format(as_json, format)
    result = asdict(with_client(fetch_fleet_health))
    rows = [
        (
            category,
            entry.get("name"),
            entry.get("uuid"),
            entry.get("state"),
            entry.get("rmc_state"),
            entry.get("system_name"),
            entry.get("system_uuid"),
        )
        for category in ("systems", "vios", "lpars")
        for entry in result[category]
    ]
    print_inventory(
        result,
        selected,
        ("Category", "Name", "UUID", "State", "RMC", "System", "System UUID"),
        rows,
        title="Fleet Health",
        empty_msg="No fleet health issue rows returned",
    )
    for warning in result["warnings"]:
        err_console.print(
            terminal_field(warning, err=True), style="yellow", markup=False
        )


def systems_list(
    state: str | None = typer.Option(
        None, "--state", help="Show only systems whose State is exactly this"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """List managed systems."""
    selected = resolve_inventory_format(as_json, format)
    systems = with_client(lambda hmc: list_systems(hmc, state))
    print_inventory(
        systems,
        selected,
        ("Name", "UUID", "State", "MTMS", "IP Address"),
        [
            (
                inventory_field(s, "SystemName"),
                s.get("UUID"),
                inventory_field(s, "State"),
                render_mtms(_resource(s)) or None,
                inventory_field(s, "IPAddress", "PrimaryIPAddress"),
            )
            for s in systems
        ],
        title="Managed Systems",
        empty_msg="No managed systems found",
    )


def systems_show(
    name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Show full details of one managed system (accepts name or UUID)."""
    selected = resolve_inventory_format(as_json, format)
    system = with_client(lambda hmc: get_system(hmc, name_or_uuid))
    if system is None:
        err_console.print(
            f"System '{terminal_field(name_or_uuid, err=True)}' not found",
            style="yellow",
            markup=False,
        )
        raise typer.Exit(code=1)
    print_inventory(system, selected, title="Managed System")


def systems_power_on(
    name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Power on a managed system (submits a PowerOn job)."""
    validate_wait_timing(wait, timeout, interval)
    if not yes and not typer.confirm(
        f"Really PowerOn system {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_system(
            hmc,
            name_or_uuid,
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


def systems_power_off(
    name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    immediate: bool = typer.Option(False, "--immediate"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Power off a managed system (submits a PowerOff job)."""
    validate_wait_timing(wait, timeout, interval)
    op = "Immediate PowerOff" if immediate else "PowerOff"
    if not yes and not typer.confirm(
        f"Really {terminal_field(op)} system {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_system(
            hmc,
            name_or_uuid,
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


def systems_summary(
    name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """One-call summary: state, MTMS, firmware, LPAR counts, free memory/CPU, VIOS count."""
    selected = resolve_inventory_format(as_json, format)
    result = asdict(with_client(lambda hmc: fetch_system_summary(hmc, name_or_uuid)))
    print_inventory(result, selected, title="System Summary")
    for warning in result.get("warnings") or ():
        err_console.print(
            terminal_field(warning, err=True), style="yellow", markup=False
        )


def systems_capacity(
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Capacity report: memory/CPU totals and free resources per managed system."""
    selected = resolve_inventory_format(as_json, format)
    report = [asdict(item) for item in with_client(fetch_capacity_report)]
    fields = (
        "system_name",
        "system_uuid",
        "total_memory_mib",
        "assigned_memory_mib",
        "free_memory_mib",
        "total_proc_units",
        "assigned_proc_units",
        "free_proc_units",
        "running_lpars",
        "total_lpars",
    )
    print_inventory(
        report,
        selected,
        (
            "System",
            "UUID",
            "Total Mem (MiB)",
            "Assigned Mem (MiB)",
            "Free Mem (MiB)",
            "Total Procs",
            "Assigned Procs",
            "Free Procs",
            "Running LPARs",
            "Total LPARs",
        ),
        [tuple(row.get(field) for field in fields) for row in report],
        title="System Capacity",
        empty_msg="No managed systems found",
    )


def systems_find_placement(
    memory: int = typer.Argument(..., help="Desired memory in MiB"),
    procs: float = typer.Option(0.5, "--procs", help="Desired processor units"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Find managed systems with enough free resources for a new LPAR."""
    selected = resolve_inventory_format(as_json, format)
    candidates = [
        asdict(item)
        for item in with_client(lambda hmc: find_placement(hmc, memory, procs))
    ]
    fields = (
        "system_name",
        "system_uuid",
        "free_memory_mib",
        "free_proc_units",
        "running_lpars",
    )
    print_inventory(
        candidates,
        selected,
        ("System", "UUID", "Free Mem (MiB)", "Free Procs", "Running LPARs"),
        [tuple(row.get(field) for field in fields) for row in candidates],
        title="Placement Candidates (sorted by free memory)",
        empty_msg="No placement candidates",
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("health")(systems_health)
    group.command("list")(systems_list)
    group.command("show")(systems_show)
    group.command("power-on")(systems_power_on)
    group.command("power-off")(systems_power_off)
    group.command("summary")(systems_summary)
    group.command("capacity")(systems_capacity)
    group.command("find-placement")(systems_find_placement)
