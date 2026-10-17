"""CLI command for the LPAR decommission workflow."""

from __future__ import annotations

import shlex
from dataclasses import asdict
from typing import Any

import typer

from ...operations.lpar.decommission import DecommissionResult, decommission_lpar
from ...resource_identity import is_uuid
from ..output import (
    InventoryFormat,
    VerbatimTable,
    confirm_on_stderr,
    console,
    err_console,
    print_inventory,
    resolve_inventory_format,
    terminal_field,
)
from ..runtime import with_client


def lpars_decommission(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str = typer.Option(
        ...,
        "--system",
        "-s",
        help="Managed system name or UUID; required for scoped ownership and cleanup checks",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Inventory the blast radius without mutating"
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval",
    ),
    immediate: bool = typer.Option(
        False, "--immediate", help="Request immediate shutdown before deletion"
    ),
    timeout_seconds: int = typer.Option(
        300, "--timeout-seconds", help="Seconds to wait for the power-off job"
    ),
    poll_interval: int = typer.Option(
        5, "--poll-interval", help="Poll interval seconds for the power-off job"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON envelope"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Inventory and optionally decommission one LPAR; retain backing storage.

    Unproven mappings need inspection, not deletion. Use --dry-run first.
    """
    selected = resolve_inventory_format(as_json, format)
    if not dry_run and not yes:
        confirm_on_stderr(
            (
                f"Decommission LPAR '{terminal_field(name_or_uuid, err=True)}' on system '"
                f"{terminal_field(system, err=True)}'? This powers it off, detaches its vSCSI mappings "
                f"before client adapters and partition deletion, and retains backing storage."
            ),
            abort=True,
        )

    result = with_client(
        lambda hmc: decommission_lpar(
            hmc,
            system,
            name_or_uuid,
            dry_run=dry_run,
            ownership_override=ownership_override,
            immediate=immediate,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )

    _render_decommission_result(result, name_or_uuid, selected, system)

    if not result.dry_run and not result.workflow_completed:
        raise typer.Exit(1)


def _detail_rows(value: Any, path: str = ""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _detail_rows(item, f"{path}.{key}" if path else key)
    elif isinstance(value, (tuple, list)) and value:
        for index, item in enumerate(value):
            yield from _detail_rows(item, f"{path}[{index}]")
    else:
        yield path, "empty" if isinstance(value, (dict, tuple, list)) else value


def _inspection(row: dict[str, str], system: str) -> str:
    vios = row.get("vios_uuid", "unknown")
    if not is_uuid(vios):
        args = ["vios", "list", "--system", system, "--format", "tsv"]
    elif row.get("type") == "VirtualSCSIMapping":
        args = ["storage", "list-mappings", vios, "--system", system, "--format", "tsv"]
    else:
        args = [
            "raw",
            "get",
            f"/rest/api/uom/VirtualIOServer/{vios}?group=ViosSCSIMapping,ViosFCMapping",
        ]
    return shlex.join(["hmcpctl", *args])


def _render_decommission_result(
    result: DecommissionResult, name_or_uuid: str, format: InventoryFormat, system: str
) -> None:
    """Render scalar details, preserving the established JSON envelope."""
    if format == InventoryFormat.json:
        print_inventory(asdict(result), format)
        for evidence in result.blast_radius.get("mapping_evidence", ()):
            err_console.print(
                f"Inspect: {terminal_field(_inspection(evidence, system), err=True)}",
                markup=False,
                soft_wrap=True,
            )
    else:
        _render_details(result, name_or_uuid, format, system)
    for warning in result.warnings:
        err_console.print(
            f"Warning: {terminal_field(warning, err=True)}",
            markup=False,
            soft_wrap=True,
        )
    if result.blast_radius.get("mapping_evidence"):
        err_console.print(
            "Uncertainty is not proof of another tenant or permission to delete. "
            "Inspect without --lpar so unresolved mappings remain visible.",
            markup=False,
        )


def _render_details(
    result: DecommissionResult, name_or_uuid: str, format: InventoryFormat, system: str
) -> None:
    if result.dry_run:
        summary = "DRY RUN — decommission plan generated; no mutations performed"
    elif result.workflow_completed:
        summary = f"LPAR '{name_or_uuid}' decommissioned successfully"
    else:
        summary = (
            f"LPAR '{name_or_uuid}' was not fully decommissioned — check step results"
        )
    err_console.print(terminal_field(summary, err=True), markup=False, soft_wrap=True)
    rows = []
    for step in result.steps:
        rows.append(("step", step.step, "status", step.status))
        rows.extend(
            ("step", step.step, path, value)
            for path, value in _detail_rows(step.result, "result")
        )
    for index, evidence in enumerate(result.blast_radius.get("mapping_evidence", ())):
        rows.extend(("mapping", index, key, value) for key, value in evidence.items())
        rows.append(("mapping", index, "inspection", _inspection(evidence, system)))
    rows.append(("storage", "backing", "disposition", "retained"))
    if format == InventoryFormat.table:
        table = VerbatimTable(title="Decommission details")
        table.add_column("Field / value", overflow="fold")
        for section, item, key, value in rows:
            table.add_row(f"{section} {item} {key}: {value}")
        console.print(table)
    else:
        print_inventory(None, format, ("Section", "Item", "Field", "Value"), rows)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("decommission")(lpars_decommission)
