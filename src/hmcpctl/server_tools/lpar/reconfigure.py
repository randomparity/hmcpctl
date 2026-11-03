"""``hmc_reconfigure_lpar``: change processors, memory and name as one logical operation.

Each patch field runs only as the specialists it can use (ADR 0189 Decision 2, ADR 0314
Decision 6). A handler therefore needs the application's ``permits`` and ``authorize``
gates, so it is built by a factory, as ``hmc_power_lpar`` is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastmcp import FastMCP

from ...config import build_config
from ...operations.logical import engine
from ...operations.logical.engine import OperationRequest
from ...operations.logical.models import OperationRecord
from ...operations.logical.store import OperationRefused, connection_label
from ...operations.lpar.reconfigure import (
    RECONFIGURE_CLASSIFIERS,
    ReconfigurePatch,
    delegated_tools,
    reconfigure_body,
)
from ...tool_registry import (
    Authorize,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    authorized,
    validate_security,
)
from ..delegation import authorize_as

RECONFIGURE_TOOL_NAME = "hmc_reconfigure_lpar"
# "console" and not exhaustive, as hmc_power_lpar: a continuation names no target, so the
# delegated specialists carry the target bound (ADR 0189 Decision 2).
RECONFIGURE_SECURITY = ToolSecurity(
    effect="destructive",
    operation="lpar.reconfigure",
    target_kind="console",
    targets=(
        TargetSelector("lpar", "lpar_name_or_uuid", False),
        TargetSelector("managed_system", "system_name_or_uuid", False),
    ),
)
Continuation = Literal["none", "resume", "abandon"]
_REQUIRED = ("lpar_name_or_uuid", "system_name_or_uuid", "patch")


def _given(**values: Any) -> dict[str, Any]:
    """The arguments the caller supplied; ADR 0094 reads a blank string as absent."""
    return {
        key: value
        for key, value in values.items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }


def _effective(
    given: dict[str, Any], agent_id: str, request_id: str, continuation: str
) -> dict[str, Any]:
    """The arguments to authorize: the call's, over the record's on a continuation."""
    if continuation == "none":
        missing = [name for name in _REQUIRED if name not in given]
        if missing:
            raise ValueError(f"a new operation needs {', '.join(missing)}")
        return {"allow_disruption": False, "exclusive_writer_window": False, **given}
    recorded = engine.recorded_arguments(agent_id, request_id, RECONFIGURE_TOOL_NAME)
    if recorded is None:
        raise OperationRefused(
            "not_found",
            f"no operation has request_id {request_id}; omit continuation to start one",
        )
    return {**recorded, **given}


def reconfigure_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., OperationRecord]:
    """The unwrapped ``hmc_reconfigure_lpar`` handler bound to the served gates."""

    def hmc_reconfigure_lpar(
        request_id: str,
        lpar_name_or_uuid: str | None = None,
        system_name_or_uuid: str | None = None,
        patch: ReconfigurePatch | None = None,
        allow_disruption: bool | None = None,
        exclusive_writer_window: bool | None = None,
        continuation: Continuation = "none",
        wait_seconds: int = 60,
        profile: str | None = None,
    ) -> OperationRecord:
        """Change an LPAR's processors, memory or name, and say where each change lives.

        Durable (ADR 0190): the call returns within ``wait_seconds`` and the work
        continues; poll ``hmc_operation_status`` with ``request_id``. Every change is
        planned before any write. Each change in ``result.changes`` is ``live`` (applied
        to the running partition by DLPAR and saved in its default profile),
        ``profile`` (the partition is not activated; its profile and current
        configuration both hold it) or ``pending_activation`` (saved in the default
        profile only; it applies when ``result.activation``, the profile, is activated).
        A change is live only when the partition is running, RMC is active, the value
        is inside the running minimum and maximum, and it is an increase or
        ``allow_disruption`` is true. A value outside the profile's minimum or maximum
        widens that bound and is always ``pending_activation``. Nothing reboots the
        partition or changes its processor mode. Processors or memory need
        ``exclusive_writer_window=true``: your assertion that no other writer changes
        this partition or its profiles until the operation ends. ``name`` needs
        ``hmc_modify_lpar`` and ``hmc_rename_lpar``; ``memory_mib`` needs
        ``hmc_dlpar_mem`` and ``hmc_update_partition_profile``; ``processors`` needs
        ``hmc_dlpar_proc`` and ``hmc_update_partition_profile``: the policy must permit
        each and admit this partition, or nothing is recorded. An interrupted DLPAR
        write always needs attention: inspect the partition, then ``abandon``. The
        rename is written last; when unsure the HMC accepts a name, rename separately.

        Args:
            request_id: Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ -.
            lpar_name_or_uuid: PartitionName or UUID; required for a new operation.
            system_name_or_uuid: The managed system holding it; required for a new
                operation.
            patch: name, processors and/or memory_mib; omitted fields are unchanged.
                Required for a new operation.
            allow_disruption: Permit live removal of memory or processors (default
                false: a removal on a running partition is pending_activation).
            exclusive_writer_window: Required true when the patch has processors or
                memory_mib.
            continuation: none, resume or abandon; a continuation needs only
                request_id, and any other value given must match the operation's.
            wait_seconds: Seconds to wait before returning, 0-600.
            profile: HMC connection profile.
        """
        given = _given(
            lpar_name_or_uuid=lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            patch=None if patch is None else patch.model_dump(exclude_none=True),
            allow_disruption=allow_disruption,
            exclusive_writer_window=exclusive_writer_window,
        )
        config = build_config(profile=profile)
        agent_id = config.agent_id or "hmcpctl"
        effective = _effective(given, agent_id, request_id, continuation)
        model = ReconfigurePatch.model_validate(effective["patch"])
        if model.writes_profile and effective["exclusive_writer_window"] is not True:
            raise PermissionError(
                "processors and memory_mib write the partition's profile: assert "
                "exclusive_writer_window=true after coordinating other writers of this "
                "partition and its profiles (ADR 0242)"
            )
        for delegated in delegated_tools(model):
            if not permits(delegated):
                raise PermissionError(
                    f"{delegated} is not permitted by this server's access policy; "
                    f"{RECONFIGURE_TOOL_NAME} needs it for this patch (ADR 0189)"
                )
            authorize_as(
                tool_security,
                authorize,
                delegated,
                {
                    "lpar": effective["lpar_name_or_uuid"],
                    "managed_system": effective["system_name_or_uuid"],
                },
                profile,
            )
        request = OperationRequest(
            RECONFIGURE_TOOL_NAME,
            agent_id,
            connection_label(profile, tool=RECONFIGURE_TOOL_NAME),
            config.host,
            request_id,
            effective if continuation == "none" else given,
        )
        body = reconfigure_body(
            model,
            effective["allow_disruption"],
            effective["system_name_or_uuid"],
            effective["lpar_name_or_uuid"],
        )
        return engine.submit(
            request,
            body,
            classifiers=RECONFIGURE_CLASSIFIERS,
            continuation=continuation,
            wait_seconds=wait_seconds,
        )

    return hmc_reconfigure_lpar


def register_reconfigure_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_reconfigure_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(RECONFIGURE_TOOL_NAME):
        return
    handler = reconfigure_handler(tool_security, permits, authorize)
    validate_security(RECONFIGURE_SECURITY, handler)
    mcp.tool(
        authorized(RECONFIGURE_TOOL_NAME, RECONFIGURE_SECURITY, handler, authorize),
        annotations=annotations_for(RECONFIGURE_SECURITY.effect),
    )
