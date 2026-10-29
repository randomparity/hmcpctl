"""MCP tool for creating an LPAR."""

from __future__ import annotations

from ..._app import with_client
from ...documents.lpar import (
    Keylock,
    LparReportingSettings,
    LparResources,
    LparSettings,
    PartitionType,
    validate_partition_type,
)
from ...documents.lpar_affinity_group import AffinityGroupID, validate_affinity_group_id
from ...documents.lpar_creation_memory import LparCreationMemory
from ...documents.lpar_hardware_memory import HardwareMemoryFlag
from ...documents.lpar_io import LparPhysicalIO
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_security_policy import LparSecurityPolicy
from ...documents.lpar_serial import VirtualSerialAdapter, validate_serial_input
from ...documents.lpar_start import LparStartSettings
from ...documents.lpar_vtpm import LparVtpmSettings
from ...operations.lpar.assignments import LparPcieAssignments, LparPcieWorkflowResult
from ...operations.lpar.core import LparCreation
from ...operations.lpar.workflows import create_lpar
from ...ssh.lpar import validate_caller_token
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


# vNIC assignments name a nested VIOS that target extraction cannot authorize.
@tool(
    effect="mutate",
    operation="lpar.create",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_create_lpar(
    system_name_or_uuid: str,
    name: str,
    resources: LparResources = LparResources(
        min_memory=256,
        desired_memory=4096,
        max_memory=8192,
        dedicated=False,
        desired_vcpus=1,
        max_vcpus=2,
        uncapped=True,
    ),
    partition_type: PartitionType = "AIX/Linux",
    partition_id: int | None = None,
    keylock: Keylock | None = None,
    max_virtual_slots: int | None = None,
    caller_token: str | None = None,
    assignments: LparPcieAssignments = LparPcieAssignments(),
    apply_partition_profile: bool = True,
    physical_io: LparPhysicalIO | None = None,
    profile: str | None = None,
    settings: LparSettings | None = None,
    reporting_settings: LparReportingSettings | None = None,
    vtpm_settings: LparVtpmSettings | None = None,
    security_policy: LparSecurityPolicy | None = None,
    hardware_mem_encryption: HardwareMemoryFlag | None = None,
    hardware_mem_expansion: HardwareMemoryFlag | None = None,
    memory_layout: LparMemoryLayout | None = None,
    virtual_serial_adapters: tuple[VirtualSerialAdapter, ...] = (),
    creation_memory: LparCreationMemory | None = None,
    affinity_group_id: AffinityGroupID | None = None,
    start_settings: LparStartSettings | None = None,
) -> LparPcieWorkflowResult:
    """Create a new LPAR on a managed system.

    system_name_or_uuid: the target managed system — accepts either a
    SystemName or a UUID. Memory values are in MiB. By default a
    shared-processor partition is created; set dedicated=True for dedicated
    CPUs (then procs are whole CPU counts). For shared partitions, procs are
    processing units (may be fractional, e.g. 0.5) and vcpus are virtual
    processor counts.

    The partition is created powered off with a default profile; storage,
    network and boot settings still need to be configured before it can boot an
    OS. This creates a real partition — confirm the target before calling.

    Raises ValueError if a partition with the given name already exists on the
    target managed system, and HMCError, before creating anything, when that
    system's partitions cannot be read (for example, it has no connection).

    Returns a dict with ``lpar``, ``ownership_stamped``, and ``warnings`` keys.

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system to create on.
        name: Unique PartitionName for the new logical partition.
        creation_memory: Optional eight strict creation-only memory fields. Explicit
            shared mode is required for entitlement, weight and paging selectors;
            providers require independent contained ownership admission. Omission
            preserves defaults; zero and secondary ID "none" remain explicit.
            Saved AME zero or numeric 1–10 declares no AIX intent, including VIOS
            and IBM i; positive AME requires selected-system capability. Saved and
            current configured observations are separate and gate follow-ups;
            unavailable evidence is incomplete. AIX-only guest runtime compression
            guidance remains separate: no activation, runtime or replay guarantee.
        resources: Memory and processor assignments for the new partition.
        virtual_serial_adapters: Optional strict seven-component serial creation records
            (64 KiB encoded maximum). Empty preserves defaults. Unique local slots >=2
            require explicit max_virtual_slots; peers must be contained and authorized.
            Client eligibility is HMC-final in every environment. No reciprocal mutation,
            retry, activation or traffic proof. Saved configuration/pair observations gate
            follow-ups; controller current observations remain unavailable.
        memory_layout: Optional AIX/Linux huge-page counts, advertised HPT/PPT
            ratios, and Linux reservation. Reservation selection including zero
            declares intent, not installed OS proof; finite/two-decimal values,
            canonical encoding <=65536 ASCII bytes; HMC decides numeric limits.
            Huge pages use a separate pool; capacity is checked at activation.
            Saved profile verification does not prove activation or runtime;
            controller current values may be unavailable, blocking follow-ups.
        start_settings: Optional five creation-only boot/start settings. Omission preserves
            defaults; explicit false is retained. Controllers require contained ownership
            admission and independent required-plane verification before follow-up effects.
        affinity_group_id: Optional native affinity group: integer 1–255 or literal none;
            omission preserves defaults. Fresh classic saved-profile proof gates follow-ups;
            native partition mismatch/unavailability is reported separately. Controller
            saved proof remains unavailable. No current/runtime or placement claim.
        hardware_mem_encryption: Optional hardware memory encryption access;
            omission preserves defaults, false explicitly disables access.
        hardware_mem_expansion: Optional hardware memory expansion access;
            omission preserves defaults, false explicitly disables access.
        security_policy: Optional strict pending secure-boot/keystore policy.
            Signed enablement is irreversible; store requires inactivity and HMC
            bounds. Linux selection declares intent, not guest proof. No key
            actions; unverified policy stops follow-ups.
        vtpm_settings: Optional advertised vTPM fields for AIX/Linux creation; native single-record creation, no implicit reset. Unverified readback stops assignments; explicit keylock cannot be combined.
        reporting_settings: Optional C02 creation fields: restricted I/O and electronic
            reporting (IBM i only), work group integer or "none", redundant reporting.
            IPL source is update-only. Explicit keylock plus reporting is refused
            before effects. Omitted values retain HMC defaults; false/zero/"none"
            are explicit. Readback failure leaves the created partition and stops
            assignments. Configuration does not prove guest-runtime behavior.
        settings: Optional partition settings, including two Linux roles. Omitted
            values retain HMC defaults; False/0 are explicit. A running time_ref partition can govern
            hypervisor/service-processor time. Settings select documented CLI creation
            unless explicit keylock requires REST plus a separate configuration step.
            A failed settings step leaves the partition created and stops assignments.
            Role field selection, including false, declares Linux intent, not
            installed OS proof. True KVM adds fixed POWER10 in the same record;
            explicit dedicated never-share CPUs are required and never rewritten.
            Roles reject explicit keylock before creation. Controller mode requires apply_partition_profile=False and rejects
            initial-profile I/O and profile-bearing assignments. No controller
            takeover occurs. Readback proves configuration, not KVM/NovaLink deployment.
        partition_type: Partition type: AIX/Linux or OS400. A Virtual I/O Server
            cannot be created here; use hmc_create_vios.
        partition_id: Optional numeric partition ID; the HMC assigns one when omitted.
        keylock: Optional initial keylock position: normal or manual.
        max_virtual_slots: Optional maximum number of virtual I/O slots.
        caller_token: Optional caller tracking reference embedded in the partition
            description as ``[caller <token>]`` after the ownership stamp (ADR 0064);
            1–64 printable ASCII characters, no whitespace or , = " [ ] \\.
        assignments: Declarative dedicated, direct SR-IOV, and vNIC requests.
        apply_partition_profile: When the HMC creates the partition through
            mksyscfg (its REST create was refused: HTTP 406, or a 400 REST0001
            payload rejection), apply the new
            default_profile so the partition has a current configuration; the
            ``apply_profile`` step reports it. False leaves it unapplied. A REST
            create sets the current configuration itself, so the step reports
            the apply as skipped.
        physical_io: Optional typed initial physical/pool and IBM i tagged-I/O
            configuration. No implicit console is chosen. Physical/direct SR-IOV
            DRCs and explicit hmc console are supported; unresolved virtual-slot
            selectors fail before effects (remaining linkage gap #638). C12 profile
            readback must succeed before application and later workflow steps.
        profile: Optional HMC connection profile, distinct from a logical-partition
            profile; uses the default when omitted.
    """
    if caller_token is not None:
        validate_caller_token(caller_token)
    validate_partition_type(partition_type)
    if start_settings is not None:
        if not isinstance(start_settings, LparStartSettings):
            raise TypeError("start_settings must be LparStartSettings")
        start_settings.validate_creation(partition_type)
    if creation_memory is not None and not isinstance(
        creation_memory, LparCreationMemory
    ):
        raise TypeError("creation_memory must be LparCreationMemory")
    validate_serial_input(virtual_serial_adapters, max_virtual_slots)
    if memory_layout is not None:
        if not isinstance(memory_layout, LparMemoryLayout):
            raise TypeError("memory_layout must be LparMemoryLayout")
        memory_layout.validate_creation(partition_type)
    validate_affinity_group_id(affinity_group_id)
    return with_client(
        lambda hmc: create_lpar(
            hmc,
            system_name_or_uuid,
            LparCreation(
                name,
                partition_type,
                resources,
                partition_id=partition_id,
                keylock=keylock,
                max_virtual_slots=max_virtual_slots,
                virtual_serial_adapters=virtual_serial_adapters,
                creation_memory=creation_memory
                if creation_memory is not None
                else LparCreationMemory(),
                caller_token=caller_token,
                physical_io=physical_io
                if physical_io is not None
                else LparPhysicalIO(),
                apply_profile=apply_partition_profile,
                hardware_mem_encryption=hardware_mem_encryption,
                hardware_mem_expansion=hardware_mem_expansion,
                memory_layout=memory_layout
                if memory_layout is not None
                else LparMemoryLayout(),
                affinity_group_id=affinity_group_id,
                start_settings=start_settings
                if start_settings is not None
                else LparStartSettings(),
                security_policy=security_policy
                if security_policy is not None
                else LparSecurityPolicy(),
                settings=settings if settings is not None else LparSettings(),
                vtpm_settings=vtpm_settings
                if vtpm_settings is not None
                else LparVtpmSettings(),
                reporting_settings=reporting_settings
                if reporting_settings is not None
                else LparReportingSettings(),
            ),
            assignments,
        ),
        profile=profile,
    )
