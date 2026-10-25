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
from ...documents.lpar_hardware_memory import HardwareMemoryFlag
from ...documents.lpar_io import LparPhysicalIO
from ...documents.lpar_security_policy import LparSecurityPolicy
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
        resources: Memory and processor assignments for the new partition.
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
                caller_token=caller_token,
                physical_io=physical_io
                if physical_io is not None
                else LparPhysicalIO(),
                apply_profile=apply_partition_profile,
                hardware_mem_encryption=hardware_mem_encryption,
                hardware_mem_expansion=hardware_mem_expansion,
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
