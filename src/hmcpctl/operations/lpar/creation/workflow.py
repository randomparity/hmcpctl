"""Complete LPAR creation workflows shared by presentation adapters."""

from __future__ import annotations

from dataclasses import replace

from hmcpctl.client.core import HMCClient

from ....errors import HMCError
from ..assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from ..errors import translate_lpar_write_error
from ..linux_roles import roles_requested
from ..workflow_contract import WorkflowStep
from .core import (
    LparCreation,
    _create_and_stamp_lpar,
    _creation_followup,
    validate_lpar_creation,
)


async def create_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    assignments: LparPcieAssignments,
) -> LparPcieWorkflowResult:
    """Validate, create, stamp, and apply ordered PCIe assignments."""
    validate_lpar_creation(creation)
    start_requested = creation.start_settings.requested()
    memory_requested = bool(creation.creation_memory.pairs())
    serial_requested = bool(
        creation.virtual_serial_adapters or creation.virtual_scsi_adapters
    )
    layout_requested = bool(creation.memory_layout.pairs())
    affinity_requested = creation.affinity_group_id is not None
    if creation.creation_memory.mem_mode == "shared" and (
        assignments.dedicated or assignments.sriov
    ):
        raise ValueError("Shared memory cannot use direct physical I/O or SR-IOV")
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
    preflight_assignments = None
    if (
        roles_requested(creation.settings)
        or affinity_requested
        or layout_requested
        or serial_requested
        or memory_requested
        or start_requested
    ) and (assignments.dedicated or assignments.sriov):
        preflight_assignments = assignments
    else:
        await prevalidate_lpar_pcie_assignments(hmc, system_name_or_uuid, assignments)
    if creation.physical_io.requested:
        assignments = replace(assignments, sriov=())
    try:
        if preflight_assignments is None:
            created, observations = await _create_and_stamp_lpar(
                hmc, system_name_or_uuid, creation
            )
        else:
            created, observations = await _create_and_stamp_lpar(
                hmc,
                system_name_or_uuid,
                creation,
                preflight_assignments=preflight_assignments,
            )
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    steps, assignments_allowed = _creation_followup(creation, created, observations)
    if not assignments_allowed:
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
