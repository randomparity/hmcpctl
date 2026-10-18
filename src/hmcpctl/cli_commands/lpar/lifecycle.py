"""CLI commands for LPAR power and deletion lifecycle operations."""

from __future__ import annotations

import typer
from rich.text import Text

from ...jobs import (
    BootMode,
    PowerOffOperation,
    PowerOnKeylock,
    PowerOnOperationType,
    validate_wait_timing,
)
from ...operations.lpar.core import (
    LparPowerResult,
    delete_lpar,
    power_off_lpar,
    submit_power_on_lpar,
)
from ..output import console, err_console, print_json, terminal_field
from ..runtime import with_client


def lpars_power_on(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Submit whatever the partition state"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    system: str | None = typer.Option(
        None,
        "--system",
        "-s",
        help="Managed system name or UUID; with HMC_AUTHORIZE_POWER_OPERATIONS it also spares the ownership guard a fleet-wide search",
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval; no effect unless HMC_AUTHORIZE_POWER_OPERATIONS is set",
    ),
    boot_mode: BootMode = typer.Option(
        "norm", "--boot-mode", help="Boot mode to activate into"
    ),
    partition_profile: str | None = typer.Option(
        None,
        "--partition-profile",
        help=(
            "UUID of the partition profile to activate against; not the connection "
            "profile. Activating it discards current-configuration changes it lacks "
            "(memory, processor and adapter changes), unless it is the partition's "
            "current profile and CurrentProfileSync was On"
        ),
    ),
    operation_type: PowerOnOperationType | None = typer.Option(
        None,
        "--operation-type",
        help="PowerOn operation type; activate is the default and is never sent",
    ),
    keylock: PowerOnKeylock | None = typer.Option(
        None,
        "--keylock",
        help="Keylock position to activate with (norm is normal); omitted, the HMC decides",
    ),
) -> None:
    """Power on an LPAR (submits a PowerOn job)."""
    validate_wait_timing(wait, timeout, interval)
    _confirm_power(name_or_uuid, "PowerOn", yes)
    result = with_client(
        lambda hmc: submit_power_on_lpar(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            force=force,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
            boot_mode=boot_mode,
            partition_profile_uuid=partition_profile,
            operation_type=operation_type,
            keylock=keylock,
        )
    )
    _render_power(result)


def lpars_power_off(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    immediate: bool = typer.Option(
        False, "--immediate", help="Immediate power off instead of a delayed shutdown"
    ),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    system: str | None = typer.Option(
        None,
        "--system",
        "-s",
        help="Managed system name or UUID; with HMC_AUTHORIZE_POWER_OPERATIONS it also spares the ownership guard a fleet-wide search",
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval; no effect unless HMC_AUTHORIZE_POWER_OPERATIONS is set",
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Restart the partition instead of leaving it off; with --operation shutdown it needs --immediate",
    ),
    operation: PowerOffOperation = typer.Option(
        "shutdown",
        "--operation",
        help="PowerOff shutdown operation; osshutdown needs an active RMC connection",
    ),
    allow_dump_restart: bool = typer.Option(
        False,
        "--allow-dump-restart",
        help="Confirm --operation dumprestart, which crashes the partition and dumps",
    ),
) -> None:
    """Power off an LPAR (submits a PowerOff job)."""
    validate_wait_timing(wait, timeout, interval)
    op = "Immediate PowerOff" if immediate else "PowerOff"
    if restart:
        op = f"{op} with restart"
    if operation != "shutdown":
        op = f"{op} (operation={operation})"
    _confirm_power(name_or_uuid, op, yes)
    result = with_client(
        lambda hmc: power_off_lpar(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            immediate=immediate,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
            restart=restart,
            operation=operation,
            allow_dump_restart=allow_dump_restart,
        )
    )
    _render_power(result)


def _confirm_power(name_or_uuid: str, operation: str, yes: bool) -> None:
    if not yes and not typer.confirm(
        f"Really submit {terminal_field(operation)} for partition '{terminal_field(name_or_uuid)}'?"
    ):
        err_console.print("Aborted.")
        raise typer.Abort()


def _render_power(result: LparPowerResult) -> None:
    uuid, job = result.lpar_uuid, result.job
    if job and job.get("already_running"):
        console.print(terminal_field(job["message"]), style="yellow", markup=False)
        print_json(job)
        return
    console.print(
        Text.assemble(("Job submitted", "green"), f" for {terminal_field(uuid)}")
    )
    print_json(job)
    for warning in result.warnings:
        console.print(
            f"Warning: {terminal_field(warning)}", style="yellow", markup=False
        )


def lpars_delete(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    system: str = typer.Option(
        ...,
        "--system",
        "-s",
        help="Managed system name or UUID; required for scoped ownership and cleanup checks",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval",
    ),
) -> None:
    """Delete (destroy) an LPAR. It must be powered off first."""
    if not yes and not typer.confirm(
        f"Permanently DELETE partition '{terminal_field(name_or_uuid)}'? This cannot be undone."
    ):
        raise typer.Abort()

    uuid = with_client(
        lambda hmc: delete_lpar(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            ownership_override=ownership_override,
        )
    )
    console.print(f"Deleted LPAR {terminal_field(uuid)}", style="green", markup=False)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("power-on")(lpars_power_on)
    group.command("power-off")(lpars_power_off)
    group.command("delete")(lpars_delete)
