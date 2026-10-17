"""Presentation-neutral LPAR provisioning workflow.

Composes create_logical_partition + add_network_adapter + map_storage_to_lpar +
power-on into a single call with a structured per-step
result and an optional dry-run that validates preconditions only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profile_sync import ChangeLocation, read_change_location

from ...documents.lpar import LparResources, PartitionType, validate_partition_type
from ...documents.storage import StorageKind, validate_virtual_disk
from ...errors import HMCError
from ...jobs import JobOutcome, job_outcome
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_uuid
from ...ssh.affinity import (
    MinimumAffinityPolicy,
    require_minimum_affinity_policy_capability,
    validate_minimum_affinity_policy,
)
from ...ssh.lpar import validate_caller_token
from ...ssh.selectors import resolve_ssh_names
from ...ssh.transport import HMCCLIError
from ..affinity.rest import (
    ProvisionAffinityAssessment,
    assess_post_activation_affinity,
    validate_affinity_request,
)
from ..affinity.ssh import set_minimum_affinity_policy
from ..storage.resources import create_virtual_disk, resolve_volume_group_uuid
from .assignments import (
    LparPcieAssignments,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from .core import (
    LparCreation,
    LparCreationResult,
    create_and_stamp_lpar,
    power_lpar,
)
from .workflow_contract import WorkflowStep


@dataclass(frozen=True)
class ProvisionAdapters:
    """Virtual Ethernet attachment inputs."""

    port_vlan_id: int = field(
        metadata={
            "description": "VLAN identifier for the client virtual Ethernet adapter."
        }
    )


@dataclass(frozen=True)
class ProvisionStorage:
    """VIOS-backed storage mapping inputs."""

    vios_uuid: str = field(
        metadata={"description": "UUID of the VIOS that owns the storage."}
    )
    storage_name: str = field(
        metadata={"description": "Physical-volume or virtual-disk name to map."}
    )
    kind: StorageKind = field(
        default="VirtualDisk", metadata={"description": "Storage resource kind to map."}
    )
    vg_uuid: str | None = field(
        default=None,
        metadata={
            "description": "Volume-group UUID used when creating a virtual disk."
        },
    )


@dataclass(frozen=True)
class ProvisionRequest:
    """Typed inputs for the end-to-end LPAR provisioning workflow."""

    name: str
    adapters: ProvisionAdapters
    storage: ProvisionStorage
    resources: LparResources
    partition_type: PartitionType = "AIX/Linux"
    power_on: bool = True
    dry_run: bool = False
    assignments: LparPcieAssignments = LparPcieAssignments()
    caller_token: str | None = None
    minimum_affinity_policy: MinimumAffinityPolicy | None = None
    affinity_assessment: ProvisionAffinityAssessment | None = None


@dataclass(frozen=True)
class ProvisionResult:
    """Truthful outcome of a provisioning attempt."""

    resource_created: bool = field(
        metadata={"description": "Whether this call created the LPAR."}
    )
    workflow_completed: bool = field(
        metadata={"description": "Whether every requested workflow step completed."}
    )
    lpar_uuid: str | None = field(
        metadata={"description": "Created LPAR UUID, or null before creation."}
    )
    dry_run: bool = field(
        metadata={"description": "Whether the call only validated preconditions."}
    )
    ownership_stamped: bool | None = field(
        metadata={
            "description": "Ownership-token stamp result, or null when not attempted."
        }
    )
    steps: tuple[WorkflowStep, ...] = field(
        metadata={"description": "Ordered per-step status and result records."}
    )
    warnings: tuple[str, ...] = field(
        metadata={"description": "Non-fatal workflow warnings."}
    )
    change_location: ChangeLocation | None = field(
        default=None,
        metadata={
            "description": (
                "Where the network and storage changes now live: the "
                "partition's CurrentProfileSync and whether the change reaches "
                "its current profile too. Null when no adapter or mapping step "
                "ran, or when the read failed (see warnings)."
            )
        },
    )


@dataclass(frozen=True)
class AttachDiskResult:
    """Truthful outcome of attaching a new disk to an existing LPAR."""

    workflow_completed: bool
    lpar_uuid: str
    dry_run: bool
    steps: tuple[WorkflowStep, ...]
    warnings: tuple[str, ...]
    change_location: ChangeLocation | None = field(
        default=None,
        metadata={
            "description": (
                "Where the new mapping now lives: the partition's "
                "CurrentProfileSync and whether the change reaches its current "
                "profile too. Null on a dry run, or when the read failed (see "
                "warnings)."
            )
        },
    )


async def _check_name_unique(hmc, system_uuid: str, name: str) -> None:
    """Raise ValueError if an LPAR with *name* already exists on *system_uuid*.

    Scoped: the HMC-wide search misses partitions on a system it cannot read
    (#1309), and the scoped feed refuses a non-operating system.
    """
    existing = await hmc.find_partition_by_name(name, system_uuid=system_uuid)
    if existing:
        raise ValueError(
            f"An LPAR named {name!r} already exists "
            f"(UUID {existing.get('UUID')!r}). Choose a different name "
            "or delete the existing partition first."
        )


async def _check_vlan_exists(hmc, system_uuid: str, port_vlan_id: int) -> None:
    """Raise ValueError if no VirtualNetwork with *port_vlan_id* exists."""
    networks = await hmc.list_virtual_networks(system_uuid)
    malformed: list[str] = []
    for net in networks:
        res = net.get("Resource") or {}
        vlan = res.get("NetworkVLANID")
        if vlan is None:
            continue
        try:
            parsed_vlan = int(vlan)
        except (TypeError, ValueError):
            identity = res.get("NetworkName") or net.get("UUID") or "unknown network"
            malformed.append(f"{identity!r} has NetworkVLANID {vlan!r}")
            continue
        if parsed_vlan == port_vlan_id:
            return
    malformed_note = (
        f" Ignored malformed network records: {', '.join(malformed)}."
        if malformed
        else ""
    )
    raise ValueError(
        f"No VirtualNetwork with VLAN ID {port_vlan_id} found on system "
        f"{system_uuid!r}. List virtual networks to inspect available VLANs."
        f"{malformed_note}"
    )


async def _check_vg_exists(hmc, vios_uuid: str, vg_uuid: str) -> None:
    """Raise ValueError if no VolumeGroup with *vg_uuid* exists on *vios_uuid*."""
    if not is_uuid(vg_uuid):
        raise ValueError("ProvisionStorage vg_uuid must be a canonical UUID")
    await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)


async def _record_hmc_step(
    steps: list[WorkflowStep], name: str, operation: Awaitable[Any]
) -> bool:
    """Record an expected HMC operation failure and propagate code defects."""
    try:
        result = await operation
    except HMCError as exc:
        steps.append(WorkflowStep(name, "error", str(exc)))
        return False
    steps.append(WorkflowStep(name, "ok", result))
    return True


async def _read_change_location(
    hmc: HMCClient, lpar_uuid: str
) -> tuple[ChangeLocation | None, tuple[str, ...]]:
    """Where the network and storage changes just made now live.

    Advisory, like ``profile_adapter_warnings``: a failed read becomes a
    warning rather than failing provisioning that already succeeded.
    """
    try:
        return await read_change_location(hmc, lpar_uuid), ()
    except HMCError as exc:
        return None, (f"Change location not read: {exc}",)


async def _add_network(
    hmc: HMCClient, lpar_uuid: str, port_vlan_id: int
) -> dict[str, Any] | None:
    return await hmc.add_network_adapter(
        lpar_uuid,
        port_vlan_id,
        slot_number=None,
        virtual_switch_id=None,
        tagged=False,
        mac_address=None,
    )


async def _map_storage(
    hmc: HMCClient, storage: ProvisionStorage, lpar_uuid: str
) -> dict[str, Any]:
    await hmc.map_storage_to_lpar(
        storage.vios_uuid,
        storage.kind,
        storage.storage_name,
        lpar_uuid,
        None,
    )
    return {
        "lpar_uuid": lpar_uuid,
        "vios_uuid": storage.vios_uuid,
        "storage_name": storage.storage_name,
    }


async def _create_disk(
    hmc: HMCClient, storage: ProvisionStorage, capacity_mib: int
) -> dict[str, Any]:
    if storage.vg_uuid is None:
        raise ValueError("virtual-disk creation requires storage vg_uuid")
    await create_virtual_disk(
        hmc,
        storage.vios_uuid,
        storage.vg_uuid,
        storage.storage_name,
        capacity_mib,
        system_name_or_uuid=None,
    )
    return {"disk_name": storage.storage_name, "capacity_mib": capacity_mib}


async def _power_on(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_uuid: str,
    assessment: ProvisionAffinityAssessment | None,
) -> dict[str, Any] | JobOutcome | None:
    """Activate the partition this workflow just created and stamped.

    ``ownership_override=True`` because the token this leg would authorize
    against is the one the same call stamped moments earlier (ADR 0092
    Consequences). The override keeps the resolution inside ADR 0092 §5's two
    mechanisms — it is audited — rather than adding a call-site-conditional
    guard. With ``authorize_power_operations`` on it spares the SSH ownership
    read and both managed-system name reads, and costs one partition-name GET
    (the UUID this leg passes needs no lookup); with the setting off nothing
    here runs at all. It also means every successful provision emits an
    ``ownership-override`` audit record once the setting is on —
    ``docs/authorization-audit.md`` records that the event is not
    human-triggered only.
    """
    result = await power_lpar(
        hmc,
        system_name_or_uuid,
        lpar_uuid,
        power_on=True,
        force=True,
        wait=assessment is not None,
        timeout_seconds=assessment.timeout_seconds if assessment else 300,
        poll_interval=assessment.poll_interval if assessment else 5,
        ownership_override=True,
    )
    if assessment is None:
        return result.job
    return job_outcome("PowerOn", result.job)


def _skip_steps(steps: list[WorkflowStep], names: list[str]) -> None:
    steps.extend(WorkflowStep(name, "skipped") for name in names)


async def _run_storage_leg(
    hmc: HMCClient,
    lpar_uuid: str,
    storage: ProvisionStorage,
    *,
    disk_capacity_mib: int | None = None,
) -> tuple[list[WorkflowStep], bool]:
    """Run the shared ordered vSCSI storage workflow.

    No client adapter is added first: the mapping makes the HMC create its own
    client/server adapter pair, and one added beforehand is left unpaired
    (ADR 0169, #1030).
    """
    steps: list[WorkflowStep] = []
    operations: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
    if disk_capacity_mib is not None:
        operations.append(
            ("create_disk", lambda: _create_disk(hmc, storage, disk_capacity_mib))
        )
    operations.append(("storage", lambda: _map_storage(hmc, storage, lpar_uuid)))
    for index, (name, operation) in enumerate(operations):
        if not await _record_hmc_step(steps, name, operation()):
            _skip_steps(steps, [step_name for step_name, _ in operations[index + 1 :]])
            return steps, False
    return steps, True


async def attach_disk_to_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    storage: ProvisionStorage,
    *,
    capacity_mib: int,
    dry_run: bool = False,
    ownership_override: bool = False,
) -> AttachDiskResult:
    """Create and attach a virtual disk to an existing LPAR."""
    if storage.kind != "VirtualDisk" or storage.vg_uuid is None:
        raise ValueError("disk attachment requires a VirtualDisk with vg_uuid")
    validate_virtual_disk(storage.storage_name, capacity_mib)

    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    await _check_vg_exists(hmc, storage.vios_uuid, storage.vg_uuid)
    step_names = ["create_disk", "storage"]
    if dry_run:
        return AttachDiskResult(
            workflow_completed=False,
            lpar_uuid=lpar_uuid,
            dry_run=True,
            steps=tuple(WorkflowStep(name, "dry_run") for name in step_names),
            warnings=(),
        )

    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )

    change_location, warnings = await _read_change_location(hmc, lpar_uuid)
    steps, completed = await _run_storage_leg(
        hmc,
        lpar_uuid,
        storage,
        disk_capacity_mib=capacity_mib,
    )
    return AttachDiskResult(
        workflow_completed=completed,
        lpar_uuid=lpar_uuid,
        dry_run=False,
        steps=tuple(steps),
        warnings=warnings,
        change_location=change_location,
    )


def _provision_result(
    creation: LparCreationResult | None,
    created_uuid: str | None,
    steps: list[WorkflowStep],
    workflow_completed: bool,
    warnings: tuple[str, ...] = (),
    change_location: ChangeLocation | None = None,
) -> ProvisionResult:
    return ProvisionResult(
        resource_created=creation.resource_created if creation else False,
        workflow_completed=workflow_completed,
        lpar_uuid=created_uuid,
        dry_run=False,
        ownership_stamped=creation.ownership_stamped if creation else None,
        steps=tuple(steps),
        warnings=(*(creation.warnings if creation else ()), *warnings),
        change_location=change_location,
    )


async def _run_policy_leg(
    steps: list[WorkflowStep],
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name: str,
    policy: MinimumAffinityPolicy | None,
) -> bool:
    if policy is None:
        return True
    return await _record_hmc_step(
        steps,
        "minimum_affinity_policy",
        set_minimum_affinity_policy(hmc, system_name_or_uuid, lpar_name, policy),
    )


async def _run_network_leg(
    steps: list[WorkflowStep], hmc: HMCClient, lpar_uuid: str, vlan_id: int
) -> bool:
    return await _record_hmc_step(
        steps, "network", _add_network(hmc, lpar_uuid, vlan_id)
    )


async def _run_assignment_leg(
    steps: list[WorkflowStep],
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name: str,
    assignments: LparPcieAssignments,
) -> bool:
    result = await apply_validated_lpar_pcie_assignments(
        hmc, system_name_or_uuid, lpar_name, assignments
    )
    steps.extend(result.steps)
    return result.workflow_completed


async def _run_power_leg(
    steps: list[WorkflowStep],
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_uuid: str,
    assessment: ProvisionAffinityAssessment | None,
) -> bool:
    try:
        result = await _power_on(hmc, system_name_or_uuid, lpar_uuid, assessment)
    except (HMCError, ValueError) as exc:
        steps.append(WorkflowStep("power_on", "error", str(exc)))
        return False
    if isinstance(result, JobOutcome):
        if result.timed_out or result.error is not None:
            message = result.error or (
                "PowerOn did not reach a successful terminal status before timeout"
            )
            steps.append(WorkflowStep("power_on", "error", message))
            return False
        steps.append(WorkflowStep("power_on", "ok", asdict(result)))
    else:
        steps.append(WorkflowStep("power_on", "ok", result))
    return True


async def _run_affinity_leg(
    steps: list[WorkflowStep],
    hmc: HMCClient,
    assessment: ProvisionAffinityAssessment,
    policy: MinimumAffinityPolicy | None,
) -> tuple[bool, tuple[str, ...]]:
    configured_minimum = policy.min_affinity_score if policy is not None else None
    try:
        result = await assess_post_activation_affinity(
            hmc, assessment, configured_minimum=configured_minimum
        )
    except (HMCError, HMCCLIError) as exc:
        steps.append(WorkflowStep("affinity_assessment", "error", str(exc)))
        return False, ()
    classification = result.assessment.classification
    serialized_result = asdict(result)
    if classification == "none":
        steps.append(WorkflowStep("affinity_assessment", "ok", serialized_result))
        return True, ()
    if assessment.response == "fail":
        steps.append(WorkflowStep("affinity_assessment", "error", serialized_result))
        return False, ()
    steps.append(WorkflowStep("affinity_assessment", "ok", serialized_result))
    return True, (f"Post-activation affinity assessment: {classification}",)


def _append_apply_step(
    steps: list[WorkflowStep], step_names: list[str], creation: LparCreationResult
) -> bool:
    """Record the mksyscfg path's profile apply after ``create``; False on error."""
    apply_step = creation.apply_step
    if apply_step is None:
        return True
    step_names.insert(1, apply_step.step)
    steps.append(apply_step)
    return apply_step.status != "error"


def _failed_provision_result(
    creation: LparCreationResult,
    created_uuid: str,
    steps: list[WorkflowStep],
    step_names: list[str],
    warnings: tuple[str, ...] = (),
    change_location: ChangeLocation | None = None,
) -> ProvisionResult:
    _skip_steps(steps, step_names[len(steps) :])
    return _provision_result(
        creation, created_uuid, steps, False, warnings, change_location
    )


def validate_provision_assignments(assignments: LparPcieAssignments) -> None:
    """Refuse a dedicated-slot request, which provision cannot activate (#1390).

    Provision's PowerOn activates the current configuration, so a slot written to
    the new profile is never owned by the running partition. Activating the profile
    instead would discard the adapters provision adds to the current configuration
    (#1345). #1225 makes provision profile-first, which removes the conflict.
    """
    if assignments.dedicated:
        raise ValueError(
            "hmc_provision_lpar does not assign dedicated PCIe slots: its power-on "
            "activates the current configuration, so the partition would not own a "
            "slot written to its profile (#1390). Create the partition instead "
            "(hmc_create_lpar, optionally with assignments.dedicated), or assign the "
            "slot to its profile with hmc_assign_dedicated_pcie_slot while it is not "
            "activated; then power it on with that profile (hmc_power_on_lpar with "
            "partition_profile_uuid)."
        )


async def _preflight_provision_request(
    hmc: HMCClient,
    system_name_or_uuid: str,
    request: ProvisionRequest,
) -> None:
    """Validate a provisioning request and every external dependency it names."""
    validate_provision_assignments(request.assignments)
    if request.affinity_assessment is not None and (
        request.affinity_assessment.system_name_or_uuid != system_name_or_uuid
        or request.affinity_assessment.lpar_name != request.name
    ):
        raise ValueError(
            "affinity assessment identities must match the provisioned system and LPAR"
        )
    if request.minimum_affinity_policy is not None:
        validate_minimum_affinity_policy(request.minimum_affinity_policy)
    if request.affinity_assessment is not None:
        configured_minimum = (
            request.minimum_affinity_policy.min_affinity_score
            if request.minimum_affinity_policy is not None
            else None
        )
        validate_affinity_request(request.affinity_assessment, configured_minimum)
    if request.caller_token is not None:
        validate_caller_token(request.caller_token)
    validate_partition_type(request.partition_type)

    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if request.minimum_affinity_policy is not None:
        system_name, _ = await resolve_ssh_names(hmc.config, system_name_or_uuid, None)
        await require_minimum_affinity_policy_capability(hmc.config, system_name)
    await _check_name_unique(hmc, system_uuid, request.name)
    await _check_vlan_exists(hmc, system_uuid, request.adapters.port_vlan_id)
    if request.storage.vg_uuid is not None:
        await _check_vg_exists(hmc, request.storage.vios_uuid, request.storage.vg_uuid)
    await prevalidate_lpar_pcie_assignments(
        hmc, system_name_or_uuid, request.assignments
    )


def _provision_step_names(
    request: ProvisionRequest,
) -> list[str]:
    """Return the ordered workflow-step names for one provision request."""
    names = ["create"]
    if request.minimum_affinity_policy is not None:
        names.append("minimum_affinity_policy")
    names.extend(["network", "storage", *assignment_step_names(request.assignments)])
    if request.power_on:
        names.append("power_on")
    if request.affinity_assessment is not None:
        names.append("affinity_assessment")
    return names


async def _run_legs_through_power(
    hmc: HMCClient,
    system_name_or_uuid: str,
    request: ProvisionRequest,
    lpar_uuid: str,
    steps: list[WorkflowStep],
    step_names: list[str],
) -> tuple[bool, tuple[str, ...], ChangeLocation | None]:
    """Run the policy through power legs; on failure, skip the remaining steps.

    Returns the completed flag, the change-location read's warnings, and the
    change location. Both are empty until the read, which follows the network
    leg.
    """
    change_location: ChangeLocation | None = None
    warnings: tuple[str, ...] = ()
    completed = await _run_policy_leg(
        steps,
        hmc,
        system_name_or_uuid,
        request.name,
        request.minimum_affinity_policy,
    )
    completed = completed and await _run_network_leg(
        steps, hmc, lpar_uuid, request.adapters.port_vlan_id
    )
    if completed:
        # Read once here, not through the standalone adapter/storage operations,
        # which would repeat the authorization read once per step (#1056).
        change_location, warnings = await _read_change_location(hmc, lpar_uuid)
        storage_steps, completed = await _run_storage_leg(
            hmc, lpar_uuid, request.storage
        )
        steps.extend(storage_steps)
    completed = completed and await _run_assignment_leg(
        steps, hmc, system_name_or_uuid, request.name, request.assignments
    )
    completed = completed and (
        not request.power_on
        or await _run_power_leg(
            steps, hmc, system_name_or_uuid, lpar_uuid, request.affinity_assessment
        )
    )
    if not completed:
        _skip_steps(steps, step_names[len(steps) :])
    return completed, warnings, change_location


async def _run_affinity_stage(
    steps: list[WorkflowStep], hmc: HMCClient, request: ProvisionRequest
) -> tuple[bool, tuple[str, ...]]:
    """Run the optional post-activation affinity assessment."""
    if request.affinity_assessment is None:
        return True, ()
    if not request.power_on:
        steps.append(WorkflowStep("affinity_assessment", "skipped"))
        return False, ()
    return await _run_affinity_leg(
        steps, hmc, request.affinity_assessment, request.minimum_affinity_policy
    )


async def provision_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    request: ProvisionRequest,
) -> ProvisionResult:
    """Provision an LPAR after validating every requested dependency.

    ``dry_run`` performs only the precondition checks and marks each planned
    step ``dry_run``. On partial failure, completed, failed, and remaining
    steps are reported as ``ok``, ``error``, and ``skipped`` respectively;
    no automatic rollback is attempted.

    Ownership stamping is best effort. ``ownership_stamped`` distinguishes a
    confirmed write, a failed write, and a dry run where no stamp was
    attempted. When ``caller_token`` is supplied, it is written together with
    the ownership stamp, so the result describes both values as one write.
    """

    await _preflight_provision_request(
        hmc,
        system_name_or_uuid,
        request,
    )
    step_names = _provision_step_names(request)

    if request.dry_run:
        return ProvisionResult(
            resource_created=False,
            workflow_completed=False,
            lpar_uuid=None,
            dry_run=True,
            ownership_stamped=None,
            steps=tuple(WorkflowStep(n, "dry_run") for n in step_names),
            warnings=(),
        )

    steps: list[WorkflowStep] = []
    try:
        creation = await create_and_stamp_lpar(
            hmc,
            system_name_or_uuid,
            LparCreation(
                request.name,
                request.partition_type,
                request.resources,
                caller_token=request.caller_token,
                apply_profile=True,
            ),
        )
    except HMCError as exc:
        steps.append(WorkflowStep("create", "error", str(exc)))
        _skip_steps(steps, step_names[1:])
        return _provision_result(None, None, steps, False)

    created_lpar = creation.lpar
    created_uuid = (created_lpar or {}).get("UUID")
    if not isinstance(created_uuid, str) or not created_uuid:
        steps.append(WorkflowStep("create", "error", "LPAR creation returned no UUID"))
        _append_apply_step(steps, step_names, creation)
        _skip_steps(steps, step_names[len(steps) :])
        return _provision_result(creation, None, steps, False)
    steps.append(WorkflowStep("create", "ok", created_lpar))
    if not _append_apply_step(steps, step_names, creation):
        return _failed_provision_result(creation, created_uuid, steps, step_names)

    completed, warnings, change_location = await _run_legs_through_power(
        hmc, system_name_or_uuid, request, created_uuid, steps, step_names
    )
    if completed:
        completed, affinity_warnings = await _run_affinity_stage(steps, hmc, request)
        warnings += affinity_warnings
    return _provision_result(
        creation, created_uuid, steps, completed, warnings, change_location
    )
