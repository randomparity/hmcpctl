"""CLI commands for PCM performance/capacity metrics."""

from __future__ import annotations

import typer

from ..operations.metrics.pcm import (
    PcmCategory,
    fetch_metric_data,
    fetch_metric_links,
    get_pcm_preferences,
    preference_flags,
    set_pcm_preferences,
    validate_pcm_metric_target,
    validate_pcm_preferences_category,
)
from .output import console, print_json, terminal_field, usage_error
from .runtime import with_client


def metrics_prefs(
    category: PcmCategory = typer.Argument(
        ..., help="ManagedSystem (LogicalPartition preferences are unavailable)"
    ),
    resource_name_or_uuid: str = typer.Argument(..., help="Resource name or UUID"),
) -> None:
    """Show PCM monitoring preferences for a resource."""
    validate_pcm_preferences_category(category)

    prefs = with_client(
        lambda hmc: get_pcm_preferences(hmc, category, resource_name_or_uuid)
    )

    print_json(prefs)


def metrics_set_prefs(
    category: PcmCategory = typer.Argument(
        ..., help="ManagedSystem (LogicalPartition preferences are unavailable)"
    ),
    resource_name_or_uuid: str = typer.Argument(..., help="Resource name or UUID"),
    ltm: bool | None = typer.Option(
        None, "--ltm/--no-ltm", help="Long-term monitoring"
    ),
    aggregation: bool | None = typer.Option(None, "--aggregation/--no-aggregation"),
    stm: bool | None = typer.Option(
        None, "--stm/--no-stm", help="Short-term monitoring"
    ),
    compute_ltm: bool | None = typer.Option(
        None, "--compute-ltm/--no-compute-ltm", help="Compute long-term monitoring"
    ),
    energy: bool | None = typer.Option(
        None, "--energy/--no-energy", help="Energy monitoring"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Enable/disable PCM data collection for a resource.

    Mutates HMC monitoring configuration, so it is gated by the same
    --yes/confirm convention as every other mutating CLI command.
    """
    flags = preference_flags(ltm, aggregation, stm, compute_ltm, energy)
    if not flags:
        usage_error("No flags supplied; nothing to change.")
    validate_pcm_preferences_category(category)

    if not yes and not typer.confirm(
        f"Enable/disable PCM monitoring on {terminal_field(category)} "
        f"{terminal_field(resource_name_or_uuid)}?"
    ):
        raise typer.Abort()

    with_client(
        lambda hmc: set_pcm_preferences(hmc, category, resource_name_or_uuid, flags)
    )

    console.print(
        (
            f"Updated {terminal_field(category)} {terminal_field(resource_name_or_uuid)}"
            f": {terminal_field(flags)}"
        ),
        style="green",
        markup=False,
    )


def metrics_show(
    category: PcmCategory = typer.Argument(
        ..., help="ManagedSystem or LogicalPartition"
    ),
    resource_name_or_uuid: str = typer.Argument(..., help="Resource name or UUID"),
    start: str = typer.Option(..., "--start", help="Start TS yyyy-MM-ddTHH:mm:ssZ"),
    end: str | None = typer.Option(None, "--end", help="End TS (optional)"),
    samples: int | None = typer.Option(None, "--samples", help="Number of samples"),
    aggregated: bool = typer.Option(
        False, "--aggregated", help="Use aggregated (long-term) metrics"
    ),
    fetch: bool = typer.Option(
        False, "--fetch", help="Also download the latest JSON doc"
    ),
    system_name_or_uuid: str | None = typer.Option(
        None,
        "--system",
        help="Owning managed system; required for LogicalPartition",
    ),
) -> None:
    """Get PCM metrics (processed by default; --aggregated for rollups)."""
    validate_pcm_metric_target(category, system_name_or_uuid)

    kind = "aggregated" if aggregated else "processed"
    operation = fetch_metric_data if fetch else fetch_metric_links
    result = with_client(
        lambda hmc: operation(
            hmc,
            category,
            resource_name_or_uuid,
            kind=kind,
            start_ts=start,
            end_ts=end,
            no_of_samples=samples,
            system_name_or_uuid=system_name_or_uuid,
        )
    )

    print_json(result)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("prefs")(metrics_prefs)
    group.command("set-prefs")(metrics_set_prefs)
    group.command("show")(metrics_show)
