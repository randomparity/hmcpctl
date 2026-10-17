"""CLI commands for LPAR provisioning."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import cast

import typer
from rich.text import Text

from ...documents.lpar import PARTITION_TYPES, LparResources, validate_partition_type
from ...documents.storage import STORAGE_KINDS, StorageKind
from ...operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStorage,
    provision_lpar,
    validate_provision_assignments,
)
from ..output import (
    VerbatimTable,
    console,
    escape_controls,
    print_json,
    terminal_field,
    usage_error,
)
from ..runtime import with_client
from .assignment_input import load_pcie_assignments


def lpars_provision(
    system: str = typer.Option(
        ..., "--system", "-s", help="Target managed system name or UUID"
    ),
    name: str = typer.Option(..., "--name", "-n", help="Name for the new LPAR"),
    port_vlan_id: int = typer.Option(
        ..., "--vlan", help="Port VLAN ID for the network adapter"
    ),
    vios_uuid: str = typer.Option(
        ..., "--vios-uuid", help="UUID of the VIOS for vSCSI / storage"
    ),
    storage_name: str = typer.Option(
        ..., "--storage-name", help="VirtualDisk or PhysicalVolume name to map"
    ),
    partition_type: str = typer.Option(
        "AIX/Linux", "--type", help=f"Partition type: {', '.join(PARTITION_TYPES)}"
    ),
    min_memory: int = typer.Option(256, "--min-mem", help="Minimum memory (MiB)"),
    memory: int = typer.Option(4096, "--mem", help="Desired memory (MiB)"),
    max_memory: int = typer.Option(8192, "--max-mem", help="Maximum memory (MiB)"),
    vcpus: int = typer.Option(1, "--vcpus", help="Desired virtual CPUs"),
    max_vcpus: int = typer.Option(2, "--max-vcpus", help="Maximum virtual CPUs"),
    storage_kind: str = typer.Option(
        "VirtualDisk", "--storage-kind", help='"VirtualDisk" or "PhysicalVolume"'
    ),
    vg_uuid: str | None = typer.Option(
        None, "--vg-uuid", help="Volume group UUID to validate (optional)"
    ),
    power_on: bool = typer.Option(
        True, "--power-on/--no-power-on", help="Power on after provisioning"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate preconditions only; do not create"
    ),
    pcie_assignments: Path | None = typer.Option(
        None,
        "--pcie-assignments",
        help=(
            "JSON file using the declarative LparPcieAssignments schema; "
            "dedicated slots are refused"
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Provision a new LPAR end-to-end: create, add network adapter, map storage, power on.

    Always validates preconditions first (name uniqueness, VLAN existence, volume-group existence).
    Pass --dry-run to run precondition checks only without creating anything.
    On partial failure the completed steps are reported as "ok", the failed step as "error",
    and remaining steps as "skipped". No automatic rollback is performed.
    """
    assignments = load_pcie_assignments(pcie_assignments)
    try:
        validate_provision_assignments(assignments)
    except ValueError as exc:
        usage_error(f"--pcie-assignments: {exc}")

    try:
        lpar_type = validate_partition_type(partition_type)
    except ValueError as exc:
        usage_error(f"--type: {exc}")
    if storage_kind not in STORAGE_KINDS:
        usage_error(
            "--storage-kind must be one of "
            f"{', '.join(sorted(STORAGE_KINDS))}, got {storage_kind!r}"
        )
    if not dry_run and not yes:
        typer.confirm(
            (
                f"Provision LPAR '{terminal_field(name)}' on system '{terminal_field(system)}"
                f"' (VLAN {terminal_field(port_vlan_id)}, VIOS {terminal_field(vios_uuid)})?"
            )
            + (" [DRY RUN]" if dry_run else ""),
            abort=True,
        )

    request = ProvisionRequest(
        name=name,
        adapters=ProvisionAdapters(port_vlan_id),
        storage=ProvisionStorage(
            vios_uuid, storage_name, cast(StorageKind, storage_kind), vg_uuid
        ),
        resources=LparResources(
            min_memory=min_memory,
            desired_memory=memory,
            max_memory=max_memory,
            desired_vcpus=vcpus,
            max_vcpus=max_vcpus,
        ),
        partition_type=lpar_type,
        power_on=power_on,
        dry_run=dry_run,
        assignments=assignments,
    )
    result = with_client(
        lambda hmc: provision_lpar(
            hmc,
            system_name_or_uuid=system,
            request=request,
        )
    )

    _render_provision_result(result, name, dry_run, as_json)


def _render_provision_result(
    result: ProvisionResult, name: str, dry_run: bool, as_json: bool
) -> None:
    if as_json:
        print_json(asdict(result))
        return
    if dry_run:
        console.print(
            "[yellow]DRY RUN — preconditions validated, no LPAR created[/yellow]"
        )
    elif result.workflow_completed:
        console.print(
            f"LPAR '{terminal_field(name)}' provisioned successfully",
            style="green",
            markup=False,
        )
    elif result.resource_created:
        identity = result.lpar_uuid or "UUID unavailable"
        console.print(
            (
                f"LPAR '{terminal_field(name)}' was created ({terminal_field(identity)}"
                f"), but provisioning is incomplete — check step results"
            ),
            style="yellow",
            markup=False,
        )
    else:
        console.print(
            f"LPAR '{terminal_field(name)}' was not created — check step results",
            style="yellow",
            markup=False,
        )

    table = VerbatimTable(title=f"Provision steps: {name}")
    table.add_column("Step", style="cyan")
    table.add_column("Status", style="green")
    for step in result.steps:
        style = (
            "green"
            if step.status == "ok"
            else ("yellow" if step.status in ("dry_run", "skipped") else "red")
        )
        table.add_row(step.step, Text(escape_controls(step.status), style=style))
    console.print(table)
    for warning in result.warnings:
        console.print(
            f"Warning: {terminal_field(warning)}", style="yellow", markup=False
        )
    if result.change_location is not None:
        console.print(terminal_field(result.change_location.summary()), markup=False)


# LPAR Boot Order Commands


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("provision")(lpars_provision)
