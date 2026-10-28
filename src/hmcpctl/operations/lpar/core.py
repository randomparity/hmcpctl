"""Shared LPAR creation and ownership operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
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
    reject_update_weight_on_create,
    validate_keylock,
    validate_partition_type,
)
from ...documents.lpar_affinity_group import AffinityGroupID, validate_affinity_group_id
from ...documents.lpar_creation_memory import LparCreationMemory
from ...documents.lpar_hardware_memory import hardware_memory_pairs
from ...documents.lpar_io import LparPhysicalIO
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_security_policy import LparSecurityPolicy
from ...documents.lpar_serial import VirtualSerialAdapter, validate_serial_input
from ...documents.lpar_start import LparStartSettings
from ...documents.lpar_vtpm import LparVtpmSettings
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_system_uuid,
)
from ...ssh.lpar import (
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
from ...ssh.lpar_memory_layout import admit_memory_layout, get_lpar_memory_layout
from ...ssh.lpar_vtpm import get_lpar_vtpm_state
from ...ssh.profiles import get_lpar_proc_compat
from ...ssh.transport import HMCCLIError
from ...xmlutil import escape_xml, leaf_text
from .affinity_group import created_affinity_group_step
from .assignments import (
    LparPcieAssignments,
    SriovLogicalPortAssignment,
    prevalidate_lpar_pcie_assignments,
)
from .configuration import configure_lpar_settings
from .creation_memory import (
    CreationMemoryAdmission,
    _raise_creation_memory_failure,
    admit_creation_memory,
    confirm_creation_memory_identity,
    creation_memory_step,
)
from .hardware_memory import admit_hardware_memory, created_hardware_memory_step
from .linux_roles import (
    admit_role_creation,
    read_creation_authority,
    read_role_capabilities,
    roles_requested,
    with_role_evidence,
)
from .physical_io import admit_creation_io, created_io_step
from .security_policy import (
    admit_security_policy,
    read_lpar_security_policy,
    read_security_policy_capabilities,
)
from .serial import admit_serial_creation, confirm_serial_created, serial_creation_step
from .start import (
    StartAdmission,
    admit_start_creation,
    confirm_start_created,
    created_start_step,
    recheck_start_controllers,
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
    memory_layout: LparMemoryLayout = field(default_factory=LparMemoryLayout)
    virtual_serial_adapters: tuple[VirtualSerialAdapter, ...] = ()
    creation_memory: LparCreationMemory = field(default_factory=LparCreationMemory)
    affinity_group_id: AffinityGroupID | None = None
    start_settings: LparStartSettings = field(default_factory=LparStartSettings)


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


@dataclass(frozen=True)
class MemoryLayoutVerification:
    """Requested creation values and an honest saved-configuration observation."""

    status: Literal["verified", "mismatch", "unavailable"]
    requested: LparMemoryLayout
    observed: dict[str, object]
    unavailable: tuple[str, ...]
    warnings: tuple[str, ...]


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


async def _create_via_cli(
    hmc: HMCClient,
    system_uuid: str,
    system_name_or_uuid: str,
    creation: LparCreation,
    *,
    controller_mode: bool = False,
    memory_admission: CreationMemoryAdmission | None = None,
    start_admission: StartAdmission | None = None,
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
    if creation.creation_memory.pairs():
        native_options["creation_memory"] = creation.creation_memory
    if start_admission is not None:
        await recheck_start_controllers(hmc, start_admission)
        native_options["start_settings"] = start_admission.settings
    serial_submission_failed = False
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
        if creation.virtual_serial_adapters:
            serial_submission_failed = True
        else:
            await _raise_layout_create_failure(hmc, system_uuid, creation, error)
    except Exception:
        if start_admission is not None:
            raise HMCError(
                "Start creation outcome unknown; partition exists or may exist. Inspect contained identity before retrying; no rollback performed"
            ) from None
        if not creation.virtual_serial_adapters:
            raise
        serial_submission_failed = True
    if serial_submission_failed:
        raise HMCError(
            "Serial creation outcome unknown; partition exists or may exist. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed"
        )
    apply_step = (
        await _apply_created_profile(hmc, system_name, creation)
        if creation.apply_profile is not None
        and not creation.physical_io.requested
        and not creation.security_policy.pairs()
        and not creation.memory_layout.pairs()
        and not creation.virtual_serial_adapters
        and not creation.creation_memory.pairs()
        and not creation.start_settings.requested()
        and creation.hardware_mem_encryption is None
        and creation.hardware_mem_expansion is None
        and creation.affinity_group_id is None
        and not roles_requested(creation.settings)
        else None
    )
    if controller_mode and creation.apply_profile is False:
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
        return system_name, apply_step, None, exc
    except Exception:
        if (
            creation.hardware_mem_encryption is None
            and creation.hardware_mem_expansion is None
            and not creation.memory_layout.pairs()
            and not creation.virtual_serial_adapters
            and not creation.creation_memory.pairs()
            and not creation.start_settings.requested()
            and creation.affinity_group_id is None
        ):
            raise
        # Native creation succeeded, but the identity lookup is unavailable.
        # Do not retain provider details in the partial result or exception chain.
        return system_name, apply_step, None, None
    return system_name, apply_step, created_lpar, None


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
    if not isinstance(creation.start_settings, LparStartSettings):
        raise TypeError("start_settings must be LparStartSettings")
    creation.start_settings.validate_creation(creation.partition_type)
    start_requested = creation.start_settings.requested()
    if start_requested and creation.keylock is not None:
        raise ValueError("Start settings cannot be combined with native keylock")
    reject_update_weight_on_create(creation.resources)
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
    role_requested = roles_requested(creation.settings)
    if preflight_assignments is not None:
        if not isinstance(preflight_assignments, LparPcieAssignments):
            raise TypeError("preflight_assignments must be LparPcieAssignments")
        if not (
            (
                role_requested
                or affinity_requested
                or layout_requested
                or serial_requested
                or memory_requested
                or start_requested
            )
            and (preflight_assignments.dedicated or preflight_assignments.sriov)
        ):
            raise ValueError(
                "preflight_assignments requires role, memory layout, affinity, serial or creation memory selection "
                "and dedicated or SR-IOV profile assignments"
            )
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
    controller_mode = False
    if role_requested:
        role_caps = await read_role_capabilities(
            hmc,
            system_uuid,
            need_kvm=creation.settings.kvm_capable is not None,
            need_management=creation.settings.powervm_mgmt_capable is not None,
        )
        admit_role_creation(creation, role_caps)
        controller_mode = role_caps.controller_mode
    elif affinity_requested or layout_requested:
        _, controller_mode = await read_creation_authority(
            hmc,
            system_uuid,
            need_management=False,
            context="Affinity group" if affinity_requested else "Memory layout",
        )
    elif serial_requested or memory_requested or start_requested:
        controller_mode = (
            await read_role_capabilities(
                hmc, system_uuid, need_kvm=False, need_management=False
            )
        ).controller_mode
    if preflight_assignments is not None:
        if controller_mode:
            raise ValueError(
                "Dedicated and SR-IOV profile assignments are unavailable in controller mode"
            )
        await prevalidate_lpar_pcie_assignments(
            hmc, system_name_or_uuid, preflight_assignments
        )
    if (
        affinity_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
    ) and controller_mode:
        if creation.apply_profile is True:
            raise ValueError(
                "Controller affinity creation requires apply_profile=False"
                if affinity_requested
                else "Controller creation cannot apply a named profile"
            )
        if creation.physical_io.requested or creation.creation_sriov:
            raise ValueError(
                "Initial-profile I/O is unavailable in controller mode"
                if affinity_requested
                else "Controller creation cannot use initial-profile I/O or SR-IOV"
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
    if not shared_memory:
        await require_memory_within_system(hmc, system_uuid, creation.resources)
    serial_admission = (
        await admit_serial_creation(
            hmc,
            system_uuid,
            name=creation.name,
            partition_id=creation.partition_id,
            maximum=creation.max_virtual_slots,
            adapters=creation.virtual_serial_adapters,
            kind="LogicalPartition",
        )
        if serial_requested
        else None
    )
    start_admission = (
        await admit_start_creation(
            hmc, system_uuid, system_name_or_uuid, creation.start_settings
        )
        if start_requested
        else None
    )
    system_name: str | None = None
    apply_step: WorkflowStep | None = None
    readback_error: HMCError | None = None
    resources = complete_create_resources(creation.resources)
    if (
        (
            policy_requested
            or hardware_requested
            or layout_requested
            or serial_requested
            or memory_requested
            or start_requested
        )
        and resources is not None
        and not shared_memory
    ):
        await require_memory_within_system(hmc, system_uuid, resources)
    memory_admission = None
    if memory_requested:
        system_name = await resolve_system_cli_name(hmc.config, system_uuid)
        memory_admission = await admit_creation_memory(
            hmc,
            system_uuid,
            system_name,
            creation.creation_memory,
            resources or creation.resources,
            kind="LogicalPartition",
            environment=creation.partition_type,
            name=creation.name,
            partition_id=creation.partition_id,
        )
    if (
        resources is None
        or policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
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
            ) = await _create_via_cli(
                hmc,
                system_uuid,
                system_name_or_uuid,
                creation,
                controller_mode=controller_mode,
                memory_admission=memory_admission,
                start_admission=start_admission,
            )
        except HMCError as error:
            if (
                serial_requested
                or layout_requested
                or memory_requested
                or start_requested
            ):
                raise
            if hardware_requested or affinity_requested:
                native_failed = True
            elif (
                not creation.physical_io.requested
                and not policy_requested
                and not role_requested
            ):
                raise
            else:
                raise HMCError(
                    "Native role creation outcome is unknown; the partition may exist. Inspect its scoped identity before retrying; no rollback performed."
                    if role_requested
                    else "Native policy creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
                    if policy_requested
                    else "Native I/O creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
                ) from (None if role_requested else error)
        if native_failed:
            raise HMCError(
                f"Native {'affinity group' if affinity_requested else 'hardware memory'} creation outcome is unknown; the partition may exist. Inspect its scoped identity before retrying; no rollback performed."
                if controller_mode
                else f"Native {'affinity group' if affinity_requested else 'hardware memory'} creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
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
    if created_lpar is not None:
        identity_ok = True
        confirmed_lpar = created_lpar
        if serial_admission is not None:
            try:
                confirmed_lpar = await confirm_serial_created(
                    hmc, serial_admission, created_lpar
                )
            except Exception:  # noqa: BLE001 — uncertain identity forbids stamping.
                identity_ok = False
        if memory_admission is not None:
            try:
                confirmed_lpar = await confirm_creation_memory_identity(
                    hmc, memory_admission, created_lpar
                )
            except Exception:  # noqa: BLE001 — uncertain identity forbids stamping.
                identity_ok = False
        if start_admission is not None:
            try:
                confirmed_lpar = await confirm_start_created(
                    hmc,
                    start_admission,
                    created_lpar,
                    creation.name,
                    creation.partition_type,
                )
            except Exception:  # noqa: BLE001 — unknown identity forbids ownership stamping.
                identity_ok = False
        created_lpar = confirmed_lpar if identity_ok else None
    serial_step = (
        await serial_creation_step(
            hmc, serial_admission, created_lpar, controller_mode=controller_mode
        )
        if serial_admission is not None and created_lpar is None
        else None
    )
    apply_warnings = _unapplied_profile_warnings(creation.name, apply_step)
    apply_note = f" {'; '.join(apply_warnings)}" if apply_warnings else ""

    if created_lpar is None:
        if (
            policy_requested
            or hardware_requested
            or layout_requested
            or affinity_requested
            or serial_requested
            or memory_requested
            or start_requested
            or role_requested
        ) and creation.stamp_policy == "required":
            raise HMCError(
                f"{'Creation memory' if memory_requested else 'Affinity group' if affinity_requested else 'Serial' if serial_requested else 'Memory layout' if layout_requested else 'Hardware memory' if hardware_requested else 'Role' if role_requested else 'Policy'} creation may have succeeded, but created identity readback is "
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
        if (
            policy_requested
            or hardware_requested
            or layout_requested
            or serial_requested
            or memory_requested
            or start_requested
            or affinity_requested
            or role_requested
        ):
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
            serial_step=serial_step,
            creation_memory_step=await creation_memory_step(
                hmc, memory_admission, None, controller_mode=controller_mode
            )
            if memory_admission is not None
            else None,
            affinity_group_step=await created_affinity_group_step(
                hmc,
                system_uuid,
                None,
                creation.name,
                creation.partition_type,
                creation.affinity_group_id,
                controller_mode=controller_mode,
            ),
            memory_layout_step=await _created_memory_layout_step(
                hmc, None, creation, controller_mode=controller_mode
            ),
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
                if creation.settings.pairs() or start_requested
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
    if (
        policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
    ) and (
        not isinstance(created_lpar, dict)
        or not isinstance(created_lpar.get("UUID"), str)
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
            None
            if role_requested
            or layout_requested
            or memory_requested
            or start_requested
            or affinity_requested
            else created_lpar,
            None,
            (),
            settings_step=WorkflowStep(
                "settings",
                "error",
                "Created partition identity unconfirmed; roles unverified",
            )
            if role_requested or start_requested
            else None,
            memory_layout_step=await _created_memory_layout_step(
                hmc, None, creation, controller_mode=controller_mode
            ),
            security_policy_step=WorkflowStep(
                "security_policy",
                "error",
                "Created partition identity unconfirmed; inspect before retrying",
            )
            if policy_requested
            else None,
            affinity_group_step=await created_affinity_group_step(
                hmc,
                system_uuid,
                None,
                creation.name,
                creation.partition_type,
                creation.affinity_group_id,
                controller_mode=controller_mode,
            ),
            hardware_memory_step=await created_hardware_memory_step(
                hmc,
                system_uuid,
                created_lpar,
                creation.name,
                creation.partition_type,
                creation.hardware_mem_encryption,
                creation.hardware_mem_expansion,
            ),
            creation_memory_step=await creation_memory_step(
                hmc, memory_admission, None, controller_mode=controller_mode
            )
            if memory_admission is not None
            else None,
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
    serial_step = (
        await serial_creation_step(
            hmc, serial_admission, created_lpar, controller_mode=controller_mode
        )
        if serial_admission is not None
        else None
    )
    memory_step = (
        await creation_memory_step(
            hmc, memory_admission, created_lpar, controller_mode=controller_mode
        )
        if memory_admission is not None
        else None
    )
    layout_step = await _created_memory_layout_step(
        hmc, system_name, creation, controller_mode=controller_mode
    )
    affinity_step = await created_affinity_group_step(
        hmc,
        system_uuid,
        created_lpar,
        creation.name,
        creation.partition_type,
        creation.affinity_group_id,
        controller_mode=controller_mode,
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
            hmc,
            system_uuid,
            system_name,
            creation,
            created_lpar,
            controller_mode=controller_mode,
            start_admission=start_admission,
        )
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else None
    )
    reporting_step = (
        await _created_reporting_step(hmc, system_name, creation)
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else None
    )
    vtpm_step = (
        await _created_vtpm_step(hmc, system_name, creation)
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else None
    )
    policy_step = await _created_security_policy_step(
        hmc, system_uuid, creation, created_lpar
    )
    if (
        policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
    ) and creation.apply_profile is not None:
        required = [
            (creation.settings.pairs() or start_requested, settings_step),
            (creation.reporting_settings.pairs(), reporting_step),
            (creation.vtpm_settings.pairs(), vtpm_step),
            (creation.physical_io.requested, io_step),
            (policy_requested, policy_step),
            (hardware_requested, hardware_step),
            (layout_requested, layout_step),
            (serial_requested, serial_step),
            (memory_requested, memory_step),
            (affinity_requested, affinity_step),
        ]
        verified = ownership_stamped is True and all(
            not requested or (step is not None and step.status == "ok")
            for requested, step in required
        )
        apply_step = (
            await _apply_created_profile(
                hmc, system_name or system_name_or_uuid, creation
            )
            if verified and not controller_mode
            else WorkflowStep(
                "apply_profile",
                "skipped",
                (
                    _CONTROLLER_AFFINITY_PROFILELESS
                    if affinity_requested
                    else _CONTROLLER_CREATE_CONFIGURED
                )
                if controller_mode
                else "Creation verification incomplete; profile not applied",
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
    if (
        memory_admission is not None
        and apply_step is not None
        and apply_step.status == "ok"
    ):
        assert memory_step is not None
        memory_step = await creation_memory_step(
            hmc,
            memory_admission,
            created_lpar,
            controller_mode=controller_mode,
            current=True,
            saved=memory_step.result.saved,
        )
    if (
        creation.physical_io.requested
        or policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
    ):
        apply_warnings = _unapplied_profile_warnings(creation.name, apply_step)
    result = LparCreationResult(
        True,
        created_lpar,
        ownership_stamped,
        (*apply_warnings, *warnings),
        apply_step,
        settings_step
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else await _created_settings_step(
            hmc,
            system_uuid,
            system_name,
            creation,
            created_lpar,
            controller_mode=controller_mode,
            start_admission=start_admission,
        ),
        io_step,
        reporting_step
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else await _created_reporting_step(hmc, system_name, creation),
        vtpm_step
        if policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
        or role_requested
        else await _created_vtpm_step(hmc, system_name, creation),
        policy_step,
        hardware_step,
        memory_layout_step=layout_step,
        serial_step=serial_step,
        creation_memory_step=memory_step,
        affinity_group_step=affinity_step,
    )
    if (
        role_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
        or affinity_requested
    ):
        safe_steps = {}
        step_names = ("apply_step", "physical_io_step", "reporting_step", "vtpm_step")
        if (
            hardware_requested
            or layout_requested
            or serial_requested
            or memory_requested
            or start_requested
            or affinity_requested
        ):
            step_names += (
                "settings_step",
                "security_policy_step",
                "hardware_memory_step",
            )
        for name in step_names:
            step = getattr(result, name)
            if (
                step is not None
                and step.status == "error"
                and isinstance(step.result, str)
            ):
                safe_steps[name] = replace(
                    step,
                    result="Partition exists or may exist; creation step unverified. Inspect before retrying; no rollback performed.",
                )
        result = replace(result, **safe_steps)
    return result


async def _raise_layout_create_failure(
    hmc: HMCClient, system_uuid: str, creation: LparCreation, error: HMCError
) -> None:
    """Reconcile one failed native request without replay, stamping or deletion."""
    outcome = "unknown"
    identity = ""
    try:
        found = await hmc.find_partition_by_name(creation.name, system_uuid=system_uuid)
        if (
            found is None
            and isinstance(error, HMCCLIError)
            and error.remote_refusal_code is not None
        ):
            outcome = "rejected; scoped lookup observed no partition"
        elif (
            isinstance(found, dict)
            and isinstance(found.get("UUID"), str)
            and is_uuid(found["UUID"])
            and isinstance(found.get("Resource"), dict)
            and leaf_text(found["Resource"].get("PartitionName")) == creation.name
        ):
            outcome = "partial; partition exists"
            identity = f" (UUID {found['UUID']})"
    except Exception:  # noqa: BLE001 — any failed post-write readback means unknown.
        outcome = "unknown; scoped identity readback unavailable"
    raise HMCError(
        f"Memory layout creation outcome {outcome}{identity}. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed."
    ) from None


async def _created_memory_layout_step(
    hmc: HMCClient,
    cli_system: str | None,
    creation: LparCreation,
    *,
    controller_mode: bool = False,
) -> WorkflowStep | None:
    requested = creation.memory_layout
    names = tuple(name for name, _ in requested.pairs())
    if not names:
        return None
    observed: dict[str, object] = {}
    warnings: tuple[str, ...] = ()
    if controller_mode:
        warnings = (
            "Controller current configured-value mappings are not established; no named profile was read",
        )
    elif cli_system is None:
        warnings = (
            "Created native identity or CLI system unavailable; layout unverified",
        )
    else:
        try:
            layout = await get_lpar_memory_layout(hmc.config, cli_system, creation.name)
            observed = {
                name: getattr(layout, name)
                for name in names
                if getattr(layout, name) is not None
            }
        except (HMCError, OSError, ValueError, PermissionError):
            warnings = (
                "Saved memory layout readback unavailable; inspect before retrying",
            )
    unavailable = tuple(name for name in names if name not in observed)
    mismatch = any(
        value != getattr(requested, name) for name, value in observed.items()
    )
    status = "mismatch" if mismatch else "unavailable" if unavailable else "verified"
    verification = MemoryLayoutVerification(
        status, requested, observed, unavailable, warnings
    )
    return WorkflowStep(
        "memory_layout", "ok" if status == "verified" else "error", verification
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
    name: str, apply_step: WorkflowStep | None
) -> tuple[str, ...]:
    """Say that a skipped or failed apply left the partition unconfigured."""
    if (
        apply_step is None
        or apply_step.status == "ok"
        or apply_step.result
        in (
            _REST_CREATE_CONFIGURED,
            _CONTROLLER_CREATE_CONFIGURED,
            _CONTROLLER_AFFINITY_PROFILELESS,
        )
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


async def _created_settings_step(
    hmc: HMCClient,
    system_uuid: str,
    cli_system: str | None,
    creation: LparCreation,
    created: dict[str, Any],
    *,
    controller_mode: bool = False,
    start_admission: StartAdmission | None = None,
) -> WorkflowStep | None:
    legacy = await _created_legacy_settings_step(
        hmc, system_uuid, cli_system, creation, created, controller_mode=controller_mode
    )
    if start_admission is None:
        return legacy
    start = await created_start_step(
        hmc,
        start_admission,
        created,
        creation.name,
        creation.partition_type,
        controller_mode=controller_mode,
    )
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
