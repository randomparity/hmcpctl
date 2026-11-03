"""``hmc_plan_lpar``: the primary read-only LPAR planning tool (ADR 0198).

Planning runs only when the policy permits every delegated tool it reads, and
each read is then authorized as that tool for its system or VIOS through the
served ``dispatch_authorizer``. A handler therefore needs the application's
``permits`` and ``authorize`` gates; that is why it is built by a factory rather
than a ``tool_module`` decorator, as ``hmc_inventory`` is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastmcp import FastMCP

from ..._app import with_client
from ...authorization.connection_scope import ConnectionScopeError
from ...authorization.target_scope import TargetScopeError
from ...documents.lpar import LparResources, PartitionType
from ...operations.affinity.rest import ProvisionAffinityAssessment
from ...operations.logical.store import connection_label
from ...operations.lpar.assignments import LparPcieAssignments
from ...operations.lpar.install_media import Boot, LparInstall
from ...operations.lpar.plan import (
    LparPlan,
    Placement,
    PlanAdapters,
    PlanAdmit,
    PlanRequest,
    PlanStorage,
    check_request,
    plan_lpar,
    required_tools,
)
from ...ssh.affinity import MinimumAffinityPolicy
from ...tool_registry import (
    Authorize,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    authorized,
    validate_security,
)
from ..delegation import authorize_as

PLAN_TOOL_NAME = "hmc_plan_lpar"
# "console" and not exhaustive, as hmc_inventory: placement names no system, and the
# VIOS and volume group the plan reads sit below the signature, so the delegated
# tools carry the target bound (ADR 0198 Decision 3). The selectors are declared so
# the audit record and denials name them, as hmc_provision_lpar's are.
PLAN_SECURITY = ToolSecurity(
    effect="read",
    operation="lpar.plan",
    target_kind="console",
    targets=(
        TargetSelector("managed_system", "system_name_or_uuid", False),
        TargetSelector("vios", "vios_uuid", False, container="storage"),
    ),
)


def _admitter(
    tool_security: Mapping[str, ToolSecurity],
    authorize: Authorize,
    profile: str | None,
) -> PlanAdmit:
    def admit(name: str, system: str | None, vios: str | None) -> str | None:
        by_kind = {"managed_system": system, "vios": vios}
        try:
            authorize_as(tool_security, authorize, name, by_kind, profile)
        except (TargetScopeError, ConnectionScopeError) as exc:
            return str(exc)
        return None

    return admit


def plan_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., LparPlan]:
    """The unwrapped ``hmc_plan_lpar`` handler bound to the served gates."""

    def hmc_plan_lpar(
        name: str,
        adapters: PlanAdapters,
        storage: PlanStorage,
        system_name_or_uuid: str | None = None,
        placement: Placement | None = None,
        resources: LparResources = LparResources(
            min_memory=256,
            desired_memory=4096,
            max_memory=8192,
            desired_vcpus=1,
            max_vcpus=2,
        ),
        partition_type: PartitionType = "AIX/Linux",
        assignments: LparPcieAssignments = LparPcieAssignments(),
        caller_token: str | None = None,
        minimum_affinity_policy: MinimumAffinityPolicy | None = None,
        affinity_assessment: ProvisionAffinityAssessment | None = None,
        power_on: bool | None = None,
        boot: Boot = "immediate",
        install: LparInstall | None = None,
        exclusive_writer_window: bool = False,
        profile: str | None = None,
    ) -> LparPlan:
        """Plan provisioning an LPAR on one connection: fit, targets, blockers and changes.

        Nothing is written or reserved; capacity is observed and may change before
        provisioning. Planning needs every tool it reads: ``hmc_list_lpars``,
        ``hmc_capacity_report``, ``hmc_list_virtual_networks``, ``hmc_list_vios`` and
        ``hmc_list_volume_groups``, plus ``hmc_list_systems`` when ``placement``
        enumerates and ``hmc_get_vios_storage_detail`` for existing storage. A withheld
        one refuses the call, naming it. Each read is then authorized as its tool for its
        system or VIOS (VIOSes by UUID), and writes its own audit record, so a placement
        call can write a few hundred. A denied target becomes a ``denied`` blocker and
        is not read; ``hmc_capacity_report`` needs an ``all-targets`` grant. Placement
        evaluates at most 16 systems and selects the tightest fit with no blocker.
        ``plan_digest`` binds the request and the selected targets; pass it to
        hmc_provision_lpar as ``expected_plan_digest`` to refuse a stale plan.

        Args:
            name: Name for the new logical partition.
            adapters: The client virtual Ethernet adapter.
            storage: The storage to map; ``capacity_mib`` creates a new virtual disk.
            system_name_or_uuid: The managed system; exactly one of this and placement.
            placement: Choose the system from 1 to 16 selectors, or omit
                ``systems`` to enumerate the connection's systems.
            resources: Memory and processor settings for the partition.
            partition_type: AIX/Linux or OS400.
            assignments: Dedicated, direct SR-IOV, and vNIC requests.
            caller_token: Optional caller tracking reference (ADR 0064).
            minimum_affinity_policy: Optional POWER11 minimum-affinity policy.
            affinity_assessment: Optional target-bound affinity evidence; needs
                ``system_name_or_uuid``.
            power_on: Power on after configuration; omitted means true without
                ``install``. With ``install``, ``boot`` governs power-on.
            boot: ``immediate``, or ``deferred`` to stop before installer power-on.
            install: Optional operating-system installation (ADR 0194).
            exclusive_writer_window: Required true with ``install``: no other writer
                changes these VIOSes until provisioning ends.
            profile: HMC connection profile.
        """
        request = check_request(
            PlanRequest(
                name=name,
                adapters=adapters,
                storage=storage,
                resources=resources,
                partition_type=partition_type,
                system_name_or_uuid=system_name_or_uuid,
                placement=placement,
                assignments=assignments,
                caller_token=caller_token,
                minimum_affinity_policy=minimum_affinity_policy,
                affinity_assessment=affinity_assessment,
                power_on=power_on,
                boot=boot,
                install=install,
                exclusive_writer_window=exclusive_writer_window,
            )
        )
        for tool in required_tools(request):
            if not permits(tool):
                raise PermissionError(
                    f"{tool} is not permitted by this server's access policy; "
                    f"{PLAN_TOOL_NAME} needs it (ADR 0198)"
                )
        admit = _admitter(tool_security, authorize, profile)
        connection = connection_label(profile, tool=PLAN_TOOL_NAME)
        return with_client(
            lambda hmc: plan_lpar(
                hmc, connection=connection, admit=admit, request=request
            ),
            profile=profile,
        )

    return hmc_plan_lpar


def register_plan_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_plan_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(PLAN_TOOL_NAME):
        return
    handler = plan_handler(tool_security, permits, authorize)
    validate_security(PLAN_SECURITY, handler)
    mcp.tool(
        authorized(PLAN_TOOL_NAME, PLAN_SECURITY, handler, authorize),
        annotations=annotations_for(PLAN_SECURITY.effect),
    )
