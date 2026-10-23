"""Complete LPAR creation workflows shared by presentation adapters."""

from __future__ import annotations

from dataclasses import replace

from hmcpctl.client.core import HMCClient

from ...documents.lpar import validate_partition_type
from ...errors import HMCError
from .assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from .core import LparCreation, create_and_stamp_lpar
from .errors import translate_lpar_write_error
from .workflow_contract import WorkflowStep


async def create_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    creation: LparCreation,
    assignments: LparPcieAssignments,
) -> LparPcieWorkflowResult:
    """Validate, create, stamp, and apply ordered PCIe assignments."""
    validate_partition_type(creation.partition_type)
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
    if created.apply_step is not None:
        steps.append(created.apply_step)
    if created.settings_step is not None:
        steps.append(created.settings_step)
    if created.reporting_step is not None:
        steps.append(created.reporting_step)
    if created.vtpm_step is not None:
        steps.append(created.vtpm_step)
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
    )
    assignments_skipped = (
        created.lpar is None
        or apply_failed
        or settings_failed
        or reporting_failed
        or io_failed
        or vtpm_failed
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
