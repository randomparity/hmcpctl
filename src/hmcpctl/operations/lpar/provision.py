"""Presentation-neutral LPAR provisioning: request types, preconditions and legs.

``provision_operation`` runs the legs as one journaled operation (ADR 0313);
``preview_provision`` is the dry run, which validates preconditions only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profile_sync import ChangeLocation, read_change_location

from ...documents.lpar import (
    LparResources,
    PartitionType,
    reject_update_weight_on_create,
    validate_partition_type,
)
from ...documents.lpar_io import LparPhysicalIO
from ...documents.storage import StorageKind, validate_virtual_disk
from ...errors import HMCError
from ...jobs import JobOutcome, job_outcome
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_uuid
from ...ssh.affinity import (
    MinimumAffinityPolicy,
    require_minimum_affinity_policy_capability,
    validate_minimum_affinity_policy,
)
from ...ssh.lpar import DEFAULT_PROFILE_NAME, validate_caller_token
from ...ssh.profiles import parse_profile_io_slots, read_profile_io_slot_rows
from ...ssh.selectors import resolve_ssh_names
from ...ssh.transport import HMCCLIError
from ...xmlutil import console_version, leaf_text
from ..affinity.rest import (
    ProvisionAffinityAssessment,
    assess_post_activation_affinity,
    validate_affinity_request,
)
from ..logical.models import OperationRecord
from ..storage.resources import (
    create_virtual_disk,
    require_allowlisted_iso_url,
    resolve_volume_group_uuid,
)
from ..virtualization.pcie import list_dedicated_slots
from .activation import submit_power_on_lpar
from .assignments import (
    LparPcieAssignments,
    assignment_step_names,
    prevalidate_lpar_pcie_assignments,
)
from .core import LparCreation
from .install_media import (
    Boot,
    LparInstall,
    bind_prepared,
    check_install,
    check_mac,
    repository_group,
)
from .physical_io import admit_creation_io
from .workflow_contract import WorkflowStep


@dataclass(frozen=True)
class ProvisionAdapters:
    """Virtual Ethernet attachment inputs."""

    port_vlan_id: int = field(
        metadata={
            "description": "VLAN identifier for the client virtual Ethernet adapter."
        }
    )
    mac: str | None = field(
        default=None,
        metadata={
            "description": "Adapter MAC in lower-case colon form, created as given; "
            "required with prepared install media and accepted only with it."
        },
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
    capacity_mib: int | None = field(
        default=None,
        metadata={
            "description": "Create storage_name as a new virtual disk of this size in "
            "vg_uuid, owned by the operation; omit to map existing storage."
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
    physical_io: LparPhysicalIO = field(default_factory=LparPhysicalIO)
    install: LparInstall | None = None
    boot: Boot = "immediate"


@dataclass(frozen=True)
class ProvisionResult:
    """Truthful outcome of a provisioning attempt."""

    resource_created: bool | None = field(
        metadata={
            "description": "Whether this call created the LPAR, or null when its outcome is unknown."
        }
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
    operation: OperationRecord | None = field(
        default=None,
        metadata={
            "description": "The logical operation that ran the provision (ADR 0313), "
            "or null for a dry run."
        },
    )
    unverified: tuple[str, ...] = field(
        default=(),
        metadata={
            "description": "What an install run does not check: the VIOS copy of the "
            "media and the guest's reach to its installer source (ADR 0191)."
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


async def check_vlan_exists(
    hmc: HMCClient, system_uuid: str, port_vlan_id: int
) -> None:
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
    hmc: HMCClient, lpar_uuid: str, port_vlan_id: int, mac: str | None = None
) -> dict[str, Any] | None:
    return await hmc.add_network_adapter(
        lpar_uuid,
        port_vlan_id,
        slot_number=None,
        virtual_switch_id=None,
        tagged=False,
        mac_address=mac.replace(":", "") if mac else None,
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
    partition_profile_uuid: str | None = None,
    warnings: list[str] | None = None,
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
    result = await submit_power_on_lpar(
        hmc,
        lpar_uuid,
        system_name_or_uuid=system_name_or_uuid,
        # Not forced: an already active partition would not activate the saved profile.
        force=partition_profile_uuid is None,
        partition_profile_uuid=partition_profile_uuid,
        wait=assessment is not None,
        timeout_seconds=assessment.timeout_seconds if assessment else 300,
        poll_interval=assessment.poll_interval if assessment else 5,
        ownership_override=True,
    )
    if (result.job or {}).get("already_activated"):
        raise HMCError(
            f"LPAR {lpar_uuid} was already active, so its saved profile was not activated"
        )
    if warnings is not None:
        # Adapters in the current configuration that the activated profile lacks.
        warnings.extend(result.warnings)
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
    lpar_name_or_uuid: str,
    storage: ProvisionStorage,
    *,
    system_name_or_uuid: str | None = None,
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
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
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


def validate_provision_assignments(assignments: LparPcieAssignments) -> None:
    """Refuse a dedicated slot that names a profile other than the create's.

    Provision writes each dedicated slot into the profile it saves and activates
    (ADR 0318), which supersedes the create's ``default_profile``.
    """
    for item in assignments.dedicated:
        if item.profile_name != DEFAULT_PROFILE_NAME:
            raise ValueError(
                "hmc_provision_lpar writes each dedicated PCIe slot into the profile it "
                "saves and activates (ADR 0318); pass profile_name "
                f"{DEFAULT_PROFILE_NAME!r}, the profile the create writes, not "
                f"{item.profile_name!r}"
            )


async def _check_dedicated_slots_free(
    hmc: HMCClient, system_name_or_uuid: str, assignments: LparPcieAssignments
) -> None:
    """Refuse a dedicated slot that is unknown, owned, or listed by any profile.

    Found only after every other write otherwise: activation leaves out a slot a
    running partition owns, since it is written not required, and the specialist
    refuses one another profile lists (ADR 0318).
    """
    if not assignments.dedicated:
        return
    inventory = await list_dedicated_slots(hmc, system_name_or_uuid)
    owners = {slot.drc_index: slot.owner_lpar for slot in inventory.items}
    rows = await read_profile_io_slot_rows(hmc.config, inventory.system)
    for item in assignments.dedicated:
        drc = item.drc_index
        if drc not in owners:
            raise ValueError(f"dedicated slot {drc} is not in the system's inventory")
        if owners[drc] is not None:
            raise ValueError(f"dedicated slot {drc} is owned by LPAR {owners[drc]!r}")
        holders = sorted(
            {
                row["lpar_name"]
                for row in rows
                if drc in row["io_slots"]
                and any(
                    s.drc_index == drc for s in parse_profile_io_slots(row["io_slots"])
                )
            }
        )
        if holders:
            raise ValueError(
                f"dedicated slot {drc} is listed by a profile of LPAR "
                f"{', '.join(holders)}; remove it there first"
            )


def validate_provision_storage(storage: ProvisionStorage) -> None:
    """Refuse a new-disk request that names no volume group or a non-virtual kind."""
    if storage.capacity_mib is None:
        return
    if storage.kind != "VirtualDisk" or not storage.vg_uuid:
        raise ValueError(
            "storage.capacity_mib creates a VirtualDisk: pass kind VirtualDisk and vg_uuid"
        )
    validate_virtual_disk(storage.storage_name, storage.capacity_mib)


def virtual_disk_names(group: Mapping[str, Any]) -> set[str]:
    """The virtual-disk names in one parsed VolumeGroup entry."""
    disks = ((group.get("Resource") or {}).get("VirtualDisks") or {}).get("VirtualDisk")
    items = disks if isinstance(disks, list) else [disks]
    names = (leaf_text(d.get("DiskName")) for d in items if isinstance(d, Mapping))
    return {name for name in names if isinstance(name, str)}


async def _check_new_disk_absent(hmc: HMCClient, storage: ProvisionStorage) -> None:
    """Raise ValueError if the new disk's name is already taken on its VIOS."""
    for group in await hmc.list_volume_groups(storage.vios_uuid):
        if storage.storage_name in virtual_disk_names(group):
            raise ValueError(
                f"A virtual disk named {storage.storage_name!r} already exists on "
                f"VIOS {storage.vios_uuid!r}; choose another name or omit "
                "capacity_mib to map it"
            )


# #637's profile jobs (save-current, default profile) need this console level (ADR 0313).
PROFILE_JOB_MINIMUM = (10, 3, 1060)


async def _check_profile_jobs(hmc: HMCClient) -> None:
    console = await hmc.get_console_info()
    resource = console.get("Resource", {}) if isinstance(console, dict) else {}
    version = console_version(resource)
    if version is None or version < PROFILE_JOB_MINIMUM:
        raise ValueError(
            "hmc_provision_lpar persists through partition-profile jobs, which need HMC "
            "10.3.1060 or later; this console's level is "
            f"{'unreadable' if version is None else '.'.join(map(str, version))}"
        )


def validate_provision_install(request: ProvisionRequest) -> None:
    """Refuse install inputs this provision cannot run (ADR 0191, ADR 0319)."""
    install = request.install
    if install is None:
        if request.boot != "immediate":
            raise ValueError("boot: deferred applies only with install")
        check_mac(request.adapters.mac, None)
        return
    if request.storage.capacity_mib is None:
        raise ValueError(
            "storage.capacity_mib: install needs a new disk (ADR 0191 Decision 6)"
        )
    if request.power_on:
        raise ValueError("power_on: with install, boot governs power-on; omit power_on")
    if request.boot != "deferred":
        raise ValueError(
            "boot: install currently needs boot=deferred; the installer power-on "
            "(boot=immediate and continuation=boot) arrives with issue #1849"
        )
    check_install(install)
    if install.media.mode != "prepared":
        raise ValueError(
            "install.media.mode: built media is not available yet (issue #1843); "
            "pass prepared media"
        )
    check_mac(request.adapters.mac, install)
    bind_prepared(install, request.adapters.mac)


def require_install_url(hmc: HMCClient, url: str) -> None:
    """Refuse a media URL off ``HMC_ISO_URL_ALLOWLIST`` without echoing it."""
    try:
        require_allowlisted_iso_url(url, hmc.config.iso_url_allowlist_entries)
    except ValueError:
        raise ValueError(
            "install.media.url: its host is not on HMC_ISO_URL_ALLOWLIST (an unset "
            "allowlist permits nothing); publish the media on a listed origin"
        ) from None


async def preflight_install(hmc: HMCClient, request: ProvisionRequest) -> None:
    """Refuse install media the HMC side cannot take: an unlisted URL, no repository."""
    if request.install is None:
        return
    validate_provision_install(request)
    require_install_url(hmc, request.install.media.url or "")
    await repository_group(hmc, request.storage.vios_uuid)


async def _preflight_provision_request(
    hmc: HMCClient,
    system_name_or_uuid: str,
    request: ProvisionRequest,
) -> None:
    """Validate a provisioning request and every external dependency it names."""
    reject_update_weight_on_create(request.resources)
    validate_provision_assignments(request.assignments)
    validate_provision_storage(request.storage)
    if request.affinity_assessment is not None and not request.power_on:
        raise ValueError(
            "affinity_assessment waits for a successful power-on; pass power_on=true"
        )
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

    await _check_profile_jobs(hmc)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if request.minimum_affinity_policy is not None:
        system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
        await require_minimum_affinity_policy_capability(hmc.config, system_name)
    if request.physical_io.requested:
        await admit_creation_io(
            hmc,
            system_uuid,
            LparCreation(
                request.name,
                request.partition_type,
                request.resources,
                physical_io=request.physical_io,
                creation_sriov=request.assignments.sriov,
            ),
        )
    await _check_name_unique(hmc, system_uuid, request.name)
    await check_vlan_exists(hmc, system_uuid, request.adapters.port_vlan_id)
    if request.storage.vg_uuid is not None:
        await _check_vg_exists(hmc, request.storage.vios_uuid, request.storage.vg_uuid)
    if request.storage.capacity_mib is not None:
        await _check_new_disk_absent(hmc, request.storage)
    await prevalidate_lpar_pcie_assignments(
        hmc, system_name_or_uuid, request.assignments
    )
    await _check_dedicated_slots_free(hmc, system_name_or_uuid, request.assignments)
    await preflight_install(hmc, request)


def _provision_step_names(
    request: ProvisionRequest,
) -> list[str]:
    """Return the ordered workflow-step names for one provision request."""
    names = ["create", "stamp"]
    if request.minimum_affinity_policy is not None:
        names.append("minimum_affinity_policy")
    names.append("network")
    if request.storage.capacity_mib is not None:
        names.append("create_disk")
    assignments = request.assignments
    names.extend(
        ["storage", *assignment_step_names(replace(assignments, dedicated=()))]
    )
    if request.install is not None:
        names.extend(["bind_media", "upload_media", "mount_media"])
    names.extend(["save_profile", "profile_readback"])
    names.extend(assignment_step_names(LparPcieAssignments(assignments.dedicated)))
    names.append("default_profile")
    if request.install is not None:
        names.append("set_boot_order")
    if request.power_on:
        names.append("power_on")
    if request.affinity_assessment is not None:
        names.append("affinity_assessment")
    return names


async def _run_affinity_stage(
    steps: list[WorkflowStep], hmc: HMCClient, request: ProvisionRequest
) -> tuple[bool, tuple[str, ...]]:
    """Run the optional post-activation affinity assessment."""
    if request.affinity_assessment is None:
        return True, ()
    return await _run_affinity_leg(
        steps, hmc, request.affinity_assessment, request.minimum_affinity_policy
    )


async def preview_provision(
    hmc: HMCClient, system_name_or_uuid: str, request: ProvisionRequest
) -> ProvisionResult:
    """Validate every precondition and report the planned steps as ``dry_run``."""
    await _preflight_provision_request(hmc, system_name_or_uuid, request)
    return ProvisionResult(
        resource_created=False,
        workflow_completed=False,
        lpar_uuid=None,
        dry_run=True,
        ownership_stamped=None,
        steps=tuple(WorkflowStep(n, "dry_run") for n in _provision_step_names(request)),
        warnings=(),
    )
