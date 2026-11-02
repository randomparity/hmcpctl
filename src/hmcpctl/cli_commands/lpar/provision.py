"""CLI commands for LPAR provisioning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import typer
from rich.text import Text

from ...client.core import HMCClient
from ...documents.lpar import PARTITION_TYPES, LparResources, validate_partition_type
from ...documents.storage import STORAGE_KINDS, StorageKind
from ...operations.logical import engine, store
from ...operations.logical.engine import MAX_WAIT_SECONDS, OperationRequest
from ...operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionResult,
    ProvisionStorage,
    preview_provision,
    validate_provision_assignments,
)
from ...operations.lpar.provision_operation import (
    json_value,
    provision_body,
    provision_classifiers,
    request_from_arguments,
    result_from_record,
)
from ..output import (
    VerbatimTable,
    console,
    escape_controls,
    fail,
    print_json,
    terminal_field,
    usage_error,
)
from ..runtime import current_options, ssh_config, with_client
from .assignment_input import load_pcie_assignments, load_physical_io

TOOL = "hmc_provision_lpar"
CONTINUATIONS = ("none", "resume", "abandon")


def lpars_provision(
    request_id: str = typer.Option(
        ...,
        "--request-id",
        help="Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ - (ADR 0313)",
    ),
    continuation: str = typer.Option(
        "none",
        "--continuation",
        help="none, resume or abandon; a continuation reuses the recorded request",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Target managed system name or UUID"
    ),
    name: str | None = typer.Option(None, "--name", "-n", help="Name for the new LPAR"),
    port_vlan_id: int | None = typer.Option(
        None, "--vlan", help="Port VLAN ID for the network adapter"
    ),
    vios_uuid: str | None = typer.Option(
        None, "--vios-uuid", help="UUID of the VIOS for vSCSI / storage"
    ),
    storage_name: str | None = typer.Option(
        None, "--storage-name", help="VirtualDisk or PhysicalVolume name to map"
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
        None, "--vg-uuid", help="Volume group UUID (required with --capacity-mib)"
    ),
    capacity_mib: int | None = typer.Option(
        None,
        "--capacity-mib",
        help="Create --storage-name as a new owned virtual disk of this size",
    ),
    power_on: bool = typer.Option(
        True, "--power-on/--no-power-on", help="Power on after provisioning"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate preconditions only; record nothing"
    ),
    physical_io: Path | None = typer.Option(
        None,
        "--physical-io",
        help="Typed creation I/O JSON (64 KiB maximum); no implicit console default",
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
    """Provision a new LPAR as one journaled operation: create, stamp, network, storage, power on.

    Always validates preconditions first (name uniqueness, VLAN existence, volume-group
    existence). --dry-run runs only those checks and records nothing. Otherwise the run is
    recorded under --request-id (ADR 0313): repeating it returns the same operation, and a
    paused run continues with --continuation resume (or stops with abandon) from this
    command or hmc_provision_lpar under the same agent id and connection. The command waits
    for the run to end and exits 1 unless it completed. No automatic rollback is performed.
    """
    try:
        store.validate_request_id(request_id)
    except ValueError as exc:
        usage_error(f"--request-id: {exc}")
    if continuation not in CONTINUATIONS:
        usage_error(f"--continuation must be one of {', '.join(CONTINUATIONS)}")
    if continuation != "none":
        if dry_run:
            usage_error("--dry-run cannot continue an operation")
        _run(request_id, continuation, None, as_json)
        return
    missing = [
        flag
        for flag, value in (
            ("--system", system),
            ("--name", name),
            ("--vlan", port_vlan_id),
            ("--vios-uuid", vios_uuid),
            ("--storage-name", storage_name),
        )
        if value is None
    ]
    if missing:
        usage_error(f"a new operation needs {', '.join(missing)}")
    try:
        validate_partition_type(partition_type)
    except ValueError as exc:
        usage_error(f"--type: {exc}")
    assignments = load_pcie_assignments(pcie_assignments)
    try:
        validate_provision_assignments(assignments)
    except ValueError as exc:
        usage_error(f"--pcie-assignments: {exc}")
    if storage_kind not in STORAGE_KINDS:
        usage_error(
            "--storage-kind must be one of "
            f"{', '.join(sorted(STORAGE_KINDS))}, got {storage_kind!r}"
        )
    arguments = {
        "system_name_or_uuid": system,
        "name": name,
        "adapters": json_value(ProvisionAdapters(port_vlan_id or 0)),
        "storage": json_value(
            ProvisionStorage(
                vios_uuid or "",
                storage_name or "",
                cast(StorageKind, storage_kind),
                vg_uuid,
                capacity_mib,
            )
        ),
        "resources": json_value(
            LparResources(
                min_memory=min_memory,
                desired_memory=memory,
                max_memory=max_memory,
                desired_vcpus=vcpus,
                max_vcpus=max_vcpus,
            )
        ),
        "partition_type": partition_type,
        "power_on": power_on,
        "assignments": json_value(assignments),
        "physical_io": json_value(load_physical_io(physical_io)),
    }
    try:
        request_from_arguments(arguments)
    except ValueError as exc:
        usage_error(str(exc))
    system_name, request = request_from_arguments(arguments)
    if (
        dry_run
        or engine.recorded_arguments(
            ssh_config().agent_id or "hmcpctl", request_id, TOOL
        )
        is None
    ):
        # Preconditions refuse a new operation before anything is recorded.
        result = with_client(lambda hmc: preview_provision(hmc, system_name, request))
        if dry_run:
            _render_provision_result(result, request.name, True, as_json)
            return
    if not yes:
        typer.confirm(
            f"Provision LPAR '{terminal_field(name)}' on system '{terminal_field(system)}"
            f"' (VLAN {terminal_field(port_vlan_id)}, VIOS {terminal_field(vios_uuid)})?",
            abort=True,
        )
    _run(request_id, continuation, arguments, as_json)


def _run(
    request_id: str,
    continuation: str,
    arguments: Mapping[str, Any] | None,
    as_json: bool,
) -> None:
    """Submit, wait for the worker to end, render, and exit 1 unless it completed."""
    config = ssh_config()
    agent_id = config.agent_id or "hmcpctl"
    try:
        if arguments is None:
            arguments = engine.recorded_arguments(agent_id, request_id, TOOL)
            if arguments is None:
                usage_error(f"no operation has request_id {request_id}")
        system, request = request_from_arguments(arguments)
        record = engine.submit(
            OperationRequest(
                TOOL,
                agent_id,
                store.connection_label(current_options().profile, tool=TOOL),
                config.host,
                request_id,
                arguments if continuation == "none" else {},
            ),
            provision_body(system, request, lambda _connection: HMCClient(config)),
            classifiers=provision_classifiers(
                request, lambda _connection: HMCClient(config)
            ),
            continuation=continuation,
            wait_seconds=MAX_WAIT_SECONDS,
        )
        # The worker dies with this process, so wait for it rather than return early.
        engine.join(record.operation_id, None)
        with store.session() as conn:
            record = store.read_record(conn, record.operation_id)
    except ValueError as exc:
        fail(exc)
    result = result_from_record(record)
    _render_provision_result(result, request.name, False, as_json)
    ended = record.outcome == "completed" or (
        continuation == "abandon" and record.outcome == "abandoned"
    )
    if not ended:
        raise typer.Exit(code=1)


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
    elif result.resource_created is None:
        console.print(
            f"LPAR '{terminal_field(name)}' creation outcome is unknown. "
            "Inspect the scoped partition and profile before retrying; "
            "no rollback performed.",
            style="yellow",
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
    if result.operation is not None:
        operation = result.operation
        status = f"{operation.state}/{operation.outcome or 'running'}"
        console.print(
            f"Operation {terminal_field(operation.operation_id)}: "
            f"{terminal_field(status)} (phase {terminal_field(operation.phase)})",
            markup=False,
        )
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
