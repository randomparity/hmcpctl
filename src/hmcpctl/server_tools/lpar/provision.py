"""``hmc_provision_lpar``: provision an LPAR as one journaled logical operation (ADR 0313).

Each write runs only as the specialist whose operation it performs (ADR 0189
Decision 2), so the handler needs the application's ``permits`` and ``authorize``
gates and is built by a factory, as ``hmc_power_lpar`` is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastmcp import FastMCP

from ..._app import with_client
from ...config import build_config
from ...documents.lpar import LparResources, PartitionType
from ...documents.lpar_io import LparPhysicalIO
from ...operations.affinity.rest import ProvisionAffinityAssessment
from ...operations.logical import engine, store
from ...operations.logical.engine import OperationRequest
from ...operations.logical.store import OperationRefused, connection_label
from ...operations.lpar.assignments import LparPcieAssignments
from ...operations.lpar.plan import (
    PlanAdapters,
    PlanRequest,
    PlanStorage,
    check_request,
    plan_lpar,
    required_tools,
)
from ...operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStorage,
    preview_provision,
)
from ...operations.lpar.provision_operation import (
    DEFAULT_ARGUMENTS,
    json_value,
    provision_body,
    provision_classifiers,
    request_from_arguments,
    result_from_record,
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
from .plan import PLAN_TOOL_NAME, _admitter

PROVISION_TOOL_NAME = "hmc_provision_lpar"
# "console" and not exhaustive, as hmc_power_lpar: a continuation names no target, so
# the delegated specialists carry the target bound (ADR 0189 Decision 2). The
# selectors are declared so the audit record and denials name them.
PROVISION_SECURITY = ToolSecurity(
    effect="mutate",
    operation="provision.lpar",
    target_kind="console",
    targets=(
        TargetSelector("managed_system", "system_name_or_uuid", False),
        TargetSelector("vios", "vios_uuid", False, container="storage"),
    ),
)
Continuation = Literal["none", "resume", "abandon"]
_REQUIRED = ("system_name_or_uuid", "name", "adapters", "storage")


def delegated_tools(request: ProvisionRequest) -> tuple[str, ...]:
    """The specialists whose writes *request* performs (ADR 0313 Decision 8)."""
    optional = (
        ("hmc_create_virtual_disk", request.storage.capacity_mib is not None),
        (
            "hmc_set_minimum_affinity_policy",
            request.minimum_affinity_policy is not None,
        ),
        ("hmc_assign_sriov_logical_port", bool(request.assignments.sriov)),
        ("hmc_add_vnic", bool(request.assignments.vnics)),
        ("hmc_power_on_lpar", request.power_on),
    )
    return (
        "hmc_create_lpar",
        "hmc_add_network_adapter",
        "hmc_map_storage_to_lpar",
        *(name for name, needed in optional if needed),
    )


def _given(**values: Any) -> dict[str, Any]:
    """The arguments the caller supplied, as JSON; ADR 0094 reads "" as absent."""
    return {
        key: json_value(value)
        for key, value in values.items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }


def _effective(
    given: dict[str, Any], agent_id: str, request_id: str, continuation: str
) -> tuple[dict[str, Any], bool]:
    """The arguments to run and authorize, and whether a record already exists."""
    recorded = engine.recorded_arguments(agent_id, request_id, PROVISION_TOOL_NAME)
    if continuation != "none":
        if recorded is None:
            # Refused here, not by submit: nothing may be admitted without the delegated
            # check, even if a concurrent call records this request_id meanwhile.
            raise OperationRefused(
                "not_found",
                f"no operation has request_id {request_id}; omit continuation to start one",
            )
        return {**recorded, **given}, True
    missing = [name for name in _REQUIRED if name not in given]
    if missing:
        raise ValueError(f"a new operation needs {', '.join(missing)}")
    return {**DEFAULT_ARGUMENTS, **given}, recorded is not None


def _authorize(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
    system: str,
    request: ProvisionRequest,
    profile: str | None,
) -> None:
    by_kind = {
        "managed_system": system,
        "lpar": request.name,
        "vios": request.storage.vios_uuid,
    }
    for name in delegated_tools(request):
        if not permits(name):
            raise PermissionError(
                f"{name} is not permitted by this server's access policy; "
                f"{PROVISION_TOOL_NAME} needs it for this request (ADR 0189)"
            )
        if name == "hmc_add_vnic":
            # Each vNIC is backed by its own VIOS, not the storage VIOS.
            for vnic in request.assignments.vnics:
                backing = {**by_kind, "vios": vnic.backing.vios_name}
                authorize_as(tool_security, authorize, name, backing, profile)
            continue
        authorize_as(tool_security, authorize, name, by_kind, profile)


def _check_plan(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
    system: str,
    request: ProvisionRequest,
    expected: str,
    profile: str | None,
) -> None:
    """Refuse unless a fresh plan of this request has digest *expected* (ADR 0313)."""
    if request.physical_io.requested:
        raise ValueError(
            "expected_plan_digest cannot cover physical_io, which hmc_plan_lpar does not plan"
        )
    storage = request.storage
    plan_request = check_request(
        PlanRequest(
            name=request.name,
            adapters=PlanAdapters(request.adapters.port_vlan_id),
            storage=PlanStorage(
                storage.storage_name,
                storage.kind,
                storage.vios_uuid,
                storage.vg_uuid,
                storage.capacity_mib,
            ),
            resources=request.resources,
            partition_type=request.partition_type,
            system_name_or_uuid=system,
            assignments=request.assignments,
            caller_token=request.caller_token,
            minimum_affinity_policy=request.minimum_affinity_policy,
            affinity_assessment=request.affinity_assessment,
            power_on=request.power_on,
        )
    )
    for tool in required_tools(plan_request):
        if not permits(tool):
            raise PermissionError(
                f"{tool} is not permitted by this server's access policy; checking "
                "expected_plan_digest re-runs hmc_plan_lpar, which needs it (ADR 0198)"
            )
    admit = _admitter(tool_security, authorize, profile)
    connection = connection_label(profile, tool=PLAN_TOOL_NAME)
    plan = with_client(
        lambda hmc: plan_lpar(
            hmc, connection=connection, admit=admit, request=plan_request
        ),
        profile=profile,
    )
    if plan.plan_digest != expected:
        raise OperationRefused(
            "plan_digest_mismatch",
            "the request or its targets no longer match expected_plan_digest; run "
            "hmc_plan_lpar again",
        )


def provision_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., ProvisionResult]:
    """The unwrapped ``hmc_provision_lpar`` handler bound to the served gates."""

    def hmc_provision_lpar(
        request_id: str,
        system_name_or_uuid: str | None = None,
        name: str | None = None,
        adapters: ProvisionAdapters | None = None,
        storage: ProvisionStorage | None = None,
        resources: LparResources | None = None,
        partition_type: PartitionType | None = None,
        power_on: bool | None = None,
        dry_run: bool = False,
        assignments: LparPcieAssignments | None = None,
        caller_token: str | None = None,
        minimum_affinity_policy: MinimumAffinityPolicy | None = None,
        affinity_assessment: ProvisionAffinityAssessment | None = None,
        physical_io: LparPhysicalIO | None = None,
        expected_plan_digest: str | None = None,
        continuation: Continuation = "none",
        wait_seconds: int = 60,
        profile: str | None = None,
    ) -> ProvisionResult:
        """Provision an LPAR with a virtual Ethernet adapter, vSCSI storage, and optional power-on.

        Durable (ADR 0190, ADR 0313): the call returns within ``wait_seconds`` and the
        work continues; poll ``hmc_operation_status`` with ``request_id``. A repeat
        with the same arguments returns the same operation and never creates a
        second partition. Each write runs as its specialist: ``hmc_create_lpar``,
        ``hmc_add_network_adapter`` and ``hmc_map_storage_to_lpar`` always, plus
        ``hmc_create_virtual_disk`` with ``storage.capacity_mib``,
        ``hmc_set_minimum_affinity_policy``, ``hmc_assign_sriov_logical_port`` and
        ``hmc_add_vnic`` for their inputs, and ``hmc_power_on_lpar`` with
        ``power_on``. The policy must permit each and admit its targets, or the call
        is refused and nothing is recorded. The ownership stamp is required: a stamp
        that does not land makes the operation ``failed``. A later step's error pauses
        it as ``needs_attention`` with the steps so far; ``continuation=resume``
        checks each interrupted write live before continuing. A clean run ends
        ``completed``; no partition profile is written. ``dry_run`` validates every
        precondition and records nothing. No rollback is attempted.

        Args:
            request_id: Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ -.
            system_name_or_uuid: Target managed-system name or UUID; required for a
                new operation.
            name: Name for the new logical partition; required for a new operation.
            adapters: Virtual Ethernet attachment settings; required for a new
                operation.
            storage: VIOS-backed storage; required for a new operation. With
                ``capacity_mib`` (and ``vg_uuid``) the disk is created and owned by
                the operation; without it existing storage is mapped.
            resources: Memory and processor settings for the partition.
            partition_type: Partition environment: AIX/Linux (default) or OS400. A
                Virtual I/O Server cannot be created here; use hmc_create_vios.
            power_on: Power on the partition after configuration succeeds (default
                true).
            dry_run: Validate preconditions without recording or changing anything.
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
                warning or fail response; needs ``power_on``. Assessment waits for
                successful activation.
            physical_io: Optional typed initial physical/pool and IBM i tagged-I/O
                configuration. No implicit console is chosen. Physical/direct SR-IOV
                DRCs and explicit hmc console are supported; unresolved virtual-slot
                selectors fail before effects (remaining linkage gap #638). C12 profile
                readback must succeed before application and later workflow steps.
            expected_plan_digest: ``plan_digest`` from hmc_plan_lpar; a new operation
                is refused unless a fresh plan of this request matches it.
            continuation: none, resume or abandon; a continuation needs only
                request_id, and any other value given must match the operation's.
            wait_seconds: Seconds to wait before returning, 0-600.
            profile: Optional TOML profile name; uses environment defaults when omitted.

        Returns:
            ``ProvisionResult``: once the operation completes, the steps and fields of
            the run; otherwise fields rebuilt from its journaled effects. ``operation``
            carries the operation record (null for a dry run).
        """
        store.validate_request_id(request_id)
        if dry_run and continuation != "none":
            raise ValueError("dry_run cannot continue an operation")
        given = _given(
            system_name_or_uuid=system_name_or_uuid,
            name=name,
            adapters=adapters,
            storage=storage,
            resources=resources,
            partition_type=partition_type,
            power_on=power_on,
            assignments=assignments,
            caller_token=caller_token,
            minimum_affinity_policy=minimum_affinity_policy,
            affinity_assessment=affinity_assessment,
            physical_io=physical_io,
            expected_plan_digest=expected_plan_digest,
        )
        config = build_config(profile=profile)
        agent_id = config.agent_id or "hmcpctl"
        effective, exists = _effective(given, agent_id, request_id, continuation)
        system, request = request_from_arguments(effective)
        _authorize(tool_security, permits, authorize, system, request, profile)
        digest = effective.get("expected_plan_digest")
        if digest is not None and not exists:
            _check_plan(
                tool_security, permits, authorize, system, request, digest, profile
            )
        if dry_run or not exists:
            # Preconditions refuse a new operation before anything is recorded.
            preview = with_client(
                lambda hmc: preview_provision(hmc, system, request), profile=profile
            )
            if dry_run:
                return preview
        record = engine.submit(
            OperationRequest(
                PROVISION_TOOL_NAME,
                agent_id,
                connection_label(profile, tool=PROVISION_TOOL_NAME),
                config.host,
                request_id,
                effective if continuation == "none" else given,
            ),
            provision_body(system, request),
            classifiers=provision_classifiers(request),
            continuation=continuation,
            wait_seconds=wait_seconds,
        )
        return result_from_record(record)

    return hmc_provision_lpar


def register_provision_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_provision_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(PROVISION_TOOL_NAME):
        return
    handler = provision_handler(tool_security, permits, authorize)
    validate_security(PROVISION_SECURITY, handler)
    mcp.tool(
        authorized(PROVISION_TOOL_NAME, PROVISION_SECURITY, handler, authorize),
        annotations=annotations_for(PROVISION_SECURITY.effect),
    )
