"""CLI commands for HMC jobs."""

from __future__ import annotations

from dataclasses import asdict

import typer

from ..operations import jobs as operations_jobs
from .output import console, err_console, print_json, terminal_field
from .runtime import with_client


def jobs_show(
    job_id: str = typer.Argument(..., help="JobID"),
    job_href: str | None = typer.Option(
        None, "--job-href", help="SELF link returned by job submission"
    ),
) -> None:
    """Show status/result of an HMC job."""

    outcome = with_client(
        lambda hmc: operations_jobs.get_job(hmc, job_id, job_href=job_href)
    )

    if not outcome.found:
        err_console.print(
            f"Job {terminal_field(job_id, err=True)} not found",
            style="yellow",
            markup=False,
        )
        raise typer.Exit(code=1)
    print_json(asdict(outcome))


def jobs_wait(
    job_id: str = typer.Argument(..., help="JobID to wait on"),
    timeout: int = typer.Option(300, "--timeout", "-t", help="Maximum seconds to wait"),
    interval: int = typer.Option(
        5, "--interval", "-i", help="Poll interval in seconds"
    ),
    job_href: str | None = typer.Option(
        None, "--job-href", help="SELF link returned by job submission"
    ),
) -> None:
    """Wait for an HMC job to reach a terminal state (COMPLETED_OK, COMPLETED_WITH_ERROR, ...).

    Prints the final job entry once a terminal state is reached or the
    timeout elapses.
    """

    outcome = with_client(
        lambda hmc: operations_jobs.wait_for_job(
            hmc,
            job_id,
            job_href=job_href,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
    )

    if not outcome.found:
        err_console.print(
            f"Job {terminal_field(job_id, err=True)} not found",
            style="yellow",
            markup=False,
        )
        raise typer.Exit(code=1)
    status = outcome.status or "unknown"
    console.print(
        f"Job {terminal_field(job_id)} status: {terminal_field(status)}",
        style="green",
        markup=False,
    )
    print_json(asdict(outcome))


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("show")(jobs_show)
    group.command("wait")(jobs_wait)
