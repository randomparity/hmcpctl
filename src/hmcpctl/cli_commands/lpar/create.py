"""CLI command for creating an LPAR."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import typer

from ...documents.lpar import (
    PARTITION_TYPES,
    LparReportingSettings,
    LparResources,
    LparSettings,
    validate_partition_type,
)
from ...operations.lpar.core import LparCreation
from ...operations.lpar.workflows import create_lpar
from ...ssh.lpar import validate_caller_token
from ..output import console, err_console, print_json, terminal_field, usage_error
from ..runtime import with_client
from .assignment_input import load_pcie_assignments, load_physical_io


def lpars_create(
    name: str = typer.Argument(..., help="Name for the new partition"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Target managed system UUID"
    ),
    partition_type: str = typer.Option(
        "AIX/Linux", "--type", help=f"One of: {', '.join(PARTITION_TYPES)}"
    ),
    partition_id: int | None = typer.Option(
        None, "--id", help="Partition ID (auto-assigned if omitted)"
    ),
    min_memory: int = typer.Option(256, "--min-mem", help="Minimum memory (MiB)"),
    memory: int = typer.Option(4096, "--mem", help="Desired memory (MiB)"),
    max_memory: int = typer.Option(8192, "--max-mem", help="Maximum memory (MiB)"),
    dedicated: bool = typer.Option(
        False, "--dedicated", help="Dedicated CPUs instead of shared"
    ),
    min_procs: float | None = typer.Option(
        None, "--min-procs", help="Min processing units / dedicated CPUs"
    ),
    procs: float | None = typer.Option(
        None, "--procs", help="Desired processing units / dedicated CPUs"
    ),
    max_procs: float | None = typer.Option(
        None, "--max-procs", help="Max processing units / dedicated CPUs"
    ),
    min_vcpus: int | None = typer.Option(
        None, "--min-vcpus", help="Min virtual processors (shared)"
    ),
    vcpus: int | None = typer.Option(
        1, "--vcpus", help="Desired virtual processors (shared)"
    ),
    max_vcpus: int | None = typer.Option(
        2, "--max-vcpus", help="Max virtual processors (shared)"
    ),
    capped: bool = typer.Option(
        False, "--capped", help="Cap shared CPU (default uncapped)"
    ),
    physical_io: Path | None = typer.Option(
        None,
        "--physical-io",
        help="Typed creation I/O JSON (64 KiB maximum); no implicit console default",
    ),
    pcie_assignments: Path | None = typer.Option(
        None,
        "--pcie-assignments",
        help="JSON file using the declarative LparPcieAssignments schema",
    ),
    caller_token: str | None = typer.Option(
        None,
        "--caller-token",
        help="Optional tracking reference embedded in the partition description "
        "as '\\[caller <token>]' (ADR 0064); 1–64 printable ASCII characters, "
        'no whitespace or , = " [ ] \\',
    ),
    no_apply: bool = typer.Option(
        False,
        "--no-apply",
        help="Leave the new partition profile unapplied when the HMC creates the "
        "partition through mksyscfg (the partition then has no current configuration)",
    ),
    allow_perf_collection: bool | None = typer.Option(
        None, "--allow-perf-collection/--no-allow-perf-collection"
    ),
    lpar_avail_priority: int | None = typer.Option(
        None, "--lpar-avail-priority", min=0, max=255
    ),
    time_ref: bool | None = typer.Option(None, "--time-ref/--no-time-ref"),
    simplified_remote_restart_capable: bool | None = typer.Option(
        None,
        "--simplified-remote-restart-capable/--no-simplified-remote-restart-capable",
    ),
    migration_disabled: bool | None = typer.Option(
        None, "--migration-disabled/--no-migration-disabled"
    ),
    os400_restricted_io_mode: bool | None = typer.Option(
        None, "--os400-restricted-io-mode/--no-os400-restricted-io-mode"
    ),
    work_group_id: str | None = typer.Option(
        None, "--work-group-id", help="Integer EWLM ID or none"
    ),
    electronic_err_reporting: bool | None = typer.Option(
        None, "--electronic-err-reporting/--no-electronic-err-reporting"
    ),
    redundant_err_path_reporting: bool | None = typer.Option(
        None, "--redundant-err-path-reporting/--no-redundant-err-path-reporting"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Create a new LPAR on a managed system.

    Creates the partition powered off with a default profile; storage/network
    and boot settings are configured afterwards via the HMC. When the HMC
    creates it through mksyscfg, the profile is then applied (an
    ``apply_profile`` step) unless --no-apply is given. Settings preserve omitted
    values; a running time-reference partition can govern system time.
    """
    try:
        reporting = LparReportingSettings(
            os400_restricted_io_mode=os400_restricted_io_mode,
            work_group_id=None
            if work_group_id is None
            else "none"
            if work_group_id == "none"
            else int(work_group_id),
            electronic_err_reporting=electronic_err_reporting,
            redundant_err_path_reporting=redundant_err_path_reporting,
        )
        reporting.validate_context("create", partition_type)
    except ValueError as error:
        usage_error(
            f"Invalid reporting settings: {error}; work-group ID must be integer or none"
        )
    if caller_token is not None:
        validate_caller_token(caller_token)
    try:
        lpar_type = validate_partition_type(partition_type)
    except ValueError as exc:
        usage_error(f"--type: {exc}")
    if not yes:
        typer.confirm(
            (
                f"Create LPAR '{terminal_field(name)}' ({terminal_field(partition_type)}, "
                f"{terminal_field(memory)} MiB) on system {terminal_field(system)}?"
            ),
            abort=True,
        )
    resources = LparResources(
        min_memory=min_memory,
        desired_memory=memory,
        max_memory=max_memory,
        dedicated=dedicated,
        min_procs=min_procs,
        desired_procs=procs,
        max_procs=max_procs,
        min_vcpus=min_vcpus,
        desired_vcpus=vcpus,
        max_vcpus=max_vcpus,
        uncapped=not capped,
    )
    assignments = load_pcie_assignments(pcie_assignments)

    result = with_client(
        lambda hmc: create_lpar(
            hmc,
            system,
            LparCreation(
                name,
                lpar_type,
                resources,
                partition_id=partition_id,
                caller_token=caller_token,
                physical_io=load_physical_io(physical_io),
                apply_profile=not no_apply,
                reporting_settings=reporting,
                settings=LparSettings(
                    allow_perf_collection=allow_perf_collection,
                    lpar_avail_priority=lpar_avail_priority,
                    time_ref=time_ref,
                    simplified_remote_restart_capable=simplified_remote_restart_capable,
                    migration_disabled=migration_disabled,
                ),
            ),
            assignments,
        )
    )

    console.print(f"Created LPAR '{terminal_field(name)}'", style="green", markup=False)
    print_json(result.lpar)
    for warning in result.warnings:
        err_console.print(
            f"Warning: {terminal_field(warning, err=True)}",
            style="yellow",
            markup=False,
        )
    if result.steps:
        print_json(asdict(result))
    if not result.workflow_completed:
        raise typer.Exit(1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("create")(lpars_create)
