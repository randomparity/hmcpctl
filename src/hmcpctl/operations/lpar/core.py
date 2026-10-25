"""Shared LPAR creation and ownership operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.errors import translate_lpar_write_error
from hmcpctl.operations.lpar.memory_bound import require_memory_within_system
from hmcpctl.operations.lpar.ownership import (
    resolve_and_authorize_lpar_mutation,
    stamp_created_lpar_ownership,
)
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

from ...documents.lpar import (
    Keylock,
    LparReportingSettings,
    LparResources,
    LparSettings,
    PartitionType,
    build_lpar_document,
    partition_updates,
    validate_keylock,
    validate_partition_type,
)
from ...documents.lpar_hardware_memory import hardware_memory_pairs
from ...documents.lpar_io import LparPhysicalIO
from ...documents.lpar_security_policy import LparSecurityPolicy
from ...documents.lpar_vtpm import LparVtpmSettings
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_system_uuid,
)
from ...ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    apply_lpar_profile_via_cli,
    complete_create_resources,
    create_lpar_via_cli,
    get_lpar_reporting_settings,
    get_lpar_settings,
    resolve_system_cli_name,
    validate_caller_token,
)
from ...ssh.lpar_vtpm import get_lpar_vtpm_state
from ...ssh.transport import HMCCLIError
from ...xmlutil import escape_xml
from .assignments import SriovLogicalPortAssignment
from .configuration import configure_lpar_settings
from .hardware_memory import admit_hardware_memory, created_hardware_memory_step
from .physical_io import admit_creation_io, created_io_step
from .security_policy import (
    admit_security_policy,
    read_lpar_security_policy,
    read_security_policy_capabilities,
)
from .vtpm import admit_vtpm, read_vtpm_capabilities

_logger = logging.getLogger(__name__)


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
    settings: LparSettings = field(default_factory=LparSettings)
    physical_io: LparPhysicalIO = field(default_factory=LparPhysicalIO)
    creation_sriov: tuple[SriovLogicalPortAssignment, ...] = ()
    reporting_settings: LparReportingSettings = field(
        default_factory=LparReportingSettings
    )
    vtpm_settings: LparVtpmSettings = field(default_factory=LparVtpmSettings)
    security_policy: LparSecurityPolicy = field(default_factory=LparSecurityPolicy)
    hardware_mem_encryption: bool | None = None
    hardware_mem_expansion: bool | None = None


@dataclass(frozen=True)
class LparCreationResult:
    """Result shared by direct creation and provisioning workflows."""

    resource_created: bool
    lpar: dict[str, Any] | None
    ownership_stamped: bool | None
    warnings: tuple[str, ...]
    apply_step: WorkflowStep | None = None
    settings_step: WorkflowStep | None = None
    physical_io_step: WorkflowStep | None = None
    reporting_step: WorkflowStep | None = None
    vtpm_step: WorkflowStep | None = None
    security_policy_step: WorkflowStep | None = None
    hardware_memory_step: WorkflowStep | None = None


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
    if creation.keylock is not None:
        raise ValueError(
            "CLI creation cannot represent keylock; supply complete resources on an HMC accepting REST creation. Nothing was created."
        )
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
        partition_id=creation.partition_id,
        settings=creation.settings,
        reporting_settings=creation.reporting_settings,
        vtpm_settings=creation.vtpm_settings,
        physical_io=creation.physical_io,
        creation_sriov=creation.creation_sriov,
        security_policy=creation.security_policy,
        hardware_mem_encryption=creation.hardware_mem_encryption,
        hardware_mem_expansion=creation.hardware_mem_expansion,
    )
    apply_step = (
        await _apply_created_profile(hmc, system_name, creation)
        if creation.apply_profile is not None
        and not creation.physical_io.requested
        and not creation.security_policy.pairs()
        and creation.hardware_mem_encryption is None
        and creation.hardware_mem_expansion is None
        else None
    )
    try:
        created_lpar = await hmc.find_partition_by_name(
            creation.name, system_uuid=system_uuid
        )
    except HMCError as exc:
        return system_name, apply_step, None, exc
    except ValueError:
        if (
            creation.hardware_mem_encryption is None
            and creation.hardware_mem_expansion is None
        ):
            raise
        # Native creation succeeded, but ambiguous identity evidence is unavailable.
        # Do not retain provider details in the partial result or exception chain.
        return system_name, apply_step, None, None
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
    hardware_requested = bool(
        hardware_memory_pairs(
            creation.hardware_mem_encryption, creation.hardware_mem_expansion
        )
    )
    if hardware_requested and creation.keylock is not None:
        raise ValueError(
            "Hardware memory settings cannot be combined with explicit keylock: native creation cannot represent keylock. Nothing was created"
        )
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
    if not isinstance(creation.security_policy, LparSecurityPolicy):
        raise TypeError("security_policy must be LparSecurityPolicy")
    creation.security_policy.validate_creation(creation.partition_type)
    policy_requested = bool(creation.security_policy.pairs())
    if not isinstance(creation.vtpm_settings, LparVtpmSettings):
        raise TypeError("vtpm_settings must be LparVtpmSettings")
    creation.vtpm_settings.validate_creation(creation.partition_type)
    if creation.vtpm_settings.pairs() and creation.keylock is not None:
        raise ValueError(
            "vTPM settings cannot be combined with explicit keylock: native creation cannot represent keylock. Nothing was created"
        )
    if not isinstance(creation.settings, LparSettings):
        raise TypeError("settings must be LparSettings")
    if (
        creation.settings.simplified_remote_restart_capable is True
        and creation.security_policy.keystore_kbytes is not None
        and creation.security_policy.keystore_kbytes > 0
    ):
        raise ValueError(
            "Creation combining simplified remote restart with a nonzero keystore "
            "requires a supported positive user-defined system-key check. "
            "Select only one of these features for this creation. "
            "Nothing was created."
        )
    if not isinstance(creation.physical_io, LparPhysicalIO):
        raise TypeError("physical_io must be LparPhysicalIO")
    validate_partition_type(creation.partition_type)
    if not isinstance(creation.reporting_settings, LparReportingSettings):
        raise TypeError("reporting_settings must be LparReportingSettings")
    creation.reporting_settings.validate_context("create", creation.partition_type)
    if creation.reporting_settings.pairs() and creation.keylock is not None:
        raise ValueError(
            "Reporting settings cannot be combined with explicit keylock: CLI creation cannot represent keylock. Nothing was created."
        )
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
    if creation.vtpm_settings.pairs():
        admit_vtpm(
            creation.vtpm_settings, await read_vtpm_capabilities(hmc, system_uuid)
        )
    if policy_requested:
        admit_security_policy(
            creation.security_policy,
            await read_security_policy_capabilities(
                hmc,
                system_uuid,
                need_power11=creation.partition_type == "OS400"
                and creation.security_policy.secure_boot is not None,
            ),
            environment=creation.partition_type,
        )
    await admit_hardware_memory(
        hmc,
        system_uuid,
        creation.hardware_mem_encryption,
        creation.hardware_mem_expansion,
    )
    await admit_creation_io(hmc, system_uuid, creation)
    await require_memory_within_system(hmc, system_uuid, creation.resources)
    system_name: str | None = None
    apply_step: WorkflowStep | None = None
    readback_error: HMCError | None = None
    resources = complete_create_resources(creation.resources)
    if (policy_requested or hardware_requested) and resources is not None:
        await require_memory_within_system(hmc, system_uuid, resources)
    if (
        resources is None
        or policy_requested
        or hardware_requested
        or creation.reporting_settings.pairs()
        or creation.vtpm_settings.pairs()
        or creation.physical_io.requested
        or (creation.settings.pairs() and creation.keylock is None)
    ):
        # Only mksyscfg's all_resources=1 expresses a create with no resource values.
        native_failed = False
        try:
            (
                system_name,
                apply_step,
                created_lpar,
                readback_error,
            ) = await _create_via_cli(hmc, system_uuid, system_name_or_uuid, creation)
        except HMCError as error:
            if hardware_requested:
                native_failed = True
            elif not creation.physical_io.requested and not policy_requested:
                raise
            else:
                raise HMCError(
                    "Native policy creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
                    if policy_requested
                    else "Native I/O creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
                ) from error
        if native_failed:
            raise HMCError(
                "Native hardware memory creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
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
        if (
            policy_requested or hardware_requested
        ) and creation.stamp_policy == "required":
            raise HMCError(
                f"{'Hardware memory' if hardware_requested else 'Policy'} creation may have succeeded, but created identity readback is "
                "unavailable and ownership was not stamped. Inspect the contained "
                "partition before retrying; no rollback performed."
            ) from None
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
        if policy_requested or hardware_requested:
            skipped = "created identity readback unavailable; inspect the contained partition before retrying"
        elif readback_error is None:
            skipped = "create returned no LPAR body"
        else:
            skipped = f"read-back after mksyscfg failed: {readback_error}"
        return LparCreationResult(
            resource_created=True,
            lpar=None,
            ownership_stamped=None,
            warnings=(
                *apply_warnings,
                f"ownership stamp skipped for LPAR {creation.name!r}: {skipped}",
            ),
            apply_step=apply_step,
            hardware_memory_step=await created_hardware_memory_step(
                hmc,
                system_uuid,
                created_lpar,
                creation.name,
                creation.partition_type,
                creation.hardware_mem_encryption,
                creation.hardware_mem_expansion,
            ),
            security_policy_step=WorkflowStep(
                "security_policy",
                "error",
                "Created partition identity unavailable; policy unverified",
            )
            if policy_requested
            else None,
            physical_io_step=WorkflowStep(
                "physical_io",
                "error",
                "Created partition identity unavailable; I/O unverified",
            )
            if creation.physical_io.requested
            else None,
            settings_step=(
                WorkflowStep(
                    "settings",
                    "error",
                    "Created partition readback unavailable; settings unverified",
                )
                if creation.settings.pairs()
                else None
            ),
            vtpm_step=WorkflowStep(
                "vtpm", "error", "Created identity unavailable; vTPM unverified"
            )
            if creation.vtpm_settings.pairs()
            else None,
            reporting_step=(
                WorkflowStep(
                    "reporting_settings",
                    "error",
                    "Created partition readback unavailable; reporting settings unverified",
                )
                if creation.reporting_settings.pairs()
                else None
            ),
        )
    if (policy_requested or hardware_requested) and (
        not isinstance(created_lpar.get("UUID"), str)
        or not is_uuid(created_lpar["UUID"])
        or not isinstance(created_lpar.get("Resource"), dict)
        or created_lpar["Resource"].get("PartitionName") != creation.name
    ):
        if creation.stamp_policy == "required":
            raise HMCError(
                "stamp_policy='required': creation may have occurred, but partition "
                "identity and ownership are unconfirmed. Inspect the contained "
                "partition before retrying; no rollback performed."
            )
        return LparCreationResult(
            True,
            created_lpar,
            None,
            (),
            security_policy_step=WorkflowStep(
                "security_policy",
                "error",
                "Created partition identity unconfirmed; inspect before retrying",
            )
            if policy_requested
            else None,
            hardware_memory_step=await created_hardware_memory_step(
                hmc,
                system_uuid,
                created_lpar,
                creation.name,
                creation.partition_type,
                creation.hardware_mem_encryption,
                creation.hardware_mem_expansion,
            ),
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
    hardware_step = await created_hardware_memory_step(
        hmc,
        system_uuid,
        created_lpar,
        creation.name,
        creation.partition_type,
        creation.hardware_mem_encryption,
        creation.hardware_mem_expansion,
    )
    io_step = await created_io_step(hmc, system_uuid, creation, created_lpar)
    if io_step is not None and ownership_stamped is not True:
        io_step = WorkflowStep(
            "physical_io",
            "error",
            "Partition exists but ownership stamp unconfirmed; I/O workflow stopped before apply",
        )
    settings_step = (
        await _created_settings_step(
            hmc, system_uuid, system_name, creation, created_lpar
        )
        if policy_requested or hardware_requested
        else None
    )
    reporting_step = (
        await _created_reporting_step(hmc, system_name, creation)
        if policy_requested or hardware_requested
        else None
    )
    vtpm_step = (
        await _created_vtpm_step(hmc, system_name, creation)
        if policy_requested or hardware_requested
        else None
    )
    policy_step = await _created_security_policy_step(
        hmc, system_uuid, creation, created_lpar
    )
    if (policy_requested or hardware_requested) and creation.apply_profile is not None:
        required = [
            (creation.settings.pairs(), settings_step),
            (creation.reporting_settings.pairs(), reporting_step),
            (creation.vtpm_settings.pairs(), vtpm_step),
            (creation.physical_io.requested, io_step),
            (policy_requested, policy_step),
            (hardware_requested, hardware_step),
        ]
        verified = ownership_stamped is True and all(
            not requested or (step is not None and step.status == "ok")
            for requested, step in required
        )
        apply_step = (
            await _apply_created_profile(
                hmc, system_name or system_name_or_uuid, creation
            )
            if verified
            else WorkflowStep(
                "apply_profile",
                "skipped",
                "Creation verification incomplete; profile not applied",
            )
        )
    elif creation.physical_io.requested and creation.apply_profile is not None:
        apply_step = (
            await _apply_created_profile(
                hmc, system_name or system_name_or_uuid, creation
            )
            if io_step is not None and io_step.status == "ok"
            else WorkflowStep(
                "apply_profile",
                "skipped",
                "I/O readback unverified; profile not applied",
            )
        )
    if creation.physical_io.requested or policy_requested or hardware_requested:
        apply_warnings = _unapplied_profile_warnings(creation.name, apply_step)
    return LparCreationResult(
        True,
        created_lpar,
        ownership_stamped,
        (*apply_warnings, *warnings),
        apply_step,
        settings_step
        if policy_requested or hardware_requested
        else await _created_settings_step(
            hmc, system_uuid, system_name, creation, created_lpar
        ),
        io_step,
        reporting_step
        if policy_requested or hardware_requested
        else await _created_reporting_step(hmc, system_name, creation),
        vtpm_step
        if policy_requested or hardware_requested
        else await _created_vtpm_step(hmc, system_name, creation),
        policy_step,
        hardware_step,
    )


async def _created_security_policy_step(
    hmc: HMCClient,
    system_uuid: str,
    creation: LparCreation,
    created_lpar: dict[str, Any],
) -> WorkflowStep | None:
    if not creation.security_policy.pairs():
        return None
    try:
        uuid = created_lpar.get("UUID")
        if (
            not isinstance(uuid, str)
            or not is_uuid(uuid)
            or (created_lpar.get("Resource") or {}).get("PartitionName")
            != creation.name
        ):
            raise ValueError("Created partition identity unconfirmed")
        observed = await read_lpar_security_policy(
            hmc, uuid, system_name_or_uuid=system_uuid
        )
        matches = observed.environment == creation.partition_type and all(
            getattr(observed.settings, name) == getattr(creation.security_policy, name)
            for name, _ in creation.security_policy.pairs()
        )
    except (HMCError, ValueError, PermissionError):
        return WorkflowStep(
            "security_policy",
            "error",
            "Partition exists or may exist; policy unverified. Inspect before retrying",
        )
    return WorkflowStep("security_policy", "ok" if matches else "error", observed)


async def _created_vtpm_step(
    hmc: HMCClient, cli_system: str | None, creation: LparCreation
) -> WorkflowStep | None:
    if not creation.vtpm_settings.pairs():
        return None
    try:
        if cli_system is None:
            raise ValueError("Native vTPM creation did not retain its system identity")
        observed = await get_lpar_vtpm_state(hmc.config, cli_system, creation.name)
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "vtpm", "error", f"Partition exists; vTPM unverified: {error}"
        )
    matches = all(
        getattr(observed.settings, name) == getattr(creation.vtpm_settings, name)
        for name, _ in creation.vtpm_settings.pairs()
    )
    return WorkflowStep("vtpm", "ok" if matches else "error", observed)


async def _created_reporting_step(
    hmc: HMCClient, cli_system: str | None, creation: LparCreation
) -> WorkflowStep | None:
    if not creation.reporting_settings.pairs():
        return None
    try:
        if cli_system is None:
            raise ValueError(
                "Reporting creation did not retain its CLI system identity"
            )
        observed = await get_lpar_reporting_settings(
            hmc.config,
            cli_system,
            creation.name,
            creation=True,
            require_ibmi=creation.reporting_settings.requires_ibmi(),
        )
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "reporting_settings",
            "error",
            f"Partition exists; reporting unverified: {error}",
        )
    matches = all(
        getattr(observed, name) == getattr(creation.reporting_settings, name)
        for name, _ in creation.reporting_settings.pairs()
    )
    return WorkflowStep("reporting_settings", "ok" if matches else "error", observed)


async def _created_settings_step(
    hmc: HMCClient,
    system_uuid: str,
    cli_system: str | None,
    creation: LparCreation,
    created: dict[str, Any],
) -> WorkflowStep | None:
    if not creation.settings.pairs():
        return None
    try:
        if cli_system is None:
            result = await configure_lpar_settings(
                hmc,
                created.get("UUID") or creation.name,
                creation.settings,
                system_name_or_uuid=system_uuid,
            )
            return WorkflowStep(
                "settings", "ok" if result.status == "verified" else "error", result
            )
        observed = await get_lpar_settings(hmc.config, cli_system, creation.name)
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "settings", "error", f"Partition exists; settings unverified: {error}"
        )
    matches = all(
        getattr(observed, name) == getattr(creation.settings, name)
        for name, _ in creation.settings.pairs()
    )
    return WorkflowStep("settings", "ok" if matches else "error", observed)


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
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> str:
    """Authorize and delete a powered-off LPAR, returning its UUID."""
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
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


async def rename_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    new_name: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve, authorize, and rename one LPAR by whole-partition read-modify-write."""
    escape_xml(new_name)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
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
