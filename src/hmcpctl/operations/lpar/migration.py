"""Presentation-neutral Live Partition Mobility operations."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation

from ...errors import HMCError
from ...jobs import (
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    SUCCESSFUL_JOB_STATUSES,
    JobOutcome,
    RemoteRestartOperation,
    job_identifier,
    job_outcome,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_name

LpmDestinationCheckBasis = Literal["calculated", "migration-check"]
LpmCapability = Literal["available", "unavailable"]
LpmResponse = Literal["warn", "fail"]
LpmPreflightStatus = Literal["passed", "warned", "failed", "unavailable"]

_MAX_CAPABILITY_LIMITS = 8
_MAX_CAPABILITY_LIMIT_LENGTH = 200


@dataclass(frozen=True)
class LpmResult:
    """An LPM submission paired with its resolved partition identity."""

    lpar_uuid: str
    job: JobOutcome


@dataclass(frozen=True)
class LpmMigrationRequest:
    """Destination-specific inputs shared by LPM validation and migration."""

    target_system_name_or_uuid: str
    target_profile_name: str | None = None
    wait_time: int | None = None


@dataclass(frozen=True)
class RemoteRestartRequest:
    """Restart-specific controls for a remote-restart workflow."""

    operation: RemoteRestartOperation
    target_system_name_or_uuid: str | None = None
    use_current_data: bool = False
    retain_devices: bool = False
    ownership_override: bool = False


@dataclass(frozen=True)
class LpmAffinityPreflightRequest:
    """Explicit affinity evidence and caller-owned migration response."""

    source_current_score: int | None = field(
        metadata={"description": "Current affinity score on the source system."}
    )
    destination_estimated_score: int | None = field(
        metadata={"description": "Estimated affinity score on the destination."}
    )
    destination_check_basis: LpmDestinationCheckBasis = field(
        metadata={"description": "Basis used for the destination estimate."}
    )
    configured_minimum: int | None = field(
        metadata={"description": "Configured minimum acceptable affinity score."}
    )
    capability: LpmCapability = field(
        metadata={"description": "Whether the platform supports the affinity check."}
    )
    capability_limits: tuple[str, ...] = field(
        metadata={"description": "Bounded limitations on the affinity evidence."}
    )
    response: LpmResponse = field(
        metadata={
            "description": "Explicit response to adverse or unavailable evidence."
        }
    )
    preflight_timeout_seconds: float = field(
        default=5.0,
        metadata={"description": "Maximum seconds allowed for affinity preflight."},
    )


@dataclass(frozen=True)
class LpmAffinityPreflightOutcome:
    """Stable evidence-bearing decision made before HMC LPM validation."""

    status: LpmPreflightStatus
    reason: str
    proceed: bool
    source_current_score: int | None
    destination_estimated_score: int | None
    destination_check_basis: LpmDestinationCheckBasis
    configured_minimum: int | None
    capability: LpmCapability
    capability_limits: tuple[str, ...]
    preflight_timeout_seconds: float


@dataclass(frozen=True)
class LpmAffinityMigrationResult:
    """Affinity preflight paired with an optional submitted migration job."""

    lpar_uuid: str | None
    preflight: LpmAffinityPreflightOutcome
    job: JobOutcome | None


def _validate_affinity_score(value: int | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be an integer from 0 through 100 or null")


def _preflight_outcome(
    request: LpmAffinityPreflightRequest,
    status: LpmPreflightStatus,
    reason: str,
    proceed: bool,
) -> LpmAffinityPreflightOutcome:
    return LpmAffinityPreflightOutcome(
        status=status,
        reason=reason,
        proceed=proceed,
        source_current_score=request.source_current_score,
        destination_estimated_score=request.destination_estimated_score,
        destination_check_basis=request.destination_check_basis,
        configured_minimum=request.configured_minimum,
        capability=request.capability,
        capability_limits=request.capability_limits,
        preflight_timeout_seconds=request.preflight_timeout_seconds,
    )


def _response_policy_outcome(
    request: LpmAffinityPreflightRequest,
    reason: str,
    nonblocking_status: LpmPreflightStatus,
) -> LpmAffinityPreflightOutcome:
    """Apply the caller's warn-or-fail policy to unavailable preflight evidence."""
    if request.response == "fail":
        return _preflight_outcome(request, "failed", reason, False)
    return _preflight_outcome(request, nonblocking_status, reason, True)


def evaluate_lpm_affinity_preflight(
    request: LpmAffinityPreflightRequest,
) -> LpmAffinityPreflightOutcome:
    """Evaluate explicit affinity evidence without HMC traffic."""
    if request.response not in {"warn", "fail"}:
        raise ValueError("affinity preflight response must be warn or fail")
    malformed: list[str] = []
    for name in (
        "source_current_score",
        "destination_estimated_score",
        "configured_minimum",
    ):
        try:
            _validate_affinity_score(getattr(request, name), name)
        except ValueError:
            malformed.append(name)
    if request.destination_check_basis not in {"calculated", "migration-check"}:
        malformed.append("destination_check_basis")
    if request.capability not in {"available", "unavailable"}:
        malformed.append("capability")
    if (
        not request.capability_limits
        or len(request.capability_limits) > _MAX_CAPABILITY_LIMITS
        or any(
            not isinstance(limit, str)
            or not limit.strip()
            or len(limit) > _MAX_CAPABILITY_LIMIT_LENGTH
            for limit in request.capability_limits
        )
    ):
        raise ValueError(
            "capability_limits must contain 1 through 8 non-empty descriptions "
            "of at most 200 characters each"
        )

    if malformed:
        reason = f"Affinity preflight input is malformed: {', '.join(malformed)}."
        return _response_policy_outcome(request, reason, "unavailable")

    destination_score = request.destination_estimated_score
    configured_minimum = request.configured_minimum
    if (
        request.capability == "unavailable"
        or request.source_current_score is None
        or destination_score is None
        or configured_minimum is None
    ):
        reason = "Affinity preflight evidence or platform capability is unavailable."
        return _response_policy_outcome(request, reason, "unavailable")

    if destination_score < configured_minimum:
        reason = (
            f"Destination estimate {destination_score} is below "
            f"configured minimum {configured_minimum}."
        )
        return _response_policy_outcome(request, reason, "warned")
    return _preflight_outcome(
        request,
        "passed",
        "Destination estimate meets the configured minimum.",
        True,
    )


async def run_lpm_affinity_preflight(
    request: LpmAffinityPreflightRequest,
) -> LpmAffinityPreflightOutcome:
    """Evaluate preflight within the caller's explicit time bound.

    Invalid request controls raise ``ValueError``. Threshold failures and timeouts
    are returned as outcomes whose ``proceed`` value reflects the requested policy.
    """
    if request.response not in {"warn", "fail"}:
        raise ValueError("affinity preflight response must be warn or fail")
    timeout = request.preflight_timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("preflight_timeout_seconds must be non-negative")

    try:
        evaluation = asyncio.to_thread(evaluate_lpm_affinity_preflight, request)
        return await asyncio.wait_for(evaluation, timeout=timeout)
    except TimeoutError:
        reason = f"Affinity preflight timed out after {timeout} seconds."
        return _response_policy_outcome(request, reason, "unavailable")


async def migrate_lpar_with_affinity_preflight(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    migration: LpmMigrationRequest,
    affinity_preflight: LpmAffinityPreflightRequest,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
) -> LpmAffinityMigrationResult:
    """Run affinity preflight before canonical validation-first migration.

    A rejected preflight is returned without a migration result. Selector,
    authorization, submission, and validation failures raise before migration;
    waited migration timeouts and job failures remain in the returned job outcome.
    """
    preflight = await run_lpm_affinity_preflight(affinity_preflight)
    if not preflight.proceed:
        return LpmAffinityMigrationResult(None, preflight, None)
    result = await migrate_lpar(
        hmc,
        lpar_name_or_uuid,
        migration,
        system_name_or_uuid=system_name_or_uuid,
        wait=wait,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        validate_first=True,
        ownership_override=ownership_override,
    )
    return LpmAffinityMigrationResult(result.lpar_uuid, preflight, result.job)


async def _finish_job(
    hmc: HMCClient,
    job: dict[str, Any] | None,
    wait: bool,
    timeout_seconds: int,
    poll_interval: int,
) -> JobOutcome:
    """Normalize an immediate submission or its final waited state."""
    submitted_id = job_identifier(job) if job is not None else None
    if not wait:
        return replace(job_outcome(submitted_id or "", job), timed_out=False)
    completed_job = await wait_for_submitted_job(
        hmc, job, True, timeout_seconds, poll_interval
    )
    return job_outcome(submitted_id or "", completed_job)


async def _submit_migration_job(
    hmc: HMCClient,
    lpar_uuid: str,
    target_system: str,
    target_profile_name: str | None,
    wait_time: int | None,
    *,
    validate: bool,
) -> dict[str, Any] | None:
    submit = hmc.lpar_migrate_validate if validate else hmc.lpar_migrate
    return await submit(
        lpar_uuid,
        target_system,
        target_profile_name=target_profile_name,
        wait_time=wait_time,
    )


async def validate_lpar_migration(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    migration: LpmMigrationRequest,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
) -> LpmResult:
    """Resolve selectors and submit standalone LPM validation.

    Invalid controls, unresolved selectors, and submission failures raise. When
    waiting, timeout and terminal job failure are represented by ``result.job``.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    target_system = await resolve_system_name(hmc, migration.target_system_name_or_uuid)
    job = await _submit_migration_job(
        hmc,
        lpar_uuid,
        target_system,
        migration.target_profile_name,
        migration.wait_time,
        validate=True,
    )
    return LpmResult(
        lpar_uuid,
        await _finish_job(hmc, job, wait, timeout_seconds, poll_interval),
    )


async def migrate_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    migration: LpmMigrationRequest,
    *,
    system_name_or_uuid: str | None = None,
    validate_first: bool = True,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
) -> LpmResult:
    """Resolve selectors and submit a migration, optionally validating first.

    Invalid controls, resolution, authorization, and submission failures raise.
    With ``validate_first``, an unsuccessful validation raises before migration is
    submitted. Waited migration timeout or job failure remains in ``result.job``.
    """
    effective_wait = wait or validate_first
    validate_wait_timing(effective_wait, timeout_seconds, poll_interval)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    target_system = await resolve_system_name(hmc, migration.target_system_name_or_uuid)
    if validate_first:
        validation_job = await _submit_migration_job(
            hmc,
            lpar_uuid,
            target_system,
            migration.target_profile_name,
            migration.wait_time,
            validate=True,
        )
        validation = await _finish_job(
            hmc, validation_job, True, timeout_seconds, poll_interval
        )
        if validation.timed_out or validation.status not in SUCCESSFUL_JOB_STATUSES:
            detail = validation.error or "no validation error detail returned"
            raise HMCError(
                "LPM validation did not succeed "
                f"(status={validation.status or 'unknown'!r}, error={detail!r}); "
                "migration was not submitted"
            )
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    job = await _submit_migration_job(
        hmc,
        lpar_uuid,
        target_system,
        migration.target_profile_name,
        migration.wait_time,
        validate=False,
    )
    return LpmResult(
        lpar_uuid,
        await _finish_job(hmc, job, wait, timeout_seconds, poll_interval),
    )


async def abort_lpar_migration(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
) -> LpmResult:
    """Resolve and abort an in-progress migration.

    Invalid controls, resolution, authorization, and submission failures raise.
    Waited timeout or terminal job failure is represented by ``result.job``.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    job = await hmc.lpar_migrate_abort(lpar_uuid)
    return LpmResult(
        lpar_uuid,
        await _finish_job(hmc, job, wait, timeout_seconds, poll_interval),
    )


async def recover_lpar_migration(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
) -> LpmResult:
    """Resolve and recover a failed migration.

    Invalid controls, resolution, authorization, and submission failures raise.
    Waited timeout or terminal job failure is represented by ``result.job``.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    job = await hmc.lpar_migrate_recover(lpar_uuid)
    return LpmResult(
        lpar_uuid,
        await _finish_job(hmc, job, wait, timeout_seconds, poll_interval),
    )


async def remote_restart_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    request: RemoteRestartRequest,
    *,
    system_name_or_uuid: str,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
) -> LpmResult:
    """Resolve selectors and submit an explicit RemoteRestart operation.

    Invalid controls, resolution, authorization, and submission failures raise.
    Waited timeout or terminal job failure is represented by ``result.job``.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=request.ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    source_system = await resolve_system_name(hmc, system_name_or_uuid)
    target_name = None
    target_uuid = None
    if request.target_system_name_or_uuid is not None:
        if is_uuid(request.target_system_name_or_uuid):
            target_uuid = request.target_system_name_or_uuid
        else:
            target_name = request.target_system_name_or_uuid
    job = await hmc.lpar_remote_restart(
        lpar_uuid,
        request.operation,
        source_system,
        target_managed_system=target_name,
        target_managed_system_uuid=target_uuid,
        use_current_data=request.use_current_data,
        retain_devices=request.retain_devices,
    )
    return LpmResult(
        lpar_uuid,
        await _finish_job(hmc, job, wait, timeout_seconds, poll_interval),
    )
