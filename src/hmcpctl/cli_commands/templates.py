"""CLI commands for the partition template library."""

from __future__ import annotations

import typer

from ..jobs import validate_wait_timing
from ..operations.templates.core import (
    deploy_partition_template,
    get_partition_template,
    list_partition_templates,
)
from .output import (
    VerbatimTable,
    console,
    first_field,
    output,
    print_json,
    terminal_field,
    usage_error,
)
from .runtime import with_client


def templates_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List partition templates in the template library."""

    templates = with_client(list_partition_templates)

    table = None
    if not as_json:
        table = VerbatimTable(title="Partition Templates")
        for col in ("Name", "UUID"):
            table.add_column(col)
        for t in templates:
            table.add_row(first_field(t, "partitionTemplateName"), t.get("UUID") or "-")
    output(templates, as_json, table, "No partition templates found")


def templates_show(uuid: str = typer.Argument(..., help="Template UUID")) -> None:
    """Show one partition template."""

    t = with_client(lambda hmc: get_partition_template(hmc, uuid))

    print_json(t)


def templates_deploy(
    draft_uuid: str = typer.Argument(..., help="Draft (transformed) template UUID"),
    system: str = typer.Option(
        ..., "--system", help="Target managed system name or UUID"
    ),
    wait: bool = typer.Option(False, "--wait", help="Wait for the deploy job"),
    timeout_seconds: int = typer.Option(300, "--timeout"),
    poll_interval: int = typer.Option(5, "--poll-interval"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Deploy a partition from a draft template (submits a job)."""
    try:
        validate_wait_timing(wait, timeout_seconds, poll_interval)
    except ValueError as exc:
        usage_error(str(exc))
    if not yes and not typer.confirm(
        f"Deploy draft template {terminal_field(draft_uuid)} to system {terminal_field(system)}?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: deploy_partition_template(
            hmc,
            draft_uuid,
            system,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )

    console.print(
        f"Deploy job for template {terminal_field(draft_uuid)}",
        style="green",
        markup=False,
    )
    print_json(result)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(templates_list)
    group.command("show")(templates_show)
    group.command("deploy")(templates_deploy)
