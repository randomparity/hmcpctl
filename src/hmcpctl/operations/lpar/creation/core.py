"""Shared LPAR creation and ownership operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NamedTuple, NoReturn

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_io import SriovLogicalPortAssignment
from hmcpctl.operations.lpar.assignments import (
    LparPcieAssignments,
    prevalidate_lpar_pcie_assignments,
)
from hmcpctl.operations.lpar.memory.bounds import require_memory_within_system
from hmcpctl.operations.lpar.memory.creation import (
    CreationMemoryAdmission,
    _raise_creation_memory_failure,
)
from hmcpctl.operations.lpar.ownership import (
    stamp_created_lpar_ownership,
)
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.partition_creation import (
    _CreationFeatureCoordinator,
    _CreationFeatureInputs,
    _CreationObservations,
    _CreationObservationSnapshot,
)

from ....documents.lpar import (
    Keylock,
    LparReportingSettings,
    LparResources,
    LparSettings,
    PartitionType,
    build_lpar_document,
    reject_update_weight_on_create,
    validate_keylock,
    validate_partition_type,
)
from ....documents.lpar_affinity_group import (
    AffinityGroupID,
    validate_affinity_group_id,
)
from ....documents.lpar_creation_memory import LparCreationMemory
from ....documents.lpar_hardware_memory import hardware_memory_pairs
from ....documents.lpar_io import LparPhysicalIO
from ....documents.lpar_memory_layout import LparMemoryLayout
from ....documents.lpar_security_policy import LparSecurityPolicy
from ....documents.lpar_serial import VirtualSerialAdapter, validate_serial_input
from ....documents.lpar_start import LparStartSettings
from ....documents.lpar_vscsi import (
    VirtualScsiClientAdapter,
    validate_tagged_vscsi,
    validate_vscsi_input,
)
from ....documents.lpar_vtpm import LparVtpmSettings
from ....errors import HMCError, HMCTLSFingerprintError
from ....resource_identity import is_uuid, resolve_system_uuid
from ....ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    _CreationMemorySubmissionError,
    _LayoutCreateSubmissionError,
    apply_lpar_profile_via_cli,
    complete_create_resources,
    create_lpar_via_cli,
    get_lpar_reporting_settings,
    get_lpar_settings,
    resolve_system_cli_name,
    validate_caller_token,
)
from ....ssh.lpar_memory_layout import admit_memory_layout
from ....ssh.profiles import get_lpar_proc_compat
from ....ssh.transport import HMCCLIError
from ....xmlutil import leaf_text
from ..configuration import configure_lpar_settings
from ..linux_roles import admit_role_creation, roles_requested, with_role_evidence
from .physical_io import admit_creation_io, created_io_step
from .start import StartAdmission, recheck_start_controllers

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
    memory_layout: LparMemoryLayout = field(default_factory=LparMemoryLayout)
    virtual_serial_adapters: tuple[VirtualSerialAdapter, ...] = ()
    creation_memory: LparCreationMemory = field(default_factory=LparCreationMemory)
    affinity_group_id: AffinityGroupID | None = None
    start_settings: LparStartSettings = field(default_factory=LparStartSettings)
    virtual_scsi_adapters: tuple[VirtualScsiClientAdapter, ...] = ()


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
    memory_layout_step: WorkflowStep | None = None
    serial_step: WorkflowStep | None = None
    creation_memory_step: WorkflowStep | None = None
    affinity_group_step: WorkflowStep | None = None
    vscsi_step: WorkflowStep | None = None


_CreationCompletion = tuple[LparCreationResult, _CreationObservations]


def _compose_creation_result(
    result: LparCreationResult, observations: _CreationObservations
) -> LparCreationResult:
    return replace(result, **observations.public_steps())


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


_CONTROLLER_CREATE_CONFIGURED = (
    "native controller creation set the current configuration, so no apply was needed"
)
_CONTROLLER_AFFINITY_PROFILELESS = (
    "Native controller creation has no named profile to apply"
)


class _CreationSubmission(NamedTuple):
    system_name: str | None
    apply_step: WorkflowStep | None
    partition: dict[str, Any] | None
    readback_error: HMCError | None
    profile_already_configured: bool


async def _create_via_cli(
    hmc: HMCClient,
    system_uuid: str,
    system_name_or_uuid: str,
    creation: LparCreation,
    *,
    defer_apply: bool,
    native_readback: bool,
    controller_mode: bool = False,
    memory_admission: CreationMemoryAdmission | None = None,
    start_admission: StartAdmission | None = None,
) -> _CreationSubmission:
    """Create with ``mksyscfg``, apply its profile, and read the partition back.

    Retains the apply outcome and whether creation itself configured the profile.
    """
    if creation.keylock is not None:
        raise ValueError(
            "CLI creation cannot represent keylock; supply complete resources on an HMC accepting REST creation. Nothing was created."
        )
    try:
        system_name = await resolve_system_cli_name(hmc.config, system_uuid)
    except HMCCLIError:
        if creation.memory_layout.pairs() or creation.creation_memory.pairs():
            raise
        system_name = system_name_or_uuid
    if creation.memory_layout.pairs():
        await admit_memory_layout(hmc.config, system_name, creation.memory_layout)
    native_options: dict[str, Any] = {}
    if controller_mode:
        native_options["profile_name"] = None
    if creation.memory_layout.pairs():
        native_options["memory_layout"] = creation.memory_layout
    if creation.virtual_serial_adapters:
        native_options["virtual_serial_adapters"] = creation.virtual_serial_adapters
    if creation.virtual_scsi_adapters:
        native_options["virtual_scsi_adapters"] = creation.virtual_scsi_adapters
    if creation.creation_memory.pairs():
        native_options["creation_memory"] = creation.creation_memory
    if start_admission is not None:
        await recheck_start_controllers(hmc, start_admission)
        native_options["start_settings"] = start_admission.settings
    try:
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
            affinity_group_id=creation.affinity_group_id,
            **native_options,
        )
    except _CreationMemorySubmissionError as error:
        assert memory_admission is not None
        await _raise_creation_memory_failure(hmc, memory_admission, error)
    except _LayoutCreateSubmissionError as error:
        if not creation.virtual_serial_adapters:
            await _raise_layout_create_failure(hmc, system_uuid, creation, error)
    except Exception:
        if start_admission is not None:
            raise HMCError(
                "Start creation outcome unknown; partition exists or may exist. Inspect contained identity before retrying; no rollback performed"
            ) from None
        if not creation.virtual_serial_adapters:
            raise
    else:
        apply_step = (
            await _apply_created_profile(hmc, system_name, creation)
            if creation.apply_profile is not None and not defer_apply
            else None
        )
        profile_already_configured = controller_mode and creation.apply_profile is False
        if profile_already_configured:
            apply_step = WorkflowStep(
                "apply_profile",
                "skipped",
                _CONTROLLER_AFFINITY_PROFILELESS
                if creation.affinity_group_id is not None
                else _CONTROLLER_CREATE_CONFIGURED,
            )
        try:
            created_lpar = await hmc.find_partition_by_name(
                creation.name, system_uuid=system_uuid
            )
        except HMCError as exc:
            return _CreationSubmission(
                system_name, apply_step, None, exc, profile_already_configured
            )
        except Exception:
            if not native_readback:
                raise
            # Native creation succeeded, but the identity lookup is unavailable.
            # Do not retain provider details in the partial result or exception chain.
            return _CreationSubmission(
                system_name, apply_step, None, None, profile_already_configured
            )
        return _CreationSubmission(
            system_name, apply_step, created_lpar, None, profile_already_configured
        )

    # Raise outside the handler to avoid retaining private provider context.
    raise HMCError(
        "Serial creation outcome unknown; partition exists or may exist. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed"
    )


def validate_lpar_creation(creation: LparCreation) -> None:
    """Validate creation admission without opening a client or reading HMC state."""
    reject_update_weight_on_create(creation.resources)
    if not isinstance(creation.physical_io, LparPhysicalIO):
        raise TypeError("physical_io must be LparPhysicalIO")
    if not isinstance(creation.start_settings, LparStartSettings):
        raise TypeError("start_settings must be LparStartSettings")
    creation.start_settings.validate_creation(creation.partition_type)
    start_requested = creation.start_settings.requested()
    if start_requested and creation.keylock is not None:
        raise ValueError("Start settings cannot be combined with native keylock")
    if not isinstance(creation.creation_memory, LparCreationMemory):
        raise TypeError("creation_memory must be LparCreationMemory")
    memory_requested = bool(creation.creation_memory.pairs())
    shared_memory = creation.creation_memory.mem_mode == "shared"
    if memory_requested and creation.keylock is not None:
        raise ValueError("Creation memory cannot be combined with native keylock")
    if shared_memory:
        completed = complete_create_resources(creation.resources)
        if completed is None or completed.dedicated is not False:
            raise ValueError("Shared memory requires known shared processor resources")
        if creation.physical_io.io_slots or creation.creation_sriov:
            raise ValueError("Shared memory cannot use direct physical I/O or SR-IOV")
    validate_serial_input(creation.virtual_serial_adapters, creation.max_virtual_slots)
    serial_requested = bool(creation.virtual_serial_adapters)
    if serial_requested and creation.keylock is not None:
        raise ValueError(
            "Serial adapters cannot be combined with keylock in native creation"
        )
    validate_vscsi_input(
        creation.virtual_scsi_adapters,
        creation.max_virtual_slots,
        creation.virtual_serial_adapters,
    )
    validate_tagged_vscsi(creation.physical_io, creation.virtual_scsi_adapters)
    if creation.virtual_scsi_adapters and creation.keylock is not None:
        raise ValueError(
            "vSCSI adapters cannot be combined with keylock in native creation"
        )
    if not isinstance(creation.memory_layout, LparMemoryLayout):
        raise TypeError("memory_layout must be LparMemoryLayout")
    creation.memory_layout.validate_creation(creation.partition_type)
    layout_requested = bool(creation.memory_layout.pairs())
    if layout_requested and creation.keylock is not None:
        raise ValueError(
            "Memory layout cannot be combined with keylock in native creation"
        )
    affinity_requested = (
        validate_affinity_group_id(creation.affinity_group_id) is not None
    )
    if affinity_requested and creation.keylock is not None:
        raise ValueError(
            "Affinity group cannot be combined with explicit keylock in native creation"
        )
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
        # Reject before effects and outside the stamp's best-effort catch:
        # a malformed token must never discard ownership (ADR 0064).
        validate_caller_token(creation.caller_token)
    if not isinstance(creation.security_policy, LparSecurityPolicy):
        raise TypeError("security_policy must be LparSecurityPolicy")
    creation.security_policy.validate_creation(creation.partition_type)
    if not isinstance(creation.vtpm_settings, LparVtpmSettings):
        raise TypeError("vtpm_settings must be LparVtpmSettings")
    creation.vtpm_settings.validate_creation(creation.partition_type)
    if creation.vtpm_settings.pairs() and creation.keylock is not None:
        raise ValueError(
            "vTPM settings cannot be combined with explicit keylock: native creation cannot represent keylock. Nothing was created"
        )
    if not isinstance(creation.settings, LparSettings):
        raise TypeError("settings must be LparSettings")
    validate_partition_type(creation.partition_type)
    if not isinstance(creation.reporting_settings, LparReportingSettings):
        raise TypeError("reporting_settings must be LparReportingSettings")
    creation.reporting_settings.validate_context("create", creation.partition_type)
    if creation.reporting_settings.pairs() and creation.keylock is not None:
        raise ValueError(
            "Reporting settings cannot be combined with explicit keylock: CLI creation cannot represent keylock. Nothing was created."
        )
    validate_keylock(creation.keylock)


_SYSTEM_KEY_REFUSAL_CODE = "HSCL0DCE"
_SYSTEM_KEY_REFUSAL = (
    "HMC refused creation combining simplified remote restart with a nonzero "
    "keystore (HSCL0DCE): the managed system's trusted system key status is not "
    "User Key. A scoped lookup observed no partition; nothing was created. Select "
    "only one of these features, or set a user-defined system key first."
)
_REJECTED_OUTCOME = "rejected; scoped lookup observed no partition"


def _srr_keystore_requested(creation: LparCreation) -> bool:
    """Whether creation asks for simplified remote restart with a nonzero keystore.

    The HMC admits this pair only when the trusted system key is a user key, and
    signals otherwise with HSCL0DCE (ADR 0377); hmcpctl never reads the key.
    """
    keystore = creation.security_policy.keystore_kbytes
    return (
        creation.settings.simplified_remote_restart_capable is True
        and keystore is not None
        and keystore > 0
    )


def _creation_feature_inputs(creation: LparCreation) -> _CreationFeatureInputs:
    return _CreationFeatureInputs(
        creation.name,
        creation.partition_type,
        creation.resources,
        partition_id=creation.partition_id,
        max_virtual_slots=creation.max_virtual_slots,
        virtual_serial_adapters=creation.virtual_serial_adapters,
        virtual_scsi_adapters=creation.virtual_scsi_adapters,
        creation_memory=creation.creation_memory,
        start_settings=creation.start_settings,
        memory_layout=creation.memory_layout,
        vtpm_settings=creation.vtpm_settings,
        security_policy=creation.security_policy,
        hardware_mem_encryption=creation.hardware_mem_encryption,
        hardware_mem_expansion=creation.hardware_mem_expansion,
        affinity_group_id=creation.affinity_group_id,
        settings=creation.settings,
        reporting_settings=creation.reporting_settings,
        physical_io=creation.physical_io,
        apply_profile=creation.apply_profile,
    )


async def create_and_stamp_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    *,
    preflight_assignments: LparPcieAssignments | None = None,
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
    result, observations = await _create_and_stamp_lpar(
        hmc,
        system_name_or_uuid,
        creation,
        preflight_assignments=preflight_assignments,
    )
    return _compose_creation_result(result, observations)


async def _create_and_stamp_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    *,
    preflight_assignments: LparPcieAssignments | None = None,
) -> _CreationCompletion:
    validate_lpar_creation(creation)
    inputs = _creation_feature_inputs(creation)
    features = await _admit_lpar_creation(
        hmc, system_name_or_uuid, creation, inputs, preflight_assignments
    )
    submission = await _submit_lpar_creation(
        hmc, system_name_or_uuid, creation, features
    )
    return await _complete_lpar_creation(
        hmc,
        system_name_or_uuid,
        creation,
        features,
        submission,
    )


async def _admit_lpar_creation(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    inputs: _CreationFeatureInputs,
    preflight_assignments: LparPcieAssignments | None,
) -> _CreationFeatureCoordinator:
    """Validate local assignments before resolving or admitting remote resources."""
    role_requested = roles_requested(creation.settings)
    if preflight_assignments is not None:
        if not isinstance(preflight_assignments, LparPcieAssignments):
            raise TypeError("preflight_assignments must be LparPcieAssignments")
        if not (
            (role_requested or inputs.profile_features_requested)
            and (preflight_assignments.dedicated or preflight_assignments.sriov)
        ):
            raise ValueError(
                "preflight_assignments requires role, memory layout, affinity, serial or creation memory selection "
                "and dedicated or SR-IOV profile assignments"
            )
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    features = _CreationFeatureCoordinator(
        hmc, system_uuid, system_name_or_uuid, inputs
    )
    # Scoped: the HMC-wide search misses partitions on a system it cannot
    # read (#1309), and the scoped feed refuses a non-operating system.
    existing = await hmc.find_partition_by_name(creation.name, system_uuid=system_uuid)
    if existing:
        raise ValueError(
            f"An LPAR named {creation.name!r} already exists "
            f"(UUID {existing.get('UUID')!r}). Choose a different name "
            "or delete the existing partition first."
        )
    role_caps = await features.discover_mode(
        creation.settings if role_requested else None
    )
    if role_requested:
        assert role_caps is not None
        admit_role_creation(creation, role_caps)
    controller_mode = features.controller_mode
    if preflight_assignments is not None:
        if controller_mode:
            raise ValueError(
                "Dedicated and SR-IOV profile assignments are unavailable in controller mode"
            )
        await prevalidate_lpar_pcie_assignments(
            hmc, system_name_or_uuid, preflight_assignments
        )
    if features.inputs.profile_features_requested and controller_mode:
        if creation.apply_profile is True:
            raise ValueError(
                "Controller affinity creation requires apply_profile=False"
                if features.inputs.affinity_requested
                else "Controller creation cannot apply a named profile"
            )
        if creation.physical_io.requested or creation.creation_sriov:
            raise ValueError(
                "Initial-profile I/O is unavailable in controller mode"
                if features.inputs.affinity_requested
                else "Controller creation cannot use initial-profile I/O or SR-IOV"
            )
    await features.admit_host_features()
    await admit_creation_io(hmc, system_uuid, creation)
    if features.inputs.creation_memory.mem_mode != "shared":
        await require_memory_within_system(hmc, system_uuid, creation.resources)
    await features.admit_partition_features()
    return features


async def _submit_lpar_creation(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
) -> _CreationSubmission:
    """Select native creation or REST with only the established refusal fallback."""
    system_uuid = features.system_uuid
    native_readback = features.inputs.native_features_requested
    defer_apply = (
        creation.physical_io.requested
        or features.inputs.policy_requested
        or native_readback
        or roles_requested(creation.settings)
    )
    system_name: str | None = (
        features.system if features.inputs.memory_requested else None
    )
    apply_step: WorkflowStep | None = None
    readback_error: HMCError | None = None
    resources = complete_create_resources(creation.resources)
    if (
        resources is None
        or features.inputs.policy_requested
        or native_readback
        or creation.reporting_settings.pairs()
        or features.inputs.vtpm_requested
        or creation.physical_io.requested
        or (creation.settings.pairs() and creation.keylock is None)
    ):
        # Only mksyscfg's all_resources=1 expresses a create with no resource values.
        return await _submit_native_creation(
            hmc,
            system_name_or_uuid,
            creation,
            features,
            defer_apply=defer_apply,
            native_readback=native_readback,
        )
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
        return await _create_via_cli(
            hmc,
            system_uuid,
            system_name_or_uuid,
            creation,
            defer_apply=defer_apply,
            native_readback=native_readback,
        )
    if creation.apply_profile is True:
        apply_step = WorkflowStep("apply_profile", "skipped", _REST_CREATE_CONFIGURED)
    return _CreationSubmission(
        system_name, apply_step, created_lpar, readback_error, True
    )


async def _submit_native_creation(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    *,
    defer_apply: bool,
    native_readback: bool,
) -> _CreationSubmission:
    """Preserve feature-specific ambiguous-submission diagnostics and causes."""
    role_requested = roles_requested(creation.settings)
    diagnostic = (
        "Native {feature} creation outcome is unknown; the partition may exist. "
        "Inspect its {inspection} before retrying; no rollback performed."
    )
    key_refused = False
    try:
        return await _create_via_cli(
            hmc,
            features.system_uuid,
            system_name_or_uuid,
            creation,
            defer_apply=defer_apply,
            native_readback=native_readback,
            controller_mode=features.controller_mode,
            memory_admission=features.memory_admission,
            start_admission=features.start_admission,
        )
    except HMCError as error:
        key_refused = (
            isinstance(error, HMCCLIError)
            and error.remote_refusal_code == _SYSTEM_KEY_REFUSAL_CODE
            and _srr_keystore_requested(creation)
        )
        if key_refused:
            pass
        elif (
            features.inputs.serial_requested
            or features.inputs.vscsi_requested
            or features.inputs.layout_requested
            or features.inputs.memory_requested
            or features.inputs.start_requested
        ):
            raise
        elif features.inputs.hardware_requested or features.inputs.affinity_requested:
            pass
        elif (
            not creation.physical_io.requested
            and not features.inputs.policy_requested
            and not role_requested
        ):
            raise
        else:
            if role_requested:
                feature = "role"
            elif features.inputs.policy_requested:
                feature = "policy"
            else:
                feature = "I/O"
            inspection = "scoped identity"
            if not role_requested:
                inspection += " and profile"
            raise HMCError(
                diagnostic.format(feature=feature, inspection=inspection)
            ) from (None if role_requested else error)
    # Keep these errors outside the handler: neither diagnostic retains the
    # provider cause or its context.
    if key_refused:
        await _raise_system_key_refusal(hmc, features.system_uuid, creation)
    if features.inputs.affinity_requested:
        feature = "affinity group"
    else:
        feature = "hardware memory"
    inspection = "scoped identity"
    if not features.controller_mode:
        inspection += " and profile"
    raise HMCError(diagnostic.format(feature=feature, inspection=inspection))


async def _complete_lpar_creation(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    submission: _CreationSubmission,
) -> _CreationCompletion:
    """Confirm, stamp, observe and apply, retaining facts at the TLS cutoff."""
    (
        system_name,
        apply_step,
        created_lpar,
        readback_error,
        profile_already_configured,
    ) = submission
    apply_warnings = _unapplied_profile_warnings(
        creation.name, apply_step, profile_already_configured=profile_already_configured
    )
    role_requested = roles_requested(creation.settings)
    verify_before_apply = (
        features.inputs.policy_requested
        or features.inputs.native_features_requested
        or role_requested
    )
    result = LparCreationResult(
        True,
        created_lpar,
        None,
        apply_warnings,
    )
    features.retain("apply_step", apply_step)
    try:
        created_lpar = await features.confirm_identity(
            created_lpar, readback_error=readback_error
        )
        result = replace(result, lpar=created_lpar)
        if created_lpar is None:
            return await _missing_created_identity(
                creation, features, result, readback_error, verify_before_apply
            )
        if verify_before_apply and (
            not isinstance(created_lpar, dict)
            or not isinstance(created_lpar.get("UUID"), str)
            or not is_uuid(created_lpar["UUID"])
            or not isinstance(created_lpar.get("Resource"), dict)
            or created_lpar["Resource"].get("PartitionName") != creation.name
        ):
            return await _unconfirmed_created_identity(creation, features, created_lpar)
        ownership_stamped, warnings = await _stamp_lpar_creation(
            hmc,
            system_name or system_name_or_uuid,
            creation,
            features,
            created_lpar,
            apply_warnings,
        )
        result = replace(
            result,
            ownership_stamped=ownership_stamped,
            warnings=(*apply_warnings, *warnings),
        )
        await features.observe_before_followup(system_name)
        await _observe_created_io(
            hmc, creation, features, created_lpar, ownership_stamped
        )
        # Roles must be verified before a deferred apply. Plain legacy settings
        # retain their apply-first behavior; both positions use the same observer.
        await _observe_creation_settings(
            hmc,
            system_name,
            creation,
            features,
            created_lpar,
            enabled=verify_before_apply,
        )
        if verify_before_apply:
            await features.observe_vtpm(system_name)
        await features.observe_security_policy()
        apply_step, profile_already_configured = await _apply_verified_creation(
            hmc,
            system_name or system_name_or_uuid,
            creation,
            features,
            ownership_stamped,
            verify_before_apply=verify_before_apply,
            profile_already_configured=profile_already_configured,
        )
        await features.observe_after_apply(apply_step)
        if creation.physical_io.requested or verify_before_apply:
            apply_warnings = _unapplied_profile_warnings(
                creation.name,
                apply_step,
                profile_already_configured=profile_already_configured,
            )
        if not verify_before_apply:
            await _observe_creation_settings(
                hmc, system_name, creation, features, created_lpar
            )
            await features.observe_vtpm(system_name)
        result = replace(
            result,
            warnings=(*apply_warnings, *warnings),
        )
        return result, _sanitize_creation_steps(
            creation,
            features,
            features.snapshot().observations,
        )
    except HMCTLSFingerprintError:
        observations = features.snapshot()
        return _tls_aborted_creation(
            replace(result, lpar=features.entry),
            observations,
        )


async def _missing_created_identity(
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    result: LparCreationResult,
    readback_error: HMCError | None,
    verify_before_apply: bool,
) -> _CreationCompletion:
    """Preserve required-stamp failures and best-effort absent-identity evidence."""
    apply_warnings = result.warnings
    apply_note = f" {'; '.join(apply_warnings)}" if apply_warnings else ""
    if verify_before_apply and creation.stamp_policy == "required":
        feature_label = "Policy"
        for requested, label in (
            (features.inputs.memory_requested, "Creation memory"),
            (features.inputs.affinity_requested, "Affinity group"),
            (features.inputs.serial_requested, "Serial"),
            (features.inputs.vscsi_requested, "vSCSI"),
            (features.inputs.layout_requested, "Memory layout"),
            (features.inputs.hardware_requested, "Hardware memory"),
            (roles_requested(creation.settings), "Role"),
        ):
            if requested:
                feature_label = label
                break
        raise HMCError(
            f"{feature_label} creation may have succeeded, but created identity readback is "
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
    if verify_before_apply:
        skipped = "created identity readback unavailable; inspect the contained partition before retrying"
    elif readback_error is None:
        skipped = "create returned no LPAR body"
    else:
        skipped = f"read-back after mksyscfg failed: {readback_error}"
    await features.observe_unavailable_identity(missing=True)
    features.retain(
        "physical_io_step",
        WorkflowStep(
            "physical_io",
            "error",
            "Created partition identity unavailable; I/O unverified",
        )
        if creation.physical_io.requested
        else None,
    )
    features.retain(
        "settings_step",
        WorkflowStep(
            "settings",
            "error",
            "Created partition readback unavailable; settings unverified",
        )
        if creation.settings.pairs() or features.inputs.start_requested
        else None,
    )
    features.retain(
        "reporting_step",
        WorkflowStep(
            "reporting_settings",
            "error",
            "Created partition readback unavailable; reporting settings unverified",
        )
        if creation.reporting_settings.pairs()
        else None,
    )
    result = LparCreationResult(
        resource_created=True,
        lpar=None,
        ownership_stamped=None,
        warnings=(
            *apply_warnings,
            f"ownership stamp skipped for LPAR {creation.name!r}: {skipped}",
        ),
    )
    return result, features.snapshot().observations


async def _unconfirmed_created_identity(
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    created_lpar: dict[str, Any],
) -> _CreationCompletion:
    """Retain the distinct unconfirmed-identity result and observation order."""
    if creation.stamp_policy == "required":
        raise HMCError(
            "stamp_policy='required': creation may have occurred, but partition "
            "identity and ownership are unconfirmed. Inspect the contained "
            "partition before retrying; no rollback performed."
        )
    await features.observe_unavailable_identity(missing=False)
    role_requested = roles_requested(creation.settings)
    features.retain("apply_step", None)
    features.retain(
        "settings_step",
        WorkflowStep(
            "settings",
            "error",
            "Created partition identity unconfirmed; roles unverified",
        )
        if role_requested or features.inputs.start_requested
        else None,
    )
    result = LparCreationResult(
        True,
        None
        if role_requested
        or features.inputs.layout_requested
        or features.inputs.memory_requested
        or features.inputs.start_requested
        or features.inputs.affinity_requested
        else created_lpar,
        None,
        (),
    )
    return result, features.snapshot().observations


async def _stamp_lpar_creation(
    hmc: HMCClient,
    system_name: str,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    created_lpar: dict[str, Any],
    apply_warnings: tuple[str, ...],
) -> tuple[bool | None, list[str]]:
    ownership_stamped, warnings = await stamp_created_lpar_ownership(
        hmc,
        features.system_uuid,
        system_name,
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
    return ownership_stamped, warnings


async def _observe_created_io(
    hmc: HMCClient,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    created_lpar: dict[str, Any],
    ownership_stamped: bool | None,
) -> None:
    io_step = await features.observe(
        "physical_io_step",
        created_io_step(hmc, features.system_uuid, creation, created_lpar),
    )
    if io_step is not None and ownership_stamped is not True:
        await features.observe(
            "physical_io_step",
            WorkflowStep(
                "physical_io",
                "error",
                "Partition exists but ownership stamp unconfirmed; I/O workflow stopped before apply",
            ),
        )


async def _observe_creation_settings(
    hmc: HMCClient,
    system_name: str | None,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    created_lpar: dict[str, Any],
    *,
    enabled: bool = True,
) -> None:
    await features.observe(
        "settings_step",
        _created_settings_step(
            hmc,
            features.system_uuid,
            system_name,
            creation,
            created_lpar,
            controller_mode=features.controller_mode,
            features=features,
        )
        if enabled
        else None,
    )
    await features.observe(
        "reporting_step",
        _created_reporting_step(hmc, system_name, creation) if enabled else None,
    )


async def _apply_verified_creation(
    hmc: HMCClient,
    system_name: str,
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    ownership_stamped: bool | None,
    *,
    verify_before_apply: bool,
    profile_already_configured: bool,
) -> tuple[WorkflowStep | None, bool]:
    observations = features.snapshot().observations
    if verify_before_apply and creation.apply_profile is not None:
        verified = ownership_stamped is True and observations.verified
        step = await features.observe(
            "apply_step",
            _apply_created_profile(hmc, system_name, creation)
            if verified and not features.controller_mode
            else WorkflowStep(
                "apply_profile",
                "skipped",
                (
                    _CONTROLLER_AFFINITY_PROFILELESS
                    if features.inputs.affinity_requested
                    else _CONTROLLER_CREATE_CONFIGURED
                )
                if features.controller_mode
                else "Creation verification incomplete; profile not applied",
            ),
        )
        return step, features.controller_mode
    if creation.physical_io.requested and creation.apply_profile is not None:
        io_step = observations.physical_io_step.step
        step = await features.observe(
            "apply_step",
            _apply_created_profile(hmc, system_name, creation)
            if io_step is not None and io_step.status == "ok"
            else WorkflowStep(
                "apply_profile",
                "skipped",
                "I/O readback unverified; profile not applied",
            ),
        )
        return step, False
    return observations.apply_step.step, profile_already_configured


def _sanitize_creation_steps(
    creation: LparCreation,
    features: _CreationFeatureCoordinator,
    observations: _CreationObservations,
) -> _CreationObservations:
    if not (
        roles_requested(creation.settings) or features.inputs.native_features_requested
    ):
        return observations
    safe_observations = {}
    step_names = (
        "apply_step",
        "physical_io_step",
        "reporting_step",
        "vtpm_step",
    )
    if features.inputs.native_features_requested:
        step_names += (
            "settings_step",
            "security_policy_step",
            "hardware_memory_step",
        )
    for name in step_names:
        observation = getattr(observations, name)
        step = observation.step
        if step is not None and step.status == "error" and isinstance(step.result, str):
            safe_observations[name] = replace(
                observation,
                step=replace(
                    step,
                    result="Partition exists or may exist; creation step unverified. Inspect before retrying; no rollback performed.",
                ),
            )
    return replace(observations, **safe_observations)


_TLS_CREATION_ABORTED = (
    "TLS certificate identity verification failed after creation. The partition exists or "
    "may exist; remaining work is unverified. Inspect before retrying; no further remote "
    "calls or rollback were attempted."
)


def _tls_aborted_creation(
    result: LparCreationResult, snapshot: _CreationObservationSnapshot
) -> _CreationCompletion:
    observations = snapshot.observations.interrupted(
        snapshot.active_step, _TLS_CREATION_ABORTED
    )
    return (
        replace(result, warnings=(*result.warnings, _TLS_CREATION_ABORTED)),
        observations,
    )


def _creation_steps_verified(
    ownership_stamped: bool | None, observations: _CreationObservations
) -> bool:
    return ownership_stamped is True and observations.verified


def creation_followup(
    creation: LparCreation, created: LparCreationResult
) -> tuple[list[WorkflowStep], bool]:
    """Return ordered creation evidence and whether PCIe assignments may proceed."""
    observations = _CreationObservations.selected(_creation_feature_inputs(creation))
    for name, _ in observations.public_items():
        observations = observations.record(name, getattr(created, name))
    return _creation_followup(creation, created, observations)


def _creation_followup(
    creation: LparCreation,
    created: LparCreationResult,
    observations: _CreationObservations,
) -> tuple[list[WorkflowStep], bool]:
    memory_requested = bool(creation.creation_memory.pairs())
    start_requested = creation.start_settings.requested()
    affinity_requested = creation.affinity_group_id is not None
    defer_apply = bool(
        creation.security_policy.pairs()
        or hardware_memory_pairs(
            creation.hardware_mem_encryption, creation.hardware_mem_expansion
        )
        or creation.memory_layout.pairs()
        or creation.virtual_serial_adapters
        or creation.virtual_scsi_adapters
        or memory_requested
        or start_requested
        or affinity_requested
    )
    # Role-only creation keeps its established apply-first presentation order.
    ordered = [observations.physical_io_step.step]
    if not defer_apply:
        ordered.append(observations.apply_step.step)
    ordered.extend(
        (
            observations.settings_step.step,
            observations.reporting_step.step,
            observations.vtpm_step.step,
            observations.security_policy_step.step,
            observations.affinity_group_step.step,
            observations.hardware_memory_step.step,
            observations.memory_layout_step.step,
            observations.serial_step.step,
            observations.vscsi_step.step,
            observations.creation_memory_step.step,
        )
    )
    if defer_apply:
        ordered.append(observations.apply_step.step)
    steps = [WorkflowStep("create", "ok", created.lpar)]
    steps.extend(step for step in ordered if step is not None)
    verification_failed = defer_apply and not _creation_steps_verified(
        created.ownership_stamped, observations
    )
    vtpm_failed = bool(creation.vtpm_settings.pairs()) and (
        observations.vtpm_step.step is None
        or observations.vtpm_step.step.status != "ok"
    )
    reporting_failed = (
        observations.reporting_step.step is not None
        and observations.reporting_step.step.status == "error"
    )
    io_failed = (
        observations.physical_io_step.step is not None
        and observations.physical_io_step.step.status != "ok"
    )
    settings_failed = (
        observations.settings_step.step is not None
        and observations.settings_step.step.status == "error"
    )
    apply_failed = (
        observations.apply_step.step is not None
        and observations.apply_step.step.status == "error"
    ) or (
        (
            roles_requested(creation.settings)
            or memory_requested
            or start_requested
            or affinity_requested
        )
        and creation.apply_profile is True
        and (
            observations.apply_step.step is None
            or observations.apply_step.step.status != "ok"
        )
    )
    allowed = created.lpar is not None and not (
        apply_failed
        or settings_failed
        or reporting_failed
        or io_failed
        or vtpm_failed
        or verification_failed
    )
    return steps, allowed


async def _scoped_create_outcome(
    hmc: HMCClient,
    system_uuid: str,
    creation: LparCreation,
    *,
    refused: bool,
    feature: str,
) -> str:
    """Classify one failed native request by one scoped lookup, without replay."""
    outcome = "unknown"
    try:
        found = await hmc.find_partition_by_name(creation.name, system_uuid=system_uuid)
        if found is None and refused:
            outcome = _REJECTED_OUTCOME
        elif (
            isinstance(found, dict)
            and isinstance(found.get("UUID"), str)
            and is_uuid(found["UUID"])
            and isinstance(found.get("Resource"), dict)
            and leaf_text(found["Resource"].get("PartitionName")) == creation.name
        ):
            outcome = f"partial; partition exists (UUID {found['UUID']})"
    except HMCTLSFingerprintError as identity_error:
        identity_error.add_note(
            f"{feature} creation submission may have taken effect. "
            "Inspect scoped identity before retrying; no replay, ownership stamp "
            "or rollback performed."
        )
        raise
    except Exception:  # noqa: BLE001 — ordinary post-submission evidence failures mean unknown.
        outcome = "unknown; scoped identity readback unavailable"
    return outcome


async def _raise_layout_create_failure(
    hmc: HMCClient, system_uuid: str, creation: LparCreation, error: HMCError
) -> NoReturn:
    """Reconcile one failed native request without replay, stamping or deletion."""
    outcome = await _scoped_create_outcome(
        hmc,
        system_uuid,
        creation,
        refused=isinstance(error, HMCCLIError)
        and error.remote_refusal_code is not None,
        feature="Memory layout",
    )
    raise HMCError(
        f"Memory layout creation outcome {outcome}. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed."
    ) from None


async def _raise_system_key_refusal(
    hmc: HMCClient, system_uuid: str, creation: LparCreation
) -> NoReturn:
    """Report the HMC's HSCL0DCE refusal as contained only when lookup finds nothing."""
    outcome = await _scoped_create_outcome(
        hmc,
        system_uuid,
        creation,
        refused=True,
        feature="Simplified remote restart and keystore",
    )
    if outcome == _REJECTED_OUTCOME:
        raise HMCCLIError(
            _SYSTEM_KEY_REFUSAL, remote_refusal_code=_SYSTEM_KEY_REFUSAL_CODE
        )
    raise HMCError(
        f"Simplified remote restart and keystore creation outcome {outcome}. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed."
    )


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
    except HMCTLSFingerprintError:
        raise
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


async def _created_legacy_settings_step(
    hmc: HMCClient,
    system_uuid: str,
    cli_system: str | None,
    creation: LparCreation,
    created: dict[str, Any],
    *,
    controller_mode: bool = False,
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
        if roles_requested(creation.settings):
            uuid = created.get("UUID")
            if not isinstance(uuid, str) or not is_uuid(uuid):
                raise ValueError("Created identity unavailable")
            entry = await hmc.get_logical_partition(uuid)
            resource = entry.get("Resource") if isinstance(entry, dict) else None
            if (
                not isinstance(entry, dict)
                or not isinstance(resource, dict)
                or not isinstance(entry.get("UUID"), str)
                or entry["UUID"].lower() != uuid.lower()
                or leaf_text(resource.get("PartitionName")) != creation.name
            ):
                raise ValueError("Created identity changed or unavailable")
            observed = with_role_evidence(observed, resource, creation.settings)
            if creation.settings.kvm_capable is True:
                mode = (
                    leaf_text(resource.get("PendingProcessorCompatibilityMode"))
                    if controller_mode
                    else (
                        await get_lpar_proc_compat(
                            hmc.config, cli_system, creation.name, DEFAULT_PROFILE_NAME
                        )
                    ).get("profile_mode")
                )
                if mode != "POWER10":
                    raise ValueError("Created POWER10 companion unverified")
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "settings",
            "error",
            "Partition exists or may exist; Linux roles unverified. Inspect before retrying"
            if roles_requested(creation.settings)
            else f"Partition exists; settings unverified: {error}",
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
    name: str, apply_step: WorkflowStep | None, *, profile_already_configured: bool
) -> tuple[str, ...]:
    """Say that a skipped or failed apply left the partition unconfigured."""
    if apply_step is None or apply_step.status == "ok" or profile_already_configured:
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


async def _created_settings_step(
    hmc: HMCClient,
    system_uuid: str,
    cli_system: str | None,
    creation: LparCreation,
    created: dict[str, Any],
    *,
    controller_mode: bool = False,
    features: _CreationFeatureCoordinator,
) -> WorkflowStep | None:
    legacy = await _created_legacy_settings_step(
        hmc, system_uuid, cli_system, creation, created, controller_mode=controller_mode
    )
    start = await features.observe_start(created)
    if start is None:
        return legacy
    if legacy is None:
        return start
    return WorkflowStep(
        "settings",
        "ok" if legacy.status == start.status == "ok" else "error",
        {
            "settings": legacy.result,
            **start.result,
        },
    )
