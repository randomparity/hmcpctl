"""CLI commands for shared processor pool reads (REST, ADR 0375)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import typer

from ...operations.systems.processor_pools import (
    SharedProcessorPool,
    get_shared_processor_pool,
    list_shared_processor_pools,
)
from ..output import (
    InventoryFormat,
    err_console,
    print_inventory,
    resolve_inventory_format,
    terminal_field,
    usage_error,
)
from ..runtime import with_client

_COLUMNS = (
    "ID",
    "Name",
    "Configured",
    "Max Units",
    "Reserved",
    "Pending Reserved",
    "Available",
    "Partitions",
)


def _units(value: float | None) -> str:
    return "n/a" if value is None else str(value)


def _row(pool: SharedProcessorPool) -> tuple[Any, ...]:
    partitions = pool.assigned_partition_uuids
    return (
        pool.pool_id,
        pool.name,
        "yes" if pool.configured else "no",
        _units(pool.maximum_processing_units),
        _units(pool.reserved_processing_units),
        _units(pool.pending_reserved_processing_units),
        _units(pool.available_processing_units),
        "not reported" if partitions is None else ",".join(partitions) or "none",
    )


def processor_pools_list(
    system: str = typer.Argument(..., help="Managed system name or UUID"),
    include_unconfigured: bool = typer.Option(
        False, "--all", help="Show every pool slot, including unconfigured ones"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """List a managed system's shared processor pools (configured ones by default)."""
    selected = resolve_inventory_format(as_json, format)
    listing = with_client(
        lambda hmc: list_shared_processor_pools(
            hmc, system, include_unconfigured=include_unconfigured
        )
    )
    print_inventory(
        asdict(listing),
        selected,
        _COLUMNS,
        [_row(pool) for pool in listing.pools],
        title="Shared Processor Pools",
        empty_msg="No shared processor pools found",
    )
    if selected != InventoryFormat.json:
        err_console.print(
            terminal_field(
                f"rule: {listing.configured_rule}; "
                f"{listing.unconfigured_omitted} unconfigured slot(s) omitted",
                err=True,
            ),
            style="dim",
            markup=False,
        )


def processor_pools_get(
    system: str = typer.Argument(..., help="Managed system name or UUID"),
    pool_id: int | None = typer.Option(None, "--id", help="Pool ID (0 is the default)"),
    pool_name: str | None = typer.Option(None, "--name", help="Exact pool name"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """Show one shared processor pool slot by ID or exact name."""
    if (pool_id is None) == (pool_name is None):
        usage_error("select exactly one of --id or --name")
    selected = resolve_inventory_format(as_json, format)
    selector: int | str = pool_id if pool_id is not None else str(pool_name)
    result = with_client(lambda hmc: get_shared_processor_pool(hmc, system, selector))
    print_inventory(
        asdict(result),
        selected,
        _COLUMNS,
        [_row(result.pool)],
        title="Shared Processor Pool",
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module's commands on *group*."""
    group.command("list")(processor_pools_list)
    group.command("get")(processor_pools_get)
