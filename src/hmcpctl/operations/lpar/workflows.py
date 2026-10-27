"""Complete LPAR creation workflows shared by presentation adapters."""

from __future__ import annotations

from dataclasses import replace

from hmcpctl.client.core import HMCClient

from ...documents.lpar import validate_partition_type
from ...documents.lpar_creation_memory import LparCreationMemory
from ...documents.lpar_hardware_memory import hardware_memory_pairs
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_serial import validate_serial_input
from ...errors import HMCError
from ...resource_identity import resolve_system_uuid
from ...ssh.lpar import complete_create_resources
from .assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from .core import LparCreation, create_and_stamp_lpar
from .errors import translate_lpar_write_error
from .linux_roles import read_role_capabilities, roles_requested
from .workflow_contract import WorkflowStep


async def create_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    assignments: LparPcieAssignments,
) -> LparPcieWorkflowResult:
    """Validate, create, stamp, and apply ordered PCIe assignments."""
    if not isinstance(creation.creation_memory, LparCreationMemory):
        raise TypeError("creation_memory must be LparCreationMemory")
    memory_requested = bool(creation.creation_memory.pairs())
    if memory_requested and creation.keylock is not None:
        raise ValueError("Creation memory cannot be combined with native keylock")
    if creation.creation_memory.mem_mode == "shared":
        resources = complete_create_resources(creation.resources)
        if resources is None or resources.dedicated is not False:
            raise ValueError("Shared memory requires known shared processor resources")
        if (
            creation.physical_io.io_slots
            or creation.creation_sriov
            or assignments.dedicated
            or assignments.sriov
        ):
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
    hardware_requested = bool(
        hardware_memory_pairs(
            creation.hardware_mem_encryption, creation.hardware_mem_expansion
        )
    )
    if hardware_requested and creation.keylock is not None:
        raise ValueError(
            "Hardware memory settings cannot be combined with explicit keylock"
        )
    validate_partition_type(creation.partition_type)
    if (
        roles_requested(creation.settings)
        or layout_requested
        or serial_requested
        or memory_requested
    ) and (assignments.dedicated or assignments.sriov):
        caps = await read_role_capabilities(
            hmc,
            await resolve_system_uuid(hmc, system_name_or_uuid),
            need_kvm=creation.settings.kvm_capable is not None,
            need_management=creation.settings.powervm_mgmt_capable is not None,
        )
        if caps.controller_mode:
            raise ValueError(
                "Dedicated and SR-IOV profile assignments are unavailable in controller mode"
            )
    if creation.physical_io.requested:
        overlap = {s.drc_index for s in creation.physical_io.io_slots} & {
            s.drc_index for s in assignments.dedicated
        }
        if overlap:
            raise ValueError("creation and post-create dedicated assignments overlap")
        if creation.creation_sriov and assignments.sriov:
            raise ValueError("direct SR-IOV creation declarations supplied twice")
        creation = replace(
            creation, creation_sriov=creation.creation_sriov or assignments.sriov
        )
        assignments = replace(assignments, sriov=creation.creation_sriov)
    await prevalidate_lpar_pcie_assignments(hmc, system_name_or_uuid, assignments)
    if creation.physical_io.requested:
        assignments = replace(assignments, sriov=())
    try:
        created = await create_and_stamp_lpar(hmc, system_name_or_uuid, creation)
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    steps = [WorkflowStep("create", "ok", created.lpar)]
    if created.physical_io_step is not None:
        steps.append(created.physical_io_step)
    policy_requested = bool(creation.security_policy.pairs())
    if created.apply_step is not None and not (
        policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
    ):
        steps.append(created.apply_step)
    if created.settings_step is not None:
        steps.append(created.settings_step)
    if created.reporting_step is not None:
        steps.append(created.reporting_step)
    if created.vtpm_step is not None:
        steps.append(created.vtpm_step)
    if created.security_policy_step is not None:
        steps.append(created.security_policy_step)
    if created.hardware_memory_step is not None:
        steps.append(created.hardware_memory_step)
    if created.memory_layout_step is not None:
        steps.append(created.memory_layout_step)
    if created.serial_step is not None:
        steps.append(created.serial_step)
    if created.creation_memory_step is not None:
        steps.append(created.creation_memory_step)
    if created.apply_step is not None and (
        policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
    ):
        steps.append(created.apply_step)
    verification_failed = (
        policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
        or memory_requested
    ) and (
        created.ownership_stamped is not True
        or any(
            requested and (step is None or step.status != "ok")
            for requested, step in (
                (policy_requested, created.security_policy_step),
                (hardware_requested, created.hardware_memory_step),
                (layout_requested, created.memory_layout_step),
                (serial_requested, created.serial_step),
                (memory_requested, created.creation_memory_step),
                (creation.settings.pairs(), created.settings_step),
                (creation.reporting_settings.pairs(), created.reporting_step),
                (creation.vtpm_settings.pairs(), created.vtpm_step),
                (creation.physical_io.requested, created.physical_io_step),
            )
        )
    )
    vtpm_failed = bool(creation.vtpm_settings.pairs()) and (
        created.vtpm_step is None or created.vtpm_step.status != "ok"
    )
    reporting_failed = (
        created.reporting_step is not None and created.reporting_step.status == "error"
    )
    io_failed = (
        created.physical_io_step is not None and created.physical_io_step.status != "ok"
    )
    settings_failed = (
        created.settings_step is not None and created.settings_step.status == "error"
    )
    # A failed apply, or a create that returns no partition body to apply against,
    # stops the ordered workflow; the partition (if created) stays created.
    apply_failed = (
        created.apply_step is not None and created.apply_step.status == "error"
    ) or (
        (roles_requested(creation.settings) or memory_requested)
        and creation.apply_profile is True
        and (created.apply_step is None or created.apply_step.status != "ok")
    )
    assignments_skipped = (
        created.lpar is None
        or apply_failed
        or settings_failed
        or reporting_failed
        or io_failed
        or vtpm_failed
        or verification_failed
    )
    if assignments_skipped:
        steps.extend(
            WorkflowStep(name, "skipped") for name in assignment_step_names(assignments)
        )
        return LparPcieWorkflowResult(
            True,
            False,
            created.lpar,
            created.ownership_stamped,
            tuple(steps),
            created.warnings,
        )
    assignment_result = await apply_validated_lpar_pcie_assignments(
        hmc, system_name_or_uuid, creation.name, assignments
    )
    steps.extend(assignment_result.steps)
    return LparPcieWorkflowResult(
        True,
        assignment_result.workflow_completed,
        created.lpar,
        created.ownership_stamped,
        tuple(steps),
        created.warnings,
    )
