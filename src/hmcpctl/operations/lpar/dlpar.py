"""Dynamic LPAR resource mutation and ownership resolution."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation

from ...documents.lpar import LparResources, partition_updates
from ...errors import HMCError
from ...resource_identity import optional_system_selector
from ...xmlutil import escape_xml
from .assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from .errors import translate_lpar_write_error
from .memory_bound import require_memory_within_selected_system
from .profile_sync import (
    change_location_of,
    resource_with_change_location,
)
from .workflow_contract import WorkflowStep

_PROCESSOR_FIELDS = (
    "dedicated",
    "min_procs",
    "desired_procs",
    "max_procs",
    "min_vcpus",
    "desired_vcpus",
    "max_vcpus",
    "sharing_mode",
    "uncapped",
)
_MEMORY_FIELDS = ("min_memory", "desired_memory", "max_memory")


async def modify_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    resources: LparResources,
    assignments: LparPcieAssignments,
    *,
    new_name: str | None = None,
    ownership_override: bool = False,
) -> LparPcieWorkflowResult:
    """Authorize and apply rename, resource, and PCIe changes in order.

    Validation and ownership authorization run before any write. Invalid scope,
    resource values, or assignments raise ``ValueError``; ownership refusal raises
    ``PermissionError``. Inventory and transport errors can also propagate.

    A rename failure propagates. A resource-update ``HMCError`` or ``ValueError``
    also propagates when no rename has succeeded (with actionable write-error
    translation where available). After a successful rename, that same failure
    returns an incomplete result retaining the renamed resource, an error step,
    warnings, and skipped assignment steps. Assignment failures likewise return
    their completed, failed, and skipped steps. Completed changes are never rolled
    back: callers must inspect ``workflow_completed`` and ``steps`` before treating
    a returned result as success. A failed final readback after recorded steps is
    reported in ``warnings`` without discarding those steps.
    """
    system_name_or_uuid = optional_system_selector(system_name_or_uuid)
    if (
        assignments != LparPcieAssignments() or new_name is not None
    ) and system_name_or_uuid is None:
        raise ValueError(
            "system_name_or_uuid is required for rename or PCIe assignments"
        )
    if new_name is not None:
        escape_xml(new_name)
    if system_name_or_uuid is not None:
        await prevalidate_lpar_pcie_assignments(hmc, system_name_or_uuid, assignments)

    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    await require_memory_within_selected_system(hmc, system_name_or_uuid, resources)
    resource = None
    resource_warnings: tuple[str, ...] = ()
    steps: list[WorkflowStep] = []
    if new_name is not None:
        resource = await hmc.update_logical_partition(
            lpar_uuid,
            lambda lpar: partition_updates(lpar, name=new_name),
            "the partition name",
        )
        steps.append(WorkflowStep("rename", "ok", resource))
    if resources != LparResources():
        try:
            resource = await hmc.update_logical_partition(
                lpar_uuid,
                lambda lpar: partition_updates(lpar, resources=resources),
                "the partition resources",
            )
        except (HMCError, ValueError) as exc:
            # A ValueError is a refusal found after the read (a mode mismatch, say); after
            # a rename it must still return the partial result that reports the rename.
            translated = (
                translate_lpar_write_error(exc) if isinstance(exc, HMCError) else exc
            )
            if new_name is None:
                if translated is exc:
                    raise
                raise translated from exc
            steps.append(WorkflowStep("resources", "error", str(translated)))
            steps.extend(
                WorkflowStep(step, "skipped")
                for step in assignment_step_names(assignments)
            )
            return LparPcieWorkflowResult(
                False,
                False,
                resource,
                None,
                tuple(steps),
                (str(translated),),
            )
        steps.append(WorkflowStep("resources", "ok", resource))
        resource_warnings = _profile_scope_warnings(resource)

    assignment_result = await apply_validated_lpar_pcie_assignments(
        hmc,
        system_name_or_uuid or "",
        lpar_uuid,
        assignments,
        ownership_override=ownership_override,
    )
    steps.extend(assignment_result.steps)
    warnings = resource_warnings
    if resource is None:
        try:
            resource = await hmc.get_logical_partition(lpar_uuid)
        except HMCError as exc:
            if not steps:
                raise
            warnings = (*warnings, f"final LPAR readback failed: {exc}")
    return LparPcieWorkflowResult(
        False,
        assignment_result.workflow_completed,
        resource,
        None,
        tuple(steps),
        warnings,
    )


def _profile_scope_warnings(entry: dict[str, Any] | None) -> tuple[str, ...]:
    """Warn when a resource write reached the current configuration but not the profile.

    Read from the write's own response: with ``CurrentProfileSync`` ``Disabled``
    the profile keeps its old values (#1170), so an activation that uses the
    profile discards the change.
    """
    location = change_location_of(entry)
    if location.lives_in == "current-configuration-and-profile":
        return ()
    return (location.summary(),)


async def _apply_dlpar_change(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    resources: LparResources,
    subject: str,
    system_name_or_uuid: str | None,
    ownership_override: bool,
) -> dict[str, Any] | None:
    """Authorize one partition, then change *resources* by read-modify-write.

    The result carries ``change_location`` and, when the change did not reach the
    partition's profile, ``warnings``.
    """
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    await require_memory_within_selected_system(hmc, system_name_or_uuid, resources)
    try:
        updated = await hmc.update_logical_partition(
            lpar_uuid,
            lambda lpar: partition_updates(lpar, resources=resources),
            subject,
        )
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    result = resource_with_change_location(updated, change_location_of(updated))
    if warnings := _profile_scope_warnings(updated):
        result["warnings"] = list(warnings)
    return result


def _require_fields(
    resources: LparResources, fields: tuple[str, ...], kind: str
) -> None:
    if all(getattr(resources, name) is None for name in fields):
        raise ValueError(
            f"Nothing to change: pass at least one {kind} field ({', '.join(fields)})"
        )


async def set_lpar_processors(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    resources: LparResources,
    *,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Authorize and apply a DLPAR processor change to one partition.

    Reads the whole partition and writes it back under ``If-Match`` with only
    the fields set on *resources* changed. For a shared partition ``procs`` are
    processing units (fractional values such as ``0.5`` are valid) and
    ``vcpus`` are virtual processor counts; for a dedicated partition ``procs``
    are whole CPUs. ``dedicated`` must match the partition's current mode or be
    left unset: switching between dedicated and shared is refused before any
    write. Virtual processor counts and ``uncapped`` are refused on a dedicated
    partition. Capping drops the uncapped weight, and uncapping a capped
    partition leaves its weight 0 (no share of spare capacity); no parameter
    sets the weight. A request carrying no processor field is refused before
    any request.

    The write changes the partition's current configuration, not a partition
    profile. With no active RMC connection it applies on the next activation only
    if that activation uses the current configuration; activating a profile
    discards it unless ``CurrentProfileSync`` is ``On``. The result's
    ``change_location`` and ``warnings`` say which. No reboot is triggered either way.

    ADR 0092 §3.2 classifies this as Reconfiguring, so
    :func:`authorize_lpar_mutation` runs unconditionally before the write.
    *system_name_or_uuid* stays optional (ADR 0063): when it is omitted the
    owning managed system is discovered so the guard can still read the token
    (ADR 0094).
    """
    _require_fields(resources, _PROCESSOR_FIELDS, "processor")
    return await _apply_dlpar_change(
        hmc,
        lpar_name_or_uuid,
        replace(resources, min_memory=None, desired_memory=None, max_memory=None),
        "the processor configuration",
        system_name_or_uuid,
        ownership_override,
    )


async def set_lpar_memory(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    resources: LparResources,
    *,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Authorize and apply a DLPAR memory change to one partition.

    Reads the whole partition and writes it back under ``If-Match`` with only
    the memory fields set on *resources* changed. Memory values are in MiB; the
    processor fields of *resources* are ignored. A request carrying no memory
    field is refused before any request.

    The write changes the partition's current configuration, not a partition
    profile. With no active RMC connection it applies on the next activation only
    if that activation uses the current configuration; activating a profile
    discards it unless ``CurrentProfileSync`` is ``On``. The result's
    ``change_location`` and ``warnings`` say which. No reboot is triggered either way.

    ADR 0092 §3.2 classifies this as Reconfiguring, so
    :func:`authorize_lpar_mutation` runs unconditionally before the write.
    *system_name_or_uuid* stays optional (ADR 0063): when it is omitted the
    owning managed system is discovered so the guard can still read the token
    (ADR 0094).
    """
    _require_fields(resources, _MEMORY_FIELDS, "memory")
    return await _apply_dlpar_change(
        hmc,
        lpar_name_or_uuid,
        LparResources(
            min_memory=resources.min_memory,
            desired_memory=resources.desired_memory,
            max_memory=resources.max_memory,
        ),
        "the memory configuration",
        system_name_or_uuid,
        ownership_override,
    )
