"""CLI command for the LPAR decommission workflow."""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import typer

from hmcpctl.operations.lpar.decommission.core import DecommissionResult
from hmcpctl.operations.lpar.decommission.operation import (
    STORAGE_CLEANUP,
    TOOL_NAME,
    decommission_body,
    decommission_classifiers,
    deletes_owned,
    owned_vios,
    preview_target,
    result_from_record,
)

from ...operations.logical import engine, store
from ...operations.logical.engine import MAX_WAIT_SECONDS, OperationRequest
from ...operations.logical.models import OperationRecord
from ...resource_identity import is_uuid
from .. import runtime
from ..output import (
    InventoryFormat,
    VerbatimTable,
    confirm_on_stderr,
    console,
    err_console,
    fail,
    print_inventory,
    resolve_inventory_format,
    terminal_field,
    usage_error,
)
from ..runtime import current_options, ssh_config, with_client

CONTINUATIONS = ("none", "resume", "abandon")


def lpars_decommission(
    name_or_uuid: str | None = typer.Argument(
        None, help="Partition name or UUID; required for a new operation"
    ),
    request_id: str = typer.Option(
        ...,
        "--request-id",
        help="Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ - (ADR 0320)",
    ),
    continuation: str = typer.Option(
        "none",
        "--continuation",
        help=("none, resume or abandon; a continuation reuses the recorded request"),
    ),
    system: str | None = typer.Option(
        None,
        "--system",
        "-s",
        help="Managed system name or UUID; required for a new operation",
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
    storage_cleanup: str | None = typer.Option(
        None,
        "--storage-cleanup",
        help=(
            "retain (default) or delete_owned: also delete the partition's recorded, "
            "unshared virtual disks and installer media (ADR 0323)"
        ),
    ),
    exclusive_writer_window: bool = typer.Option(
        False,
        "--exclusive-writer-window",
        help="Assert no other writer changes these VIOS volume groups; delete_owned "
        "needs it",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON envelope"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Inventory and optionally decommission one LPAR; retain backing storage by default.

    With --storage-cleanup delete_owned and --exclusive-writer-window, the partition's
    recorded, unshared virtual disks and installer media are deleted after it.
    Unproven mappings need inspection, not deletion. Use --dry-run first; it records
    nothing. Otherwise the run is recorded under --request-id (ADR 0320): repeating it
    returns the same operation, and a paused run continues with --continuation resume
    (or stops with abandon) from this command or hmc_decommission_lpar under the same
    agent id and connection. The command waits for the run to end and exits 1 unless
    it completed.
    """
    try:
        store.validate_request_id(request_id)
    except ValueError as exc:
        usage_error(f"--request-id: {exc}")
    if continuation not in CONTINUATIONS:
        usage_error(f"--continuation must be one of {', '.join(CONTINUATIONS)}")
    if storage_cleanup is not None and storage_cleanup not in STORAGE_CLEANUP:
        usage_error(f"--storage-cleanup must be one of {', '.join(STORAGE_CLEANUP)}")
    selected = resolve_inventory_format(as_json, format)
    if continuation != "none":
        if dry_run:
            usage_error("--dry-run cannot continue an operation")
        if name_or_uuid is not None or system is not None:
            usage_error(
                "a continuation takes the recorded partition and system; omit the "
                "partition argument and --system"
            )
        if (
            ownership_override,
            immediate,
            timeout_seconds,
            poll_interval,
            storage_cleanup,
        ) != (False, False, 300, 5, None):
            usage_error(
                "a continuation reuses the recorded options; omit --ownership-override, "
                "--immediate, --timeout-seconds, --poll-interval and --storage-cleanup"
            )
        try:
            recorded = engine.recorded_arguments(
                ssh_config().agent_id or "hmcpctl", request_id, TOOL_NAME
            )
        except ValueError as exc:
            fail(exc)
        if recorded is None:
            usage_error(f"no operation has request_id {request_id}")
        if continuation == "resume":
            _require_window(recorded, exclusive_writer_window)
        if continuation == "resume" and not yes:
            confirm_on_stderr(
                f"Resume decommission request '{terminal_field(request_id, err=True)}' "
                f"of LPAR '{terminal_field(recorded['lpar_name_or_uuid'], err=True)}' "
                f"on system '{terminal_field(recorded['system_name_or_uuid'], err=True)}'"
                "? It continues powering off, detaching and deleting that partition"
                + (
                    ", then deletes its recorded virtual disks and installer media that "
                    "no other partition maps."
                    if deletes_owned(recorded)
                    else "."
                ),
                abort=True,
            )
        _run(request_id, continuation, recorded, selected)
        return
    if name_or_uuid is None or system is None:
        usage_error("a new operation needs the partition argument and --system")
    arguments = {
        "system_name_or_uuid": system,
        "lpar_name_or_uuid": name_or_uuid,
        "ownership_override": ownership_override,
        "immediate": immediate,
        "timeout_seconds": timeout_seconds,
        "poll_interval": poll_interval,
        "storage_cleanup": storage_cleanup or "retain",
    }
    if not dry_run:
        _require_window(arguments, exclusive_writer_window)
    if not (dry_run or yes):
        confirm_on_stderr(
            (
                f"Decommission LPAR '{terminal_field(name_or_uuid, err=True)}' on system '"
                f"{terminal_field(system, err=True)}'? This powers it off, detaches its vSCSI mappings "
                f"before client adapters and partition deletion"
                + (
                    ", then deletes its recorded virtual disks and installer media that "
                    "no other partition maps."
                    if deletes_owned(arguments)
                    else ", and retains backing storage."
                )
            ),
            abort=True,
        )
    try:
        new = dry_run or (
            engine.recorded_arguments(
                ssh_config().agent_id or "hmcpctl", request_id, TOOL_NAME
            )
            is None
        )
    except ValueError as exc:
        fail(exc)
    if new:
        # Preconditions refuse a new operation before anything is recorded.
        result, target = with_client(lambda hmc: preview_target(hmc, arguments))
        if dry_run:
            if deletes_owned(arguments) and not exclusive_writer_window:
                warnings = (*result.warnings, _WINDOW_WARNING)
                result = dataclasses.replace(result, warnings=warnings)
            _render_decommission_result(result, name_or_uuid, selected, system)
            return
        _run(request_id, continuation, arguments, selected, target)
        return
    _run(request_id, continuation, arguments, selected)


_WINDOW_WARNING = (
    "--storage-cleanup delete_owned needs --exclusive-writer-window to run: you assert "
    "no other writer changes these VIOS volume groups until the operation ends"
)


def _require_window(arguments: Mapping[str, Any], window: bool) -> None:
    if deletes_owned(arguments) and not window:
        usage_error(
            "--storage-cleanup delete_owned needs --exclusive-writer-window: assert no "
            "other writer changes these VIOS volume groups until the operation ends "
            "(ADR 0323); a resume asserts it again"
        )


def _run(
    request_id: str,
    continuation: str,
    arguments: Mapping[str, Any],
    format: InventoryFormat,
    target: tuple[str, str] | None = None,
) -> None:
    """Submit, wait for the worker to end, render, and exit 1 unless it completed."""
    try:
        record = _submit(request_id, continuation, arguments, target)
    except ValueError as exc:
        fail(exc)
    _render_decommission_result(
        result_from_record(record, delete_owned=deletes_owned(arguments)),
        arguments["lpar_name_or_uuid"],
        format,
        arguments["system_name_or_uuid"],
    )
    ended = record.outcome == "completed" or (
        continuation == "abandon" and record.outcome == "abandoned"
    )
    if not ended:
        raise typer.Exit(1)


def _submit(
    request_id: str,
    continuation: str,
    arguments: Mapping[str, Any],
    target: tuple[str, str] | None,
) -> OperationRecord:
    """Run the operation in this process and return its record once the worker ends."""
    config = ssh_config()
    agent_id = config.agent_id or "hmcpctl"
    vioses: frozenset[str] = frozenset()
    if deletes_owned(arguments) and continuation != "abandon":
        # The CLI holds no access policy, so every owning VIOS is authorized.
        recorded = engine.recorded_partition(agent_id, request_id, TOOL_NAME)
        vioses = owned_vios(
            target
            or recorded
            or with_client(lambda hmc: preview_target(hmc, arguments))[1]
        )
    # Workers reuse this admitted config instead of resolving the profile again.
    record = engine.submit(
        OperationRequest(
            TOOL_NAME,
            agent_id,
            store.connection_label(current_options().profile, tool=TOOL_NAME),
            config.host,
            request_id,
            arguments if continuation == "none" else {},
        ),
        decommission_body(
            arguments,
            lambda _connection, _host: runtime.HMCClient(config),
            authorized_vios=vioses,
        ),
        classifiers=decommission_classifiers(
            lambda _connection, _host: runtime.HMCClient(config)
        ),
        continuation=continuation,
        wait_seconds=MAX_WAIT_SECONDS,
    )
    # The worker dies with this process, so wait for it rather than return early.
    return engine.wait_for_record(record.operation_id, None)


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
    if result.operation is not None:
        operation = result.operation
        status = f"{operation.state}/{operation.outcome or 'running'}"
        err_console.print(
            f"Operation {terminal_field(operation.operation_id, err=True)}: "
            f"{terminal_field(status, err=True)} "
            f"(phase {terminal_field(operation.phase, err=True)})",
            markup=False,
        )
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
    if result.storage is None:
        rows.append(("storage", "backing", "disposition", "retained"))
    else:
        for disposition in ("deleted", "retained", "pending"):
            for index, item in enumerate(result.storage[disposition]):
                rows.extend(
                    (f"storage.{disposition}", index, key, value)
                    for key, value in item.items()
                )
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
