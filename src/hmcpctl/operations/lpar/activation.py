"""LPAR activation operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.affinity.rest import (
    LparAffinityAssessmentOutcome,
    ProvisionAffinityAssessment,
    affinity_not_measured,
    assess_post_activation_affinity,
    classify_affinity_outcome,
    validate_affinity_request,
)
from hmcpctl.operations.lpar.ownership import (
    resolve_and_authorize_lpar_mutation,
)
from hmcpctl.operations.lpar.profile_sync import profile_adapter_warnings
from hmcpctl.operations.partition_state import PartitionState

from ...audit import records as audit
from ...errors import HMCError
from ...jobs import (
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    SUCCESSFUL_JOB_STATUSES,
    BootMode,
    PowerOffOperation,
    PowerOnKeylock,
    PowerOnOperationType,
    job_outcome,
    power_off_lpar_job,
    power_on_lpar_job,
    validate_power_off_operation,
    validate_power_on_activation,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import (
    optional_system_selector,
    resolve_lpar_uuid,
)
from ...ssh.transport import HMCCLIError

# The HMC accepts PowerOn only from ``not activated`` (otherwise the REST job fails
# with HSCL3681, and ``chsysstate -o on`` with HSCL05EA). In these
# states the partition is already activated, so the request is already satisfied;
# every other state is refused before a job the HMC would fail is submitted.
ACTIVATED_STATES: frozenset[PartitionState] = frozenset(
    {"running", "starting", "open firmware"}
)


# States a waited PowerOn that completed successfully must not leave the partition
# in: the job can finish COMPLETED_OK while activation fails (live capture, #1165).
_FAILED_ACTIVATION_STATES: frozenset[PartitionState] = frozenset(
    {"error", "not activated"}
)


@dataclass(frozen=True)
class LparPowerResult:
    """An LPAR power outcome paired with its resolved identity."""

    lpar_uuid: str
    job: dict[str, Any] | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LparPowerOnOutcome:
    """Stable public result for an LPAR PowerOn request."""

    already_running: bool
    job: dict[str, Any] | None
    message: str | None
    affinity_assessment: LparAffinityAssessmentOutcome
    warnings: tuple[str, ...]


def power_on_outcome(
    result: LparPowerResult,
    affinity_assessment: LparAffinityAssessmentOutcome | None = None,
) -> LparPowerOnOutcome:
    """Normalize submitted and already-running PowerOn results."""
    job = result.job
    if job is not None and job.get("already_running") is True:
        message = job.get("message")
        return LparPowerOnOutcome(
            already_running=True,
            job=None,
            message=message if isinstance(message, str) else None,
            affinity_assessment=affinity_assessment
            or affinity_not_measured(
                "skipped",
                "No activation was observed because the LPAR was already activated.",
            ),
            warnings=result.warnings,
        )
    return LparPowerOnOutcome(
        already_running=False,
        job=job,
        message=None,
        affinity_assessment=affinity_assessment
        or affinity_not_measured(
            "skipped", "Post-activation assessment was not requested."
        ),
        warnings=result.warnings,
    )


def activation_allows_assessment(result: LparPowerResult) -> tuple[bool, str]:
    """Return whether a waited PowerOn result proves successful activation."""
    outcome = job_outcome("PowerOn", result.job)
    if outcome.timed_out:
        return False, "PowerOn did not reach a terminal status before timeout."
    if outcome.status not in SUCCESSFUL_JOB_STATUSES:
        return False, outcome.error or f"PowerOn ended with status {outcome.status}."
    return True, "PowerOn reached a successful terminal status."


async def require_activated(hmc: HMCClient, result: LparPowerResult) -> None:
    """Raise when a successfully completed waited PowerOn left the partition failed.

    The job status alone does not prove activation: the HMC reports a
    partition-fails-to-activate condition as a clean ``COMPLETED_OK``, and the
    partition state reads ``error`` from the first read after the terminal status.
    """
    if not activation_allows_assessment(result)[0]:
        return
    state = await hmc.get_quick_property(
        "LogicalPartition", result.lpar_uuid, "PartitionState"
    )
    if (state or "").strip().lower() not in _FAILED_ACTIVATION_STATES:
        return
    outcome = job_outcome("PowerOn", result.job)
    raise HMCError(
        f"PowerOn job {outcome.job_id} ended {outcome.status} but LPAR "
        f"{result.lpar_uuid} is in state {state!r}, so activation failed. Read the "
        "reference code with hmc_read_lpar_refcodes; power the partition off "
        "before retrying."
    )


async def power_on_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    force: bool = False,
    affinity_assessment: ProvisionAffinityAssessment | None = None,
    ownership_override: bool = False,
    boot_mode: BootMode = "norm",
    partition_profile_uuid: str | None = None,
    operation_type: PowerOnOperationType | None = None,
    keylock: PowerOnKeylock | None = None,
) -> LparPowerOnOutcome:
    """Activate an LPAR and optionally assess its resulting affinity.

    ``boot_mode``, ``partition_profile_uuid``, ``operation_type`` and ``keylock``
    are passed through to the PowerOn job document; their defaults leave it
    unchanged. ``operation_type="activate"`` is validated and never sent: it is
    the job's default, and a V10R3 HMC refuses the ``OperationType`` parameter.
    Activating against ``partition_profile_uuid`` discards current-configuration
    changes the profile lacks (memory, processor and adapter changes), unless it
    is the partition's current profile and CurrentProfileSync was On when they
    were made.
    """
    system_name_or_uuid = optional_system_selector(system_name_or_uuid)
    if affinity_assessment is not None:
        if system_name_or_uuid is None:
            raise ValueError(
                "system_name_or_uuid is required for post-activation affinity assessment"
            )
        # Compared raw: the assessment is measured against its own captured
        # identity, so a padded capture must not pass as the stripped target.
        if affinity_assessment.system_name_or_uuid != system_name_or_uuid:
            raise ValueError(
                "affinity assessment managed-system identity must match target"
            )
        if affinity_assessment.lpar_name != lpar_name_or_uuid:
            raise ValueError("affinity assessment LPAR identity must match target")
        validate_affinity_request(affinity_assessment)

    result = await submit_power_on_lpar(
        hmc,
        lpar_name_or_uuid,
        system_name_or_uuid=system_name_or_uuid,
        force=force,
        wait=wait,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        ownership_override=ownership_override,
        boot_mode=boot_mode,
        partition_profile_uuid=partition_profile_uuid,
        operation_type=operation_type,
        keylock=keylock,
    )
    if (
        wait
        and result.job is not None
        and result.job.get("already_running") is not True
    ):
        await require_activated(hmc, result)
    if (
        affinity_assessment is None
        or result.job is None
        or result.job.get("already_running") is True
    ):
        return power_on_outcome(result)
    if not wait:
        return power_on_outcome(
            result,
            affinity_not_measured(
                "skipped",
                "Assessment requires wait=true to observe successful activation.",
            ),
        )
    successful, reason = activation_allows_assessment(result)
    if not successful:
        status = "failed" if affinity_assessment.response == "fail" else "unavailable"
        return power_on_outcome(result, affinity_not_measured(status, reason))
    try:
        assessment = await assess_post_activation_affinity(hmc, affinity_assessment)
    except (HMCError, HMCCLIError, ValueError) as exc:
        status = "failed" if affinity_assessment.response == "fail" else "unavailable"
        return power_on_outcome(
            result,
            affinity_not_measured(status, f"Affinity measurement unavailable: {exc}"),
        )
    return power_on_outcome(
        result,
        classify_affinity_outcome(assessment, affinity_assessment.response),
    )


async def _require_contained_partition_profile(
    hmc: HMCClient, lpar_uuid: str, partition_profile_uuid: str
) -> dict[str, Any]:
    """Refuse a partition profile the target partition does not contain.

    ADR 0039: ``hmc_power_on_lpar`` declares ``exhaustive_targets``, which means
    every resource it acts on is the value of a declared selector or is derived
    by the server through the HMC's own containment from one. A caller-supplied
    profile UUID is neither on its own — it is in no target table, so
    ``targets_permitted`` never compares it against the policy grant, and a
    narrow ``targets = {lpar = [...]}`` grant would otherwise bound nothing here.

    Reading the partition's own ``LogicalPartitionProfile`` feed is what makes
    the profile a derived, contained resource. ADR 0044 declined to rest this
    kind of classification on the premise that the HMC would reject the value,
    so the check is made here rather than assumed of the remote end.

    Returns the partition's own profile entry, whose ``UUID`` is the spelling
    to send, because the match is casefolded: echoing the caller's string would
    put a value on the wire that this check never compared.
    """
    profiles = await hmc.list_child(
        "LogicalPartition", lpar_uuid, "LogicalPartitionProfile"
    )
    wanted = partition_profile_uuid.casefold()
    if not profiles or not any(str(profile.get("UUID") or "") for profile in profiles):
        raise ValueError(
            "the target partition's profile feed came back empty or carried no "
            "profile UUID, so the supplied partition profile could not be "
            "verified; read /rest/api/uom/LogicalPartition/<uuid>/"
            "LogicalPartitionProfile on the HMC to confirm the feed"
        )
    for profile in profiles:
        if str(profile.get("UUID") or "").casefold() == wanted:
            return profile
    raise ValueError(
        "partition profile is not a profile of the target partition; "
        "read /rest/api/uom/LogicalPartition/<uuid>/LogicalPartitionProfile on "
        "the HMC to see the profiles it does contain"
    )


def _unapplied_activation_clause(
    boot_mode: BootMode,
    partition_profile_uuid: str | None,
    keylock: PowerOnKeylock | None,
) -> str:
    """Name the activation parameters an already-running partition discarded.

    "Boot this partition into SMS" is usually asked about a running partition,
    and a bare already-running message reads as success to a caller whose
    request was never attempted. Only the parameters actually supplied are
    named, and the remedy is leaving the running state: resubmitting PowerOn
    with ``force`` does not apply a boot mode to a partition already running.
    An operation type is never named: ``activate``, its only value, is the
    default and is never sent (#1392), as the default ``norm`` boot mode is not.
    """
    requested = [
        name
        for name, supplied in (
            ("boot mode", boot_mode != "norm"),
            ("partition profile", bool(partition_profile_uuid)),
            ("keylock position", bool(keylock)),
        )
        if supplied
    ]
    if not requested:
        return ""
    if len(requested) == 1:
        named, verb = requested[0], "was"
    else:
        named = f"{', '.join(requested[:-1])} and {requested[-1]}"
        verb = "were"
    return f" The requested {named} {verb} not applied; power the partition off first."


async def _resolve_power_target(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    system_name_or_uuid: str | None,
    ownership_override: bool,
) -> str:
    """Resolve a power target with the opt-in ADR 0092 ownership guard.

    With the guard off, resolution reads no ownership token and opens no SSH
    connection. With it on, an omitted system is derived and a supplied system
    is checked for containment. An override skips the SSH read and fleet walk,
    but still reads the partition name for the audited override.
    """
    if hmc.config.authorize_power_operations:
        lpar_uuid = await resolve_and_authorize_lpar_mutation(
            hmc,
            lpar_name_or_uuid,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
    else:
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
    return lpar_uuid


async def submit_power_on_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    force: bool = False,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
    boot_mode: BootMode = "norm",
    partition_profile_uuid: str | None = None,
    operation_type: PowerOnOperationType | None = None,
    keylock: PowerOnKeylock | None = None,
) -> LparPowerResult:
    """Submit activation and optionally wait, retaining the raw job result.

    Activation options are validated before ownership reads or audited overrides.
    An active partition returns an already_running result unless force is set.
    A partition profile must belong to the target; its adapter warnings accompany
    the job. Use power_on_lpar for activation readback and affinity assessment.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    validate_power_on_activation(boot_mode, operation_type, keylock)
    lpar_uuid = await _resolve_power_target(
        hmc, lpar_name_or_uuid, system_name_or_uuid, ownership_override
    )
    if not force:
        state = await hmc.get_quick_property(
            "LogicalPartition", lpar_uuid, "PartitionState"
        )
        # REST reports the state in lower case and the CLI in title case; the
        # guard holds for either rendering.
        observed = (state or "").strip().lower()
        if observed in ACTIVATED_STATES:
            unapplied = _unapplied_activation_clause(
                boot_mode, partition_profile_uuid, keylock
            )
            described = "running" if observed == "running" else f"active ({state})"
            return LparPowerResult(
                lpar_uuid,
                {
                    "already_running": True,
                    "message": (
                        f"LPAR {lpar_uuid} is already {described}. "
                        f"No PowerOn job was submitted.{unapplied}"
                    ),
                },
            )
        if observed != "not activated":
            raise HMCError(
                f"Cannot power on LPAR {lpar_uuid} — current state is {state!r}; "
                "PowerOn requires 'not activated', so no job was submitted. Wait "
                "for the partition to settle, or power it off, before retrying.",
                status_code=409,
            )
    warnings: tuple[str, ...] = ()
    if partition_profile_uuid:
        profile = await _require_contained_partition_profile(
            hmc, lpar_uuid, partition_profile_uuid
        )
        partition_profile_uuid = str(profile["UUID"])
        warnings = await profile_adapter_warnings(hmc, lpar_uuid, profile)
    job = await hmc.submit_job(
        f"/rest/api/uom/LogicalPartition/{lpar_uuid}/do/PowerOn",
        power_on_lpar_job(
            profile_uuid=partition_profile_uuid,
            bootmode=boot_mode,
            operation_type=operation_type,
            keylock=keylock,
        ),
    )
    selected_job = await wait_for_submitted_job(
        hmc, job, wait, timeout_seconds, poll_interval
    )
    return LparPowerResult(lpar_uuid, selected_job, warnings)


async def power_off_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    immediate: bool = False,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
    restart: bool = False,
    operation: PowerOffOperation = "shutdown",
    allow_dump_restart: bool = False,
) -> LparPowerResult:
    """Shut down an LPAR, optionally restarting it or requesting a platform dump.

    Ownership is advisory unless authorize_power_operations is enabled. An
    ownership override bypasses rejection for this call and records an audit.
    Validation precedes ownership reads, audited overrides and job submission.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    validate_power_off_operation(
        operation, allow_dump_restart, immediate=immediate, restart=restart
    )
    lpar_uuid = await _resolve_power_target(
        hmc, lpar_name_or_uuid, system_name_or_uuid, ownership_override
    )
    job = await submit_power_off(
        hmc,
        lpar_uuid,
        operation=operation,
        immediate=immediate,
        restart=restart,
        allow_dump_restart=allow_dump_restart,
    )
    selected_job = await wait_for_submitted_job(
        hmc, job, wait, timeout_seconds, poll_interval
    )
    return LparPowerResult(lpar_uuid, selected_job)


async def submit_power_off(
    hmc: HMCClient,
    lpar_uuid: str,
    *,
    operation: PowerOffOperation,
    immediate: bool,
    restart: bool,
    allow_dump_restart: bool = False,
) -> dict[str, Any] | None:
    """Audit, then submit, one PowerOff job for an already-authorized partition.

    The document is built first, so a refused vocabulary records nothing. The audit
    record is written before the submit: a submit that raises may still have reached
    the HMC.
    """
    document = power_off_lpar_job(
        immediate=immediate,
        restart=restart,
        operation=operation,
        allow_dump_restart=allow_dump_restart,
    )
    audit.record_lpar_power_off(
        lpar=lpar_uuid,
        host=hmc.config.host,
        operation=operation,
        immediate=immediate,
        restart=restart,
        agent_id=hmc.config.agent_id or "hmcpctl",
    )
    return await hmc.submit_job(
        f"/rest/api/uom/LogicalPartition/{lpar_uuid}/do/PowerOff", document
    )
