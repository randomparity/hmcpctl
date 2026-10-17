"""Shared LPAR creation and ownership operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.affinity.rest import (
    LparAffinityAssessmentOutcome,
    ProvisionAffinityAssessment,
    affinity_not_measured,
    assess_post_activation_affinity,
    classify_affinity_outcome,
    validate_affinity_request,
)
from hmcpctl.operations.lpar.errors import translate_lpar_write_error
from hmcpctl.operations.lpar.memory_bound import require_memory_within_system
from hmcpctl.operations.lpar.ownership import (
    resolve_and_authorize_lpar_mutation,
    stamp_created_lpar_ownership,
)
from hmcpctl.operations.lpar.profile_sync import profile_adapter_warnings
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.partition_state import PARTITION_STATES, PartitionState
from hmcpctl.operations.systems.fleet import FleetListing, read_fleet

from ...audit import records as audit
from ...documents.lpar import (
    Keylock,
    LparResources,
    PartitionType,
    build_lpar_document,
    partition_updates,
    validate_keylock,
    validate_partition_type,
)
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
    is_uuid,
    optional_system_selector,
    resolve_lpar_uuid,
    resolve_system_uuid,
)
from ...ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    apply_lpar_profile_via_cli,
    complete_create_resources,
    create_lpar_via_cli,
    resolve_system_cli_name,
    validate_caller_token,
)
from ...ssh.transport import HMCCLIError
from ...xmlutil import escape_xml

_logger = logging.getLogger(__name__)

_LPAR_POWER_OPERATIONS = frozenset({"PowerOn", "PowerOff"})
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

ProcessorCompatibilityMode = Literal[
    "default",
    "POWER5",
    "POWER6",
    "POWER6+",
    "POWER7",
    "POWER8",
    "POWER9_Base",
    "POWER9_base",
    "POWER9",
    "POWER10",
    "POWER11",
]
PROCESSOR_COMPATIBILITY_MODES: frozenset[ProcessorCompatibilityMode] = frozenset(
    {
        "default",
        "POWER5",
        "POWER6",
        "POWER6+",
        "POWER7",
        "POWER8",
        "POWER9_Base",
        "POWER9_base",
        "POWER9",
        "POWER10",
        "POWER11",
    }
)


async def list_lpars(
    hmc: HMCClient,
    system_name_or_uuid: str | None = None,
    state: PartitionState | None = None,
) -> FleetListing:
    """List LPARs, optionally scoped to one system and partition state.

    Without a system, each operating managed system is read in turn and the
    others are named in ``unreadable_systems`` (ADR 0197).
    """
    if state is not None and state not in PARTITION_STATES:
        allowed = ", ".join(sorted(PARTITION_STATES))
        raise ValueError(f"state must be one of: {allowed}")
    selector = optional_system_selector(system_name_or_uuid)
    system_uuid = (
        await resolve_system_uuid(hmc, selector) if selector is not None else None
    )
    # No PartitionState search: a V10R3 HMC answers a value with a space
    # ("not activated") with 500 "Unable to parse expression" (#1202).
    listing = (
        FleetListing(await hmc.list_logical_partitions(system_uuid))
        if system_uuid is not None
        else await read_fleet(hmc, hmc.list_logical_partitions, "LPARs")
    )
    if state is None:
        return listing
    return replace(
        listing,
        entries=[
            entry
            for entry in listing.entries
            if (entry.get("Resource") or {}).get("PartitionState") == state
        ],
    )


async def get_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get one LPAR by UUID or by an optionally system-scoped exact name."""
    if is_uuid(lpar_name_or_uuid):
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
        return await hmc.get_logical_partition(lpar_uuid)
    selector = optional_system_selector(system_name_or_uuid)
    system_uuid = (
        await resolve_system_uuid(hmc, selector) if selector is not None else None
    )
    return await hmc.find_partition_by_name(lpar_name_or_uuid, system_uuid=system_uuid)


async def get_lpar_state(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> str | None:
    """Return one LPAR's state by UUID or an optionally system-scoped name.

    System scope disambiguates duplicate partition names; UUID selectors do not
    require it.
    """
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    return await hmc.get_quick_property("LogicalPartition", lpar_uuid, "PartitionState")


@dataclass(frozen=True)
class LparCreation:
    """Inputs needed by both REST and CLI LPAR creation paths."""

    name: str
    partition_type: PartitionType
    resources: LparResources
    partition_id: int | None = None
    keylock: Keylock | None = None
    max_virtual_slots: int | None = None
    caller_token: str | None = None
    stamp_policy: Literal["best-effort", "required"] = "best-effort"
    # True applies the profile mksyscfg created (#939) and, after a REST create,
    # reports the apply as skipped because none is needed (#1164); False reports a
    # skipped apply step; None (not requested) adds no step.
    apply_profile: bool | None = None


@dataclass(frozen=True)
class LparCreationResult:
    """Result shared by direct creation and provisioning workflows."""

    resource_created: bool
    lpar: dict[str, Any] | None
    ownership_stamped: bool | None
    warnings: tuple[str, ...]
    apply_step: WorkflowStep | None = None


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

    result = await power_lpar(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        power_on=True,
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


def _rest_create_refused(exc: HMCError) -> bool:
    """Whether the HMC refused the REST create before creating anything (ADR 0178).

    A 406 is a negotiation refusal. A 400 ``REST0001`` is the HMC failing to
    unmarshal the document we sent -- a defect in our payload or a caller value,
    not the firmware refusing the create path (#1164). Either leaves ``mksyscfg``
    as the way to create, and the caller logs the HMC's message.
    """
    return exc.status_code == 406 or (
        exc.status_code == 400 and "REST0001" in (exc.body or "")
    )


# A V10R3 REST create writes default_profile and a current configuration, and the
# partition activates without an apply (#1164).
_REST_CREATE_CONFIGURED = (
    "the REST create set the current configuration, so no apply was needed"
)


async def _create_via_cli(
    hmc: HMCClient,
    system_uuid: str,
    system_name_or_uuid: str,
    creation: LparCreation,
) -> tuple[str, WorkflowStep | None, dict[str, Any] | None, HMCError | None]:
    """Create with ``mksyscfg``, apply its profile, and read the partition back.

    Returns the CLI system name, the apply step, the created partition (None
    when the read-back failed) and the read-back error.
    """
    try:
        system_name = await resolve_system_cli_name(hmc.config, system_uuid)
    except HMCCLIError:
        system_name = system_name_or_uuid
    await create_lpar_via_cli(
        hmc.config,
        system_name=system_name,
        name=creation.name,
        partition_type=creation.partition_type,
        resources=creation.resources,
        max_virtual_slots=creation.max_virtual_slots,
    )
    apply_step = (
        await _apply_created_profile(hmc, system_name, creation)
        if creation.apply_profile is not None
        else None
    )
    try:
        created_lpar = await hmc.find_partition_by_name(
            creation.name, system_uuid=system_uuid
        )
    except HMCError as exc:
        return system_name, apply_step, None, exc
    return system_name, apply_step, created_lpar, None


async def create_and_stamp_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
) -> LparCreationResult:
    """Validate and create an LPAR with fallback and ownership stamping.

    ``creation.stamp_policy`` selects how an ownership-stamp failure or skip
    is surfaced (issue #377):

    - ``"best-effort"`` (default, ADR 0011): a failed or skipped stamp never
      fails the call. The result reports it through ``ownership_stamped``
      and ``warnings``.
    - ``"required"``: any outcome other than a confirmed stamp raises
      :class:`HMCError` *after* the create completes. The exception carries
      the new LPAR's name and UUID so the caller can find the partition.
      The LPAR **still exists** when the error is raised — the create is not
      rolled back — so re-stamp it with
      :func:`set_lpar_ownership_description` (issue #376, ADR 0066) or
      delete it to release its resources.
    """
    if creation.stamp_policy not in ("best-effort", "required"):
        raise ValueError(
            f"stamp_policy must be 'best-effort' or 'required', "
            f"got {creation.stamp_policy!r}"
        )
    if creation.caller_token is not None:
        # First statement, before find_partition_by_name and outside the
        # stamp's best-effort catch: no create can precede rejection, and a
        # malformed token can never discard the ownership stamp (ADR 0064).
        validate_caller_token(creation.caller_token)
    validate_partition_type(creation.partition_type)
    validate_keylock(creation.keylock)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    # Scoped: the HMC-wide search misses partitions on a system it cannot
    # read (#1309), and the scoped feed refuses a non-operating system.
    existing = await hmc.find_partition_by_name(creation.name, system_uuid=system_uuid)
    if existing:
        raise ValueError(
            f"An LPAR named {creation.name!r} already exists "
            f"(UUID {existing.get('UUID')!r}). Choose a different name "
            "or delete the existing partition first."
        )
    await require_memory_within_system(hmc, system_uuid, creation.resources)
    system_name: str | None = None
    apply_step: WorkflowStep | None = None
    readback_error: HMCError | None = None
    resources = complete_create_resources(creation.resources)
    if resources is None:
        # Only mksyscfg's all_resources=1 expresses a create with no resource values.
        system_name, apply_step, created_lpar, readback_error = await _create_via_cli(
            hmc, system_uuid, system_name_or_uuid, creation
        )
    else:
        document = build_lpar_document(
            name=creation.name,
            partition_type=creation.partition_type,
            partition_id=creation.partition_id,
            resources=resources,
            keylock=creation.keylock,
            max_virtual_slots=creation.max_virtual_slots,
        )
        try:
            created_lpar = await hmc.create_logical_partition(system_uuid, document)
        except HMCError as exc:
            if not _rest_create_refused(exc):
                raise
            _logger.warning(
                "REST create of LPAR %r was refused, falling back to mksyscfg: %s",
                creation.name,
                exc,
            )
            (
                system_name,
                apply_step,
                created_lpar,
                readback_error,
            ) = await _create_via_cli(hmc, system_uuid, system_name_or_uuid, creation)
        else:
            if creation.apply_profile is True:
                apply_step = WorkflowStep(
                    "apply_profile", "skipped", _REST_CREATE_CONFIGURED
                )
    apply_warnings = _unapplied_profile_warnings(creation.name, apply_step)
    apply_note = f" {'; '.join(apply_warnings)}" if apply_warnings else ""

    if created_lpar is None:
        if creation.stamp_policy == "required" and readback_error is not None:
            raise HMCError(
                f"stamp_policy='required': mksyscfg created LPAR {creation.name!r} "
                f"but its read-back failed ({readback_error}), so it was not "
                f"stamped.{apply_note} The LPAR still exists — re-stamp it with "
                "set_lpar_ownership_description or delete it to release its "
                "resources."
            ) from readback_error
        if creation.stamp_policy == "required":
            raise HMCError(
                "stamp_policy='required': cannot confirm the created LPAR "
                f"exists — the create returned no body for {creation.name!r}."
                f"{apply_note} Verify whether the partition was created before "
                "retrying; a created partition can be re-stamped with "
                "set_lpar_ownership_description."
            )
        skipped = (
            "create returned no LPAR body"
            if readback_error is None
            else f"read-back after mksyscfg failed: {readback_error}"
        )
        return LparCreationResult(
            resource_created=True,
            lpar=None,
            ownership_stamped=None,
            warnings=(
                *apply_warnings,
                f"ownership stamp skipped for LPAR {creation.name!r}: {skipped}",
            ),
            apply_step=apply_step,
        )
    ownership_stamped, warnings = await stamp_created_lpar_ownership(
        hmc,
        system_uuid,
        system_name or system_name_or_uuid,
        created_lpar,
        caller_token=creation.caller_token,
    )
    if creation.stamp_policy == "required" and ownership_stamped is not True:
        resource = created_lpar.get("Resource") or {}
        name = resource.get("PartitionName") or creation.name
        uuid = created_lpar.get("UUID")
        identity = f"{name!r} (UUID {uuid!r})" if uuid else f"{name!r} (UUID unknown)"
        raise HMCError(
            "stamp_policy='required': ownership stamping did not succeed for "
            f"LPAR {identity}. {'; '.join((*apply_warnings, *warnings))} The "
            "LPAR still exists — the create is not rolled back. Re-stamp it "
            "with set_lpar_ownership_description (issue #376) or delete it to "
            "release its resources."
        )
    return LparCreationResult(
        True,
        created_lpar,
        ownership_stamped,
        (*apply_warnings, *warnings),
        apply_step,
    )


async def _apply_created_profile(
    hmc: HMCClient, system_name: str, creation: LparCreation
) -> WorkflowStep:
    """Apply the mksyscfg-created profile, reporting the outcome as a step.

    An apply failure is reported, not raised: the partition already exists and
    is not rolled back.
    """
    if creation.apply_profile is False:
        return WorkflowStep("apply_profile", "skipped")
    try:
        await apply_lpar_profile_via_cli(hmc.config, system_name, creation.name)
    except HMCCLIError as exc:
        return WorkflowStep("apply_profile", "error", str(exc))
    return WorkflowStep("apply_profile", "ok", DEFAULT_PROFILE_NAME)


def _unapplied_profile_warnings(
    name: str, apply_step: WorkflowStep | None
) -> tuple[str, ...]:
    """Say that a skipped or failed apply left the partition unconfigured."""
    if (
        apply_step is None
        or apply_step.status == "ok"
        or apply_step.result == _REST_CREATE_CONFIGURED
    ):
        return ()
    warning = (
        f"partition profile {DEFAULT_PROFILE_NAME!r} was not applied: {name!r} has "
        "no current configuration, so its current memory and processors read as "
        "zero until the profile is applied or the partition is activated"
    )
    if apply_step.status == "error":
        warning += (
            "; apply it with chsyscfg -o apply or activate with it, then redo any "
            "skipped steps"
        )
    return (warning,)


async def delete_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize and delete a powered-off LPAR, returning its UUID."""
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    state = await hmc.get_quick_property(
        "LogicalPartition", lpar_uuid, "PartitionState"
    )
    if state != "not activated":
        raise HMCError(
            f"Cannot delete LPAR {lpar_uuid} — current state is {state!r}; "
            "it must be 'not activated' to delete. Power off the partition "
            "and verify its state before retrying.",
            status_code=409,
        )
    await hmc.delete_logical_partition(lpar_uuid)
    return lpar_uuid


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


async def power_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    *,
    power_on: bool,
    immediate: bool = False,
    force: bool = False,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
    ownership_override: bool = False,
    boot_mode: BootMode = "norm",
    partition_profile_uuid: str | None = None,
    operation_type: PowerOnOperationType | None = None,
    keylock: PowerOnKeylock | None = None,
    restart: bool = False,
    operation: PowerOffOperation = "shutdown",
    allow_dump_restart: bool = False,
) -> LparPowerResult:
    """Apply shared LPAR power policy, submit the job, and optionally wait.

    ``boot_mode``, ``partition_profile_uuid``, ``operation_type`` and ``keylock``
    are activation parameters and apply to PowerOn only; the PowerOff arm builds a
    different document and ignores them. ``partition_profile_uuid`` is the UUID
    of a partition profile, not the connection profile the tool and CLI call
    ``profile``. Their defaults emit the document this call has always emitted.

    ``restart``, ``operation`` and ``allow_dump_restart`` mirror them on the other
    arm: they apply to PowerOff only, and the PowerOn arm ignores them. ``operation`` is the
    job's closed vocabulary — ``shutdown``, ``osshutdown`` (needs an active RMC
    connection to the partition's operating system), and ``dumprestart``, which
    crashes the partition and takes a platform dump and is refused unless
    ``allow_dump_restart`` is set (ADR 0164). Their defaults likewise emit the
    document this call has always emitted.

    ADR 0011 ownership is advisory here by default. Powering a partition another
    agent owns is only rejected when the operator sets
    ``authorize_power_operations`` (``HMC_AUTHORIZE_POWER_OPERATIONS``), the
    opt-in ADR 0092 §4 records; with the setting off this call reads no
    ownership token and opens no SSH connection, so a caller that cares should
    read the description itself — ``list_lpar_ownership`` reports it for a whole
    managed system in one REST call.

    With the setting on the resolve chain is ADR 0094's
    :func:`resolve_and_authorize_lpar_mutation`, shared with the DLPAR operations —
    the same shape, because these are the operations whose managed-system
    selector is optional (ADR 0063). It derives the owning system when the
    caller omits the selector, and confirms the partition lives on the system
    the caller named when they supply one, so the token read is never taken
    from a system the partition does not belong to.

    ``ownership_override=True`` bypasses the rejection for this one call and
    records an audited override. It skips the SSH ownership read and the fleet
    walk, not the partition-name read that names the audit record. When the
    ownership read fails or times out, the call fails with
    :class:`HMCCLIError` and submits no job.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    if power_on:
        # Ahead of every side effect: the ownership leg below can write an
        # audited override, and the already-running branch never reaches a builder.
        validate_power_on_activation(boot_mode, operation_type, keylock)
    else:
        # Same reason on this arm: the ownership leg runs before the builder does.
        validate_power_off_operation(
            operation, allow_dump_restart, immediate=immediate, restart=restart
        )
    if hmc.config.authorize_power_operations:
        lpar_uuid = await resolve_and_authorize_lpar_mutation(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            ownership_override=ownership_override,
        )
    else:
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
    if power_on and not force:
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
    if power_on and partition_profile_uuid:
        profile = await _require_contained_partition_profile(
            hmc, lpar_uuid, partition_profile_uuid
        )
        partition_profile_uuid = str(profile["UUID"])
        warnings = await profile_adapter_warnings(hmc, lpar_uuid, profile)
    # Named for the ``/do/{operation}`` path segment ADR 0158 governs, and kept
    # distinct from the PowerOff job parameter that now owns the name ``operation``.
    path_operation = "PowerOn" if power_on else "PowerOff"
    if path_operation not in _LPAR_POWER_OPERATIONS:
        allowed = ", ".join(sorted(_LPAR_POWER_OPERATIONS))
        raise ValueError(f"LPAR power job operation must be one of: {allowed}")
    if power_on:
        job = await hmc.submit_job(
            f"/rest/api/uom/LogicalPartition/{lpar_uuid}/do/{path_operation}",
            power_on_lpar_job(
                profile_uuid=partition_profile_uuid,
                bootmode=boot_mode,
                operation_type=operation_type,
                keylock=keylock,
            ),
        )
    else:
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
    return LparPowerResult(lpar_uuid, selected_job, warnings)


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


async def rename_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    new_name: str,
    *,
    ownership_override: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve, authorize, and rename one LPAR by whole-partition read-modify-write."""
    escape_xml(new_name)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    try:
        updated = await hmc.update_logical_partition(
            lpar_uuid,
            lambda lpar: partition_updates(lpar, name=new_name),
            "the partition name",
        )
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    return lpar_uuid, updated
