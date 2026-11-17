"""CLI commands for LPAR provisioning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import typer
from pydantic import ValidationError
from rich.text import Text
from typer._click.core import ParameterSource

from hmcpctl.operations.lpar.provisioning.core import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStorage,
    preview_provision,
    validate_provision_assignments,
)
from hmcpctl.operations.lpar.provisioning.media import MediaMismatch
from hmcpctl.operations.lpar.provisioning.operation import (
    json_value,
    provision_body,
    provision_classifiers,
    request_from_arguments,
    result_from_record,
)

from ...client.core import HMCClient
from ...documents.lpar import PARTITION_TYPES, LparResources, validate_partition_type
from ...documents.storage import STORAGE_KINDS, StorageKind
from ...operations.logical import engine, store
from ...operations.logical.engine import MAX_WAIT_SECONDS, OperationRequest
from ..output import (
    VerbatimTable,
    confirm_on_stderr,
    console,
    escape_controls,
    fail,
    print_json,
    terminal_field,
    usage_error,
)
from ..runtime import current_options, ssh_config, with_client
from .assignment_input import load_install, load_pcie_assignments, load_physical_io

TOOL = "hmc_provision_lpar"
CONTINUATIONS = ("none", "resume", "abandon", "boot")

# A CLI flag may select one member of a composite operation argument.
_REQUEST_FIELDS = {
    "system": "system_name_or_uuid",
    "name": "name",
    "port_vlan_id": "adapters.port_vlan_id",
    "mac": "adapters.mac",
    "vios_uuid": "storage.vios_uuid",
    "storage_name": "storage.storage_name",
    "storage_kind": "storage.kind",
    "vg_uuid": "storage.vg_uuid",
    "capacity_mib": "storage.capacity_mib",
    "min_memory": "resources.min_memory",
    "memory": "resources.desired_memory",
    "max_memory": "resources.max_memory",
    "vcpus": "resources.desired_vcpus",
    "max_vcpus": "resources.max_vcpus",
    "partition_type": "partition_type",
    "power_on": "power_on",
    "install": "install",
    "boot": "boot",
    "physical_io": "physical_io",
    "pcie_assignments": "assignments",
    "exclusive_writer_window": "exclusive_writer_window",
}
_FILE_LOADERS = {
    "install": load_install,
    "physical_io": load_physical_io,
    "pcie_assignments": load_pcie_assignments,
}


def _continuation_values(
    ctx: typer.Context, recorded: Mapping[str, Any]
) -> dict[str, Any]:
    """Send only explicit request flags to the engine's recorded-value check."""
    given: dict[str, Any] = {}
    for option, field in _REQUEST_FIELDS.items():
        if ctx.get_parameter_source(option) != ParameterSource.COMMANDLINE:
            continue
        value = ctx.params[option]
        if loader := _FILE_LOADERS.get(option):
            value = json_value(loader(Path(value)))
        group, _, member = field.partition(".")
        if member:
            given.setdefault(group, dict(recorded.get(group, {})))[member] = value
        else:
            given[group] = value
    return given


def _validated_request(arguments: Mapping[str, Any]) -> tuple[str, ProvisionRequest]:
    """Keep caller-controlled producer fields and schema errors out of CLI output."""
    try:
        return request_from_arguments(arguments)
    except MediaMismatch:
        raise ValueError(
            "--install producer result does not match the requested media, network or MAC"
        ) from None
    except ValidationError:
        if arguments.get("install") is not None:
            raise ValueError(
                "Invalid --install provision request; check the typed options"
            ) from None
        raise


def lpars_provision(
    ctx: typer.Context,
    request_id: str = typer.Option(
        ...,
        "--request-id",
        help="Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ - (ADR 0313)",
    ),
    continuation: str = typer.Option(
        "none",
        "--continuation",
        help="none, resume, abandon or boot; a continuation reuses the recorded request",
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
    power_on: bool | None = typer.Option(
        None, "--power-on/--no-power-on", help="Power on after provisioning"
    ),
    install: Path | None = typer.Option(
        None,
        "--install",
        help="Prepared or built LparInstall JSON file (2 MiB maximum)",
    ),
    mac: str | None = typer.Option(
        None, "--mac", help="Pinned MAC for prepared install media"
    ),
    boot: str | None = typer.Option(
        None, "--boot", help="Installer boot mode: immediate (default) or deferred"
    ),
    exclusive_writer_window: bool = typer.Option(
        False,
        "--exclusive-writer-window",
        help=(
            "Attest that no other writer changes this partition's profiles until the "
            "operation ends; required to provision, resume or boot (ADR 0313)"
        ),
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
            "JSON file using the declarative LparPcieAssignments schema; a "
            "dedicated slot names profile default_profile and is written into the "
            "saved profile provision activates"
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Provision a new LPAR as one journaled operation and persist it in a saved profile.

    Creates and stamps the partition, adds its network adapter and storage, saves the
    current configuration into a new profile that it reads back and makes the default,
    then optionally powers on from that profile. Needs --exclusive-writer-window.

    Always validates preconditions first (name uniqueness, VLAN existence, volume-group
    existence). --dry-run runs only those checks and records nothing. Otherwise the run is
    recorded under --request-id (ADR 0313): repeating it returns the same operation, and a
    paused run continues with --continuation resume (or stops with abandon) from this
    command or hmc_provision_lpar under the same agent id and connection. The command waits
    for the worker. With --install, coordinate writers to the disk's VolumeGroup for the
    entire operation. Deferred stops with the partition powered off; --continuation boot
    starts it. Boot success means firmware handed control to the boot image. Guest
    installation is unverified. An incomplete or failed run exits 1.
    """
    try:
        store.validate_request_id(request_id)
    except ValueError as exc:
        usage_error(f"--request-id: {exc}")
    if continuation not in CONTINUATIONS:
        usage_error(f"--continuation must be one of {', '.join(CONTINUATIONS)}")
    if boot is not None and boot not in ("immediate", "deferred"):
        usage_error("--boot must be immediate or deferred")
    if not (dry_run or continuation == "abandon" or exclusive_writer_window):
        usage_error(
            "--exclusive-writer-window is required: provision writes a partition "
            "profile and the default-profile setting (ADR 0313)"
        )
    if continuation != "none":
        if dry_run:
            usage_error("--dry-run cannot continue an operation")
        _run(ctx, request_id, continuation, None, as_json)
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
        "adapters": json_value(ProvisionAdapters(port_vlan_id or 0, mac)),
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
        "power_on": install is None if power_on is None else power_on,
        "assignments": json_value(assignments),
        "physical_io": json_value(load_physical_io(physical_io)),
        "exclusive_writer_window": exclusive_writer_window,
    }
    if install is not None:
        arguments["install"] = json_value(load_install(install))
    if boot is not None:
        arguments["boot"] = boot
    try:
        system_name, request = _validated_request(arguments)
    except ValueError as exc:
        usage_error(str(exc))
    try:
        new = dry_run or (
            engine.recorded_arguments(
                ssh_config().agent_id or "hmcpctl", request_id, TOOL
            )
            is None
        )
    except ValueError as exc:
        fail(exc)
    if new:
        # Preconditions refuse a new operation before anything is recorded.
        result = with_client(lambda hmc: preview_provision(hmc, system_name, request))
        if dry_run:
            _render_provision_result(result, request.name, True, as_json)
            return
    if not yes:
        confirm_on_stderr(
            f"Provision LPAR '{terminal_field(name, err=True)}' on system "
            f"'{terminal_field(system, err=True)}' (VLAN {terminal_field(port_vlan_id, err=True)}, "
            f"VIOS {terminal_field(vios_uuid, err=True)})?",
            abort=True,
        )
    _run(ctx, request_id, continuation, arguments, as_json)


def _run(
    ctx: typer.Context,
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
            submitted = _continuation_values(ctx, arguments)
        else:
            submitted = arguments
        system, request = _validated_request(arguments)
        # Workers reuse this admitted config instead of resolving the profile again.
        record = engine.submit(
            OperationRequest(
                TOOL,
                agent_id,
                store.connection_label(current_options().profile, tool=TOOL),
                config.host,
                request_id,
                submitted,
            ),
            provision_body(
                system,
                request,
                lambda _connection, _host: HMCClient(config),
                wait_seconds=MAX_WAIT_SECONDS,
            ),
            classifiers=provision_classifiers(
                request, lambda _connection, _host: HMCClient(config)
            ),
            continuation=continuation,
            wait_seconds=MAX_WAIT_SECONDS,
        )
        # The worker dies with this process, so wait for it rather than return early.
        record = engine.wait_for_record(record.operation_id, None)
    except ValueError as exc:
        fail(exc)
    result = result_from_record(record)
    _render_provision_result(result, request.name, False, as_json)
    ended = record.outcome in ("configured", "ready_to_boot", "boot_started") or (
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
    elif result.operation and result.operation.outcome == "ready_to_boot":
        console.print(
            "Installer media staged. The partition is powered off and ready to boot."
        )
    elif result.operation and result.operation.outcome == "boot_started":
        console.print(
            "Partition firmware handed control to the boot image. Guest installation is unverified."
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
