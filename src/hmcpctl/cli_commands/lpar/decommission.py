"""CLI command for the LPAR decommission workflow."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import typer

from ...operations.logical import engine, store
from ...operations.logical.engine import MAX_WAIT_SECONDS, OperationRequest
from ...operations.logical.models import OperationRecord
from ...operations.lpar.decommission import DecommissionResult
from ...operations.lpar.decommission_operation import (
    TOOL_NAME,
    decommission_body,
    decommission_classifiers,
    preview,
    result_from_record,
)
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
        help=(
            "none, resume or abandon; a continuation reuses the recorded request and "
            "ignores the option flags"
        ),
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
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON envelope"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Inventory and optionally decommission one LPAR; retain backing storage.

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
    selected = resolve_inventory_format(as_json, format)
    if continuation != "none":
        if dry_run:
            usage_error("--dry-run cannot continue an operation")
        if name_or_uuid is not None or system is not None:
            usage_error(
                "a continuation takes the recorded partition and system; omit the "
                "partition argument and --system"
            )
        if continuation == "resume" and not yes:
            confirm_on_stderr(
                f"Resume decommission request '{terminal_field(request_id, err=True)}'? "
                "It continues powering off, detaching and deleting the recorded partition.",
                abort=True,
            )
        _run(request_id, continuation, None, selected)
        return
    if name_or_uuid is None or system is None:
        usage_error("a new operation needs the partition argument and --system")
    if not (dry_run or yes):
        confirm_on_stderr(
            (
                f"Decommission LPAR '{terminal_field(name_or_uuid, err=True)}' on system '"
                f"{terminal_field(system, err=True)}'? This powers it off, detaches its vSCSI mappings "
                f"before client adapters and partition deletion, and retains backing storage."
            ),
            abort=True,
        )
    arguments = {
        "system_name_or_uuid": system,
        "lpar_name_or_uuid": name_or_uuid,
        "ownership_override": ownership_override,
        "immediate": immediate,
        "timeout_seconds": timeout_seconds,
        "poll_interval": poll_interval,
    }
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
        result = with_client(lambda hmc: preview(hmc, arguments))
        if dry_run:
            _render_decommission_result(result, name_or_uuid, selected, system)
            return
    _run(request_id, continuation, arguments, selected)


def _run(
    request_id: str,
    continuation: str,
    arguments: Mapping[str, Any] | None,
    format: InventoryFormat,
) -> None:
    """Submit, wait for the worker to end, render, and exit 1 unless it completed."""
    try:
        if arguments is None:
            arguments = engine.recorded_arguments(
                ssh_config().agent_id or "hmcpctl", request_id, TOOL_NAME
            )
            if arguments is None:
                usage_error(f"no operation has request_id {request_id}")
        record = _submit(request_id, continuation, arguments)
    except ValueError as exc:
        fail(exc)
    _render_decommission_result(
        result_from_record(record),
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
    request_id: str, continuation: str, arguments: Mapping[str, Any]
) -> OperationRecord:
    """Run the operation in this process and return its record once the worker ends."""
    config = ssh_config()
    record = engine.submit(
        OperationRequest(
            TOOL_NAME,
            config.agent_id or "hmcpctl",
            store.connection_label(current_options().profile, tool=TOOL_NAME),
            config.host,
            request_id,
            arguments if continuation == "none" else {},
        ),
        decommission_body(arguments, lambda _connection: runtime.HMCClient(config)),
        classifiers=decommission_classifiers(
            lambda _connection: runtime.HMCClient(config)
        ),
        continuation=continuation,
        wait_seconds=MAX_WAIT_SECONDS,
    )
    # The worker dies with this process, so wait for it rather than return early.
    engine.join(record.operation_id, None)
    with store.session() as conn:
        return store.read_record(conn, record.operation_id)


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
    rows.append(("storage", "backing", "disposition", "retained"))
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
