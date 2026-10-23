"""MCP adapter for the end-to-end LPAR provisioning operation."""

from __future__ import annotations

from ..._app import with_client
from ...documents.lpar import LparResources, PartitionType, validate_partition_type
from ...documents.lpar_io import LparPhysicalIO
from ...operations.affinity.rest import ProvisionAffinityAssessment
from ...operations.lpar.assignments import LparPcieAssignments
from ...operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStorage,
    provision_lpar,
    validate_provision_assignments,
)
from ...ssh.affinity import MinimumAffinityPolicy
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


# The VIOS this call mutates arrives one level below the signature —
# `storage.vios_uuid` — and is declared here as a nested selector (#260), so
# extraction, the audit record, and denial messages see it instead of only the
# managed system. It stays `exhaustive_targets=False`, so only
# `targets = "all-targets"` grants it (#1086): `assignments` names vNIC backing
# VIOSes in nested entries, dedicated-slot DRC indexes, and SR-IOV adapter and port
# ids, and no target kind can express those.
@tool(
    effect="mutate",
    operation="provision.lpar",
    target_kind="managed_system",
    extra_targets=(("vios", "storage.vios_uuid"),),
    exhaustive_targets=False,
)
def hmc_provision_lpar(
    system_name_or_uuid: str,
    name: str,
    adapters: ProvisionAdapters,
    storage: ProvisionStorage,
    resources: LparResources = LparResources(
        min_memory=256,
        desired_memory=4096,
        max_memory=8192,
        desired_vcpus=1,
        max_vcpus=2,
    ),
    partition_type: PartitionType = "AIX/Linux",
    power_on: bool = True,
    dry_run: bool = False,
    assignments: LparPcieAssignments = LparPcieAssignments(),
    caller_token: str | None = None,
    minimum_affinity_policy: MinimumAffinityPolicy | None = None,
    affinity_assessment: ProvisionAffinityAssessment | None = None,
    physical_io: LparPhysicalIO | None = None,
    profile: str | None = None,
) -> ProvisionResult:
    """Provision an LPAR with a virtual Ethernet adapter, vSCSI storage, and optional power-on.

    Args:
        system_name_or_uuid: Target managed-system name or UUID.
        name: Name for the new logical partition.
        adapters: Virtual Ethernet attachment settings.
        storage: VIOS-backed storage mapping settings.
        resources: Memory and processor settings for the partition.
        partition_type: Partition environment: AIX/Linux or OS400. A Virtual I/O
            Server cannot be created here; use hmc_create_vios.
        power_on: Power on the partition after configuration succeeds.
        dry_run: Validate preconditions without creating or changing resources.
        assignments: Declarative direct SR-IOV and vNIC requests. A dedicated-slot
            request is refused before any HMC call: power-on activates the current
            configuration, so the partition would not own a slot written to its
            profile. Create the partition, assign the slot with
            hmc_assign_dedicated_pcie_slot (or create-time
            ``assignments.dedicated``), then power on with that profile.
        caller_token: Optional caller tracking reference embedded in the partition
            description as ``[caller <token>]`` after the ownership stamp (ADR 0064);
            1–64 printable ASCII characters, no whitespace or , = " [ ] \\.
        minimum_affinity_policy: Optional POWER11 score and deliberately selected
            action. Omission preserves HMC defaults; ``fail`` is never implicit.
        affinity_assessment: Optional target-bound captured evidence and explicit
            warning or fail response. Assessment waits for successful activation;
            omission preserves asynchronous power-on behavior.
        physical_io: Optional typed initial physical/pool and IBM i tagged-I/O
            configuration. No implicit console is chosen. Physical/direct SR-IOV
            DRCs and explicit hmc console are supported; unresolved virtual-slot
            selectors fail before effects (remaining linkage gap #638). C12 profile
            readback must succeed before application and later workflow steps.
        profile: Optional TOML profile name; uses environment defaults when omitted.

    Returns:
        A structured result with resource_created, workflow_completed, lpar_uuid,
        dry_run, ownership_stamped, steps, warnings, and change_location fields.
        With ``caller_token``, ``ownership_stamped=True`` confirms both the ownership
        stamp and the caller segment landed (one combined write); ``False`` means both
        were lost; ``None`` means the stamp was skipped — the reason is in ``warnings``.
        ``change_location`` reports where the network and storage changes
        just made now live — the partition's CurrentProfileSync and whether a later
        ``power-on --partition-profile`` would keep them; ``None`` when no adapter or
        mapping step ran, or when the read itself failed (see ``warnings``).
    """
    validate_partition_type(partition_type)
    validate_provision_assignments(assignments)
    return with_client(
        lambda hmc: provision_lpar(
            hmc,
            system_name_or_uuid=system_name_or_uuid,
            request=ProvisionRequest(
                name=name,
                adapters=adapters,
                storage=storage,
                resources=resources,
                partition_type=partition_type,
                power_on=power_on,
                dry_run=dry_run,
                assignments=assignments,
                caller_token=caller_token,
                physical_io=physical_io
                if physical_io is not None
                else LparPhysicalIO(),
                minimum_affinity_policy=minimum_affinity_policy,
                affinity_assessment=affinity_assessment,
            ),
        ),
        profile=profile,
    )
