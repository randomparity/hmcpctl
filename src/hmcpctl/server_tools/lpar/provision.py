"""``hmc_provision_lpar``: provision an LPAR as one journaled logical operation (ADR 0313).

Each write runs only as the specialist whose operation it performs (ADR 0189
Decision 2), so the handler needs the application's ``permits`` and ``authorize``
gates and is built by a factory, as ``hmc_power_lpar`` is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastmcp import FastMCP

from hmcpctl.operations.lpar.provisioning.core import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStorage,
    preview_provision,
)
from hmcpctl.operations.lpar.provisioning.media import Boot, LparInstall
from hmcpctl.operations.lpar.provisioning.operation import (
    DEFAULT_ARGUMENTS,
    json_value,
    provision_body,
    provision_classifiers,
    request_from_arguments,
    result_from_record,
)

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
from .continuation import continuation_arguments
from .plan import PLAN_TOOL_NAME, _admitter

PROVISION_TOOL_NAME = "hmc_provision_lpar"
# "console" and not exhaustive, as hmc_power_lpar: a continuation names no target, so
# the delegated specialists carry the target bound (ADR 0189 Decision 2), the VIOS
# included. No nested storage.vios_uuid selector: a continuation omits storage, and a
# selector in a None container is unreadable, which denies even under all-targets.
PROVISION_SECURITY = ToolSecurity(
    effect="destructive",
    operation="provision.lpar",
    target_kind="console",
    targets=(TargetSelector("managed_system", "system_name_or_uuid", False),),
)
Continuation = Literal["none", "resume", "abandon", "boot"]
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
        ("hmc_assign_dedicated_pcie_slot", bool(request.assignments.dedicated)),
        # With install, the installer boot powers on (ADR 0326 Decision 7).
        ("hmc_power_on_lpar", request.power_on or request.install is not None),
        ("hmc_upload_iso", request.install is not None),
        ("hmc_mount_optical_media", request.install is not None),
        ("hmc_set_lpar_boot_order", request.install is not None),
        ("hmc_read_lpar_refcodes", request.install is not None),
    )
    return (
        "hmc_create_lpar",
        "hmc_add_network_adapter",
        "hmc_map_storage_to_lpar",
        "hmc_save_current_partition_profile",
        "hmc_set_default_partition_profile",
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
        return continuation_arguments(recorded, given, request_id), True
    missing = [name for name in _REQUIRED if name not in given]
    if missing:
        raise ValueError(f"a new operation needs {', '.join(missing)}")
    # With install, boot governs power-on (H1 spec), so the default is off.
    defaults = {
        **DEFAULT_ARGUMENTS,
        **({"power_on": False} if "install" in given else {}),
    }
    return {**defaults, **given}, recorded is not None


def _require_window(dry_run: bool, continuation: str, window: bool | None) -> None:
    """Refuse a call that would write without the caller's writer window (ADR 0313)."""
    if dry_run or continuation == "abandon" or window is True:
        return
    raise ValueError(
        "exclusive_writer_window=true is required: provision writes a partition profile "
        "and the default-profile setting (ADR 0242, ADR 0313); pass it once no other "
        "writer changes this partition's profiles until the operation ends"
        + (
            f"; a {continuation} asserts it again"
            if continuation in ("resume", "boot")
            else ""
        )
    )


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
    window: bool,
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
            adapters=PlanAdapters(request.adapters.port_vlan_id, request.adapters.mac),
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
            power_on=None if request.install is not None else request.power_on,
            boot=request.boot,
            install=request.install,
            exclusive_writer_window=window,
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
        install: LparInstall | None = None,
        boot: Boot | None = None,
        exclusive_writer_window: bool | None = None,
        expected_plan_digest: str | None = None,
        continuation: Continuation = "none",
        wait_seconds: int = 60,
        profile: str | None = None,
    ) -> ProvisionResult:
        """Provision an LPAR; creating its disk can lose concurrent VolumeGroup changes.

        Includes virtual Ethernet, vSCSI storage and optional power-on. Creating a
        virtual disk writes the whole VolumeGroup. The HMC may ignore If-Match
        (observed on V10R3 M1060), so callers must serialize writers to that group
        for the entire operation. The profile-only ``exclusive_writer_window``
        assertion does not establish VolumeGroup coordination.

        Durable (ADR 0190, ADR 0313): the call returns within ``wait_seconds`` and the
        work continues; poll ``hmc_operation_status`` with ``request_id``. A repeat
        with the same arguments returns the same operation and never creates a
        second partition. Each write runs as its specialist: ``hmc_create_lpar``,
        ``hmc_add_network_adapter`` and ``hmc_map_storage_to_lpar`` always, plus
        ``hmc_create_virtual_disk`` with ``storage.capacity_mib``,
        ``hmc_set_minimum_affinity_policy``, ``hmc_assign_sriov_logical_port``,
        ``hmc_add_vnic`` and ``hmc_assign_dedicated_pcie_slot`` for their inputs,
        and ``hmc_power_on_lpar`` with
        ``power_on``. The policy must permit each and admit its targets, or the call
        is refused and nothing is recorded; the profile writes also need
        ``hmc_save_current_partition_profile`` and ``hmc_set_default_partition_profile``.
        The ownership stamp is required: a stamp that does not land makes the operation
        ``failed``. A later step's error pauses
        it as ``needs_attention`` with the steps so far; ``continuation=resume``
        checks each interrupted write live before continuing. After the adapters and
        storage, provision saves the current configuration into a new partition profile
        ``hmcpctl_<12 hex>``, reads it back (memory, processors, adapters), makes it the
        default, and with ``power_on`` activates from it; without ``power_on`` (and
        without ``install``) it applies that profile without activation, so it is also
        the last-activated profile ``hmc_reconfigure_lpar`` requires (ADR 0331; this
        apply uses the SSH transport); only then does the run end ``configured``. It needs HMC 10.3.1060 or later. ``dry_run`` validates every
        precondition and records nothing. No rollback is attempted.

        With ``install`` (prepared media; ADR 0191, ADR 0319) the
        producer result must bind the request and the pinned ``adapters.mac``, field by
        field, or the call is refused. After the adapters and the new disk, the run binds
        the result again against the created adapter's MAC, uploads the media as
        ``hmcpctl_<12 hex>.iso`` into the storage VIOS's one media repository only if the
        download matches the bound SHA-256 and size, and mounts it. Only then does it save,
        read back (including the media's vSCSI adapter) and default the profile. It then
        writes the boot order, media first. With ``boot=deferred`` it pauses
        ``ready_to_boot`` with the partition powered off, and ``continuation=boot`` later
        boots it; ``boot=immediate`` boots in the same call (ADR 0326). The boot re-checks
        that the partition is powered off, the media mapped and listed, the boot order and
        the adapter MAC unchanged, and the owned disk its only disk, or pauses
        ``needs_attention`` without powering on. It records the newest reference code,
        activates the saved profile once, and reads reference codes every 15 seconds
        until ``wait_seconds`` passes. ``boot_started`` needs a newer code in the
        boot-started set, ``CA00E105`` (firmware passing control to the boot image, ADR
        0328), which natively came about 130 seconds after power-on; a wait that ends
        first is ``needs_attention`` with the newer codes it saw, and ``resume`` polls
        again and never powers on twice. A mismatch ends ``failed``; nothing is rebuilt or
        overwritten. These writes also need ``hmc_upload_iso``,
        ``hmc_mount_optical_media``, ``hmc_set_lpar_boot_order``, ``hmc_power_on_lpar``
        and ``hmc_read_lpar_refcodes``. ``unverified`` lists what is not checked.

        Built media (``media.mode=built``, ADR 0324) instead takes no ``adapters.mac``
        and needs a partition name of 1-32 characters matching ``[a-z][a-z0-9-]*``,
        not ending in ``-``. After the disk, the run writes an iso-chain-loader target
        request with the created adapter's MAC and the operation id, runs the build
        entry the server's operator configured (``HMC_ISO_BUILD_ENTRY``), and binds the
        result it prints, including its allowlisted ``url``; the upload then follows
        as for prepared media. A caller cannot supply a builder. A failed or unbound
        build ends ``failed``; an interrupted one pauses and is never run twice.

        Args:
            request_id: Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ -.
            system_name_or_uuid: Target managed-system name or UUID; required for a
                new operation.
            name: Name for the new logical partition; required for a new operation.
            adapters: Virtual Ethernet attachment settings; required for a new
                operation. ``mac`` pins the adapter MAC for prepared install media.
            storage: VIOS-backed storage; required for a new operation. With
                ``capacity_mib`` (and ``vg_uuid``) the disk is created and owned by
                the operation; without it existing storage is mapped.
            resources: Memory and processor settings for the partition.
            partition_type: Partition environment: AIX/Linux (default) or OS400. A
                Virtual I/O Server cannot be created here; use hmc_create_vios.
            power_on: Power on the partition after configuration succeeds (default
                true).
            dry_run: Validate preconditions without recording or changing anything.
            assignments: Declarative dedicated-slot, direct SR-IOV and vNIC requests.
                A dedicated slot must name ``default_profile``; provision writes it
                into the profile it saves, after the readback and before that
                profile becomes the default. A slot missing from the inventory, owned,
                or listed by a profile is refused before anything is recorded. The slot
                is written not required and ownership is not re-read after activation,
                so a slot another partition takes meanwhile is left out; confirm with
                hmc_list_dedicated_pcie_slots.
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
                DRCs and explicit hmc console are supported; virtual-slot selectors
                fail before effects, because provisioning declares no vSCSI client to
                bind them to (ADR 0382). C12 profile readback must succeed before
                application and later workflow steps.
            install: Optional operating-system installation: ``media.mode=prepared``
                with an iso-chain-media-v1 producer result plus an allowlisted URL, or
                ``media.mode=built`` with ``ssh_authorized_keys`` and ``login_user``
                for the server's configured build entry. Needs
                ``storage.capacity_mib``, and refuses ``power_on``: ``boot`` governs it.
            boot: ``immediate`` (default) boots the installer in the same call;
                ``deferred`` stops at ``ready_to_boot`` with the media mounted and the
                boot order set, for ``continuation=boot``. ``deferred`` needs ``install``.
            exclusive_writer_window: Required true for every call that may write (a new
                operation, ``resume`` or ``boot``): the caller asserts no other writer
                changes this partition's profiles until the operation ends (ADR 0242).
            expected_plan_digest: ``plan_digest`` from hmc_plan_lpar; a new operation
                is refused unless a fresh plan of this request matches it.
            continuation: none, resume or abandon, or boot for a ``ready_to_boot``
                operation; a continuation needs only request_id, and any other value
                given must match the operation's.
            wait_seconds: Seconds to wait before returning, 0-600, counted from the
                call's start; an installer boot reads reference codes until then, so
                reaching ``boot_started`` in one call needs time for creation, upload
                and about 130 seconds after power-on.
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
            install=install,
            boot=boot,
            exclusive_writer_window=exclusive_writer_window,
            expected_plan_digest=expected_plan_digest,
        )
        config = build_config(profile=profile)
        agent_id = config.agent_id or "hmcpctl"
        _require_window(dry_run, continuation, exclusive_writer_window)
        effective, exists = _effective(given, agent_id, request_id, continuation)
        system, request = request_from_arguments(effective)
        _authorize(tool_security, permits, authorize, system, request, profile)
        digest = effective.get("expected_plan_digest")
        if digest is not None and not exists:
            _check_plan(
                tool_security,
                permits,
                authorize,
                system,
                request,
                digest,
                profile,
                effective.get("exclusive_writer_window") is True,
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
            provision_body(system, request, wait_seconds=wait_seconds),
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
