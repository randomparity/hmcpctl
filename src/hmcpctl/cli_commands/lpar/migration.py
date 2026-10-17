"""LPAR migration and remote-restart CLI commands."""

from __future__ import annotations

from dataclasses import asdict
from typing import cast

import typer

from ...jobs import (
    REMOTE_RESTART_OPERATIONS,
    JobOutcome,
    RemoteRestartOperation,
    validate_wait_timing,
)
from ...operations.lpar.migration import (
    LpmAffinityMigrationResult,
    LpmAffinityPreflightRequest,
    LpmCapability,
    LpmDestinationCheckBasis,
    LpmMigrationRequest,
    LpmResponse,
    RemoteRestartRequest,
    abort_lpar_migration,
    migrate_lpar,
    migrate_lpar_with_affinity_preflight,
    recover_lpar_migration,
    remote_restart_lpar,
    validate_lpar_migration,
)
from ..output import console, print_json, terminal_field
from ..runtime import with_client


def _lpm_run(name_or_uuid: str, fn, action: str, target: str | None, yes: bool) -> None:
    """Confirm and present the result of a shared LPM operation."""

    if not yes:
        dest = f" to '{target}'" if target else ""
        if not typer.confirm(
            f"Really {terminal_field(action)} partition '{terminal_field(name_or_uuid)}'"
            f"{terminal_field(dest)}?"
        ):
            raise typer.Abort()

    result = with_client(fn)
    if isinstance(result, LpmAffinityMigrationResult):
        status = "Submitted" if result.job is not None else "Stopped"
        console.print(
            f"{terminal_field(status)} {terminal_field(action)}",
            style="green",
            markup=False,
        )
        print_json(asdict(result))
        return
    console.print(
        f"Submitted {terminal_field(action)} for {terminal_field(result.lpar_uuid)}",
        style="green",
        markup=False,
    )
    job = asdict(result.job) if isinstance(result.job, JobOutcome) else result.job
    print_json(job)


def lpars_migrate(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    target: str = typer.Option(..., "--target", help="Target managed system name"),
    profile: str | None = typer.Option(None, "--profile", help="Target profile name"),
    wait_time: int | None = typer.Option(
        None, "--wait-time", help="Override operation wait time"
    ),
    validate_first: bool = typer.Option(
        True,
        "--validate-first/--no-validate-first",
        help="Require successful terminal validation before migration",
    ),
    wait: bool = typer.Option(False, "--wait/--no-wait", help="Wait for migration"),
    timeout: int = typer.Option(300, "--timeout", help="Polling timeout seconds"),
    interval: int = typer.Option(5, "--interval", help="Polling interval seconds"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Source managed system name or UUID"
    ),
) -> None:
    """Live-migrate (LPM) an LPAR to another managed system."""

    validate_wait_timing(wait or validate_first, timeout, interval)

    async def _fn(hmc):
        return await migrate_lpar(
            hmc,
            system,
            name_or_uuid,
            LpmMigrationRequest(target, profile, wait_time),
            validate_first=validate_first,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
        )

    _lpm_run(name_or_uuid, _fn, "Migrate", target, yes)


def lpars_migrate_affinity(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    target: str = typer.Option(..., "--target", help="Target managed system name"),
    source_score: int | None = typer.Option(None, "--source-score"),
    destination_estimate: int | None = typer.Option(None, "--destination-estimate"),
    check_basis: LpmDestinationCheckBasis = typer.Option("calculated", "--check-basis"),
    configured_minimum: int | None = typer.Option(None, "--configured-minimum"),
    capability: LpmCapability = typer.Option("available", "--capability"),
    response: LpmResponse = typer.Option("warn", "--response"),
    preflight_timeout: float = typer.Option(
        5.0, "--preflight-timeout", help="Affinity preflight timeout seconds"
    ),
    wait: bool = typer.Option(False, "--wait/--no-wait", help="Wait for migration"),
    timeout: int = typer.Option(300, "--timeout", help="Polling timeout seconds"),
    interval: int = typer.Option(5, "--interval", help="Polling interval seconds"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Source managed system name or UUID"
    ),
) -> None:
    """Run explicit affinity preflight before validation-first LPM."""
    validate_wait_timing(True, timeout, interval)
    request = LpmAffinityPreflightRequest(
        source_current_score=source_score,
        destination_estimated_score=destination_estimate,
        destination_check_basis=check_basis,
        configured_minimum=configured_minimum,
        capability=capability,
        capability_limits=("Destination affinity is estimated, not guaranteed.",),
        response=response,
        preflight_timeout_seconds=preflight_timeout,
    )

    async def _fn(hmc):
        return await migrate_lpar_with_affinity_preflight(
            hmc,
            system,
            name_or_uuid,
            LpmMigrationRequest(target),
            request,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
        )

    _lpm_run(name_or_uuid, _fn, "affinity-aware Migrate", target, yes)


def lpars_migrate_validate(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    target: str = typer.Option(..., "--target", help="Target managed system name"),
    profile: str | None = typer.Option(None, "--profile", help="Target profile name"),
    wait_time: int | None = typer.Option(None, "--wait-time"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Source managed system name or UUID"
    ),
) -> None:
    """Validate whether an LPM migration would succeed."""

    async def _fn(hmc):
        return await validate_lpar_migration(
            hmc, system, name_or_uuid, LpmMigrationRequest(target, profile, wait_time)
        )

    _lpm_run(name_or_uuid, _fn, "MigrateValidate", target, yes)


def lpars_migrate_abort(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Source managed system name or UUID"
    ),
) -> None:
    """Abort an in-progress LPM migration."""
    validate_wait_timing(wait, timeout, interval)

    async def _fn(hmc):
        return await abort_lpar_migration(
            hmc,
            system,
            name_or_uuid,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
        )

    _lpm_run(name_or_uuid, _fn, "MigrateAbort", None, yes)


def lpars_migrate_recover(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Source managed system name or UUID"
    ),
) -> None:
    """Recover an LPAR after a failed LPM migration."""
    validate_wait_timing(wait, timeout, interval)

    async def _fn(hmc):
        return await recover_lpar_migration(
            hmc,
            system,
            name_or_uuid,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
            ownership_override=ownership_override,
        )

    _lpm_run(name_or_uuid, _fn, "MigrateRecover", None, yes)


def lpars_remote_restart(
    name_or_uuid: str = typer.Argument(..., help="Partition name or UUID"),
    operation: str = typer.Option(..., "--operation", help="RemoteRestart operation"),
    system: str = typer.Option(
        ..., "--system", help="Source managed system name or UUID"
    ),
    target: str | None = typer.Option(
        None, "--target", help="Target managed system name or UUID"
    ),
    use_current_data: bool = typer.Option(False, "--use-current-data"),
    retain_devices: bool = typer.Option(False, "--retain-devices"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Remote-restart a failed LPAR on another managed system."""
    validate_wait_timing(wait, timeout, interval)
    if operation not in REMOTE_RESTART_OPERATIONS:
        raise typer.BadParameter(
            f"operation must be one of: "
            f"{terminal_field(', '.join(sorted(REMOTE_RESTART_OPERATIONS)), err=True)}"
        )

    async def _fn(hmc):
        return await remote_restart_lpar(
            hmc,
            system,
            name_or_uuid,
            RemoteRestartRequest(
                operation=cast(RemoteRestartOperation, operation),
                target_system_name_or_uuid=target,
                use_current_data=use_current_data,
                retain_devices=retain_devices,
                ownership_override=ownership_override,
            ),
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
        )

    _lpm_run(name_or_uuid, _fn, f"RemoteRestart {operation}", target, yes)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("migrate")(lpars_migrate)
    group.command("migrate-affinity")(lpars_migrate_affinity)
    group.command("migrate-validate")(lpars_migrate_validate)
    group.command("migrate-abort")(lpars_migrate_abort)
    group.command("migrate-recover")(lpars_migrate_recover)
    group.command("remote-restart")(lpars_remote_restart)
