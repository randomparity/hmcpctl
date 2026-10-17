"""CLI commands for shared memory pools (HMC CLI via SSH)."""

from __future__ import annotations

import typer

from ...ssh.memory import list_memory_pools, remove_memory_pool
from ..output import VerbatimTable, console, err_console, print_json, terminal_field
from ..runtime import run_cli_coroutine, ssh_config


def memory_pools_list(
    system_name: str = typer.Argument(..., help="Managed system name"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List the shared memory pool on a managed system (HMC CLI via SSH)."""
    config = ssh_config()
    pools = run_cli_coroutine(lambda: list_memory_pools(config, system_name))
    if as_json:
        print_json(pools)
        return

    if not pools:
        err_console.print("[yellow]No memory pools found[/yellow]")
        return

    table = VerbatimTable(title=f"Memory Pools — {system_name}")
    for key in pools[0]:
        table.add_column(key)
    for pool in pools:
        table.add_row(*pool.values())
    console.print(table)


def memory_pools_remove(
    system_name: str = typer.Argument(..., help="Managed system name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove the shared memory pool after confirming operator intent."""
    if not yes and not typer.confirm(
        f"Remove the shared memory pool on system '{terminal_field(system_name)}'?"
    ):
        raise typer.Abort()

    config = ssh_config()
    result = run_cli_coroutine(lambda: remove_memory_pool(config, system_name))

    console.print(
        f"Shared memory pool removed from '{terminal_field(system_name)}'",
        style="green",
        markup=False,
    )
    if result.strip():
        console.print(terminal_field(result.strip()), markup=False)


def register_commands(group: typer.Typer) -> None:
    """Register this module's commands on *group*."""
    group.command("list")(memory_pools_list)
    group.command("remove")(memory_pools_remove)
