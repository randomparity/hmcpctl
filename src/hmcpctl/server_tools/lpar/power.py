"""``hmc_power_lpar``: start, stop or restart an LPAR as a logical operation (ADR 0199).

Each action runs only as the specialist it uses (ADR 0189 Decision 2): ``start`` as
``hmc_power_on_lpar``, ``stop`` and ``restart`` as ``hmc_power_off_lpar``. A handler
therefore needs the application's ``permits`` and ``authorize`` gates, so it is built
by a factory, as ``hmc_plan_lpar`` is.
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
from ...operations.lpar.power import (
    DELEGATED,
    PowerAction,
    PowerMode,
    check_inputs,
    power_body,
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

POWER_TOOL_NAME = "hmc_power_lpar"
# "console" and not exhaustive, as hmc_plan_lpar: a continuation names no target, so the
# delegated specialist carries the target bound (ADR 0189 Decision 2). The selectors are
# declared so the audit record and denials name them.
POWER_SECURITY = ToolSecurity(
    effect="destructive",
    operation="lpar.power",
    target_kind="console",
    targets=(
        TargetSelector("lpar", "lpar_name_or_uuid", False),
        TargetSelector("managed_system", "system_name_or_uuid", False),
    ),
)
Continuation = Literal["none", "resume", "abandon"]


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
        missing = [
            name
            for name in ("lpar_name_or_uuid", "system_name_or_uuid", "action")
            if name not in given
        ]
        if missing:
            raise ValueError(f"a new operation needs {', '.join(missing)}")
        return {"mode": "graceful", **given}
    recorded = engine.recorded_arguments(agent_id, request_id, POWER_TOOL_NAME)
    if recorded is None:
        # Refused here, not by submit: nothing may be admitted without the delegated
        # check, even if a concurrent call records this request_id meanwhile.
        raise OperationRefused(
            "not_found",
            f"no operation has request_id {request_id}; omit continuation to start one",
        )
    return {**recorded, **given}


def power_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., OperationRecord]:
    """The unwrapped ``hmc_power_lpar`` handler bound to the served gates."""

    def hmc_power_lpar(
        request_id: str,
        lpar_name_or_uuid: str | None = None,
        system_name_or_uuid: str | None = None,
        action: PowerAction | None = None,
        mode: PowerMode | None = None,
        continuation: Continuation = "none",
        wait_seconds: int = 60,
        profile: str | None = None,
    ) -> OperationRecord:
        """Start, stop or restart one LPAR, wait a bounded time, and verify its state.

        Durable (ADR 0190): the call returns within ``wait_seconds`` and the work
        continues; poll ``hmc_operation_status`` with ``request_id``. A repeat with the
        same arguments returns the same operation. ``start`` needs
        ``hmc_power_on_lpar`` and ``stop`` / ``restart`` need ``hmc_power_off_lpar``:
        the policy must permit that tool and admit this partition for it, or the call
        is refused and nothing is recorded. ``mode`` ``graceful`` asks the operating
        system (PowerOff ``osshutdown``, RMC must be active); ``immediate`` must be
        stated and is never chosen for you, not even after a timeout. A partition
        already in the requested state completes with ``already_in_state`` true and no
        job. The final ``result`` carries ``already_in_state``, ``observed_state``
        (host power state only, not guest readiness) and ``job_id``. A job not done in
        300 seconds, or a state that does not settle in 120 more, pauses as
        ``needs_attention``; ``continuation=resume`` re-checks without submitting again.
        A completed restart means its job succeeded and the partition reads activated,
        not that a cycle was observed; a restart whose job the HMC no longer has pauses
        on every resume and needs ``abandon``. A job that ends with warnings or is
        cancelled makes the operation failed though it may have acted: inspect first.
        An interrupted restart always needs attention: inspect the partition, then
        ``abandon``. Crash with dump is ``hmc_dump_restart_lpar``, not this tool.

        Args:
            request_id: Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ -.
            lpar_name_or_uuid: PartitionName or UUID; required for a new operation.
            system_name_or_uuid: The managed system holding it; required for a new
                operation.
            action: start, stop or restart; required for a new operation.
            mode: graceful (default) or immediate, for stop and restart only.
            continuation: none, resume or abandon; a continuation needs only
                request_id, and any other value given must match the operation's.
            wait_seconds: Seconds to wait before returning, 0-600.
            profile: HMC connection profile.
        """
        given = _given(
            lpar_name_or_uuid=lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            action=action,
            mode=mode,
        )
        config = build_config(profile=profile)
        agent_id = config.agent_id or "hmcpctl"
        effective = _effective(given, agent_id, request_id, continuation)
        check_inputs(effective["action"], effective["mode"])
        delegated = DELEGATED[effective["action"]]
        if not permits(delegated):
            raise PermissionError(
                f"{delegated} is not permitted by this server's access policy; "
                f"{POWER_TOOL_NAME} action={effective['action']} needs it (ADR 0189)"
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
            POWER_TOOL_NAME,
            agent_id,
            connection_label(profile, tool=POWER_TOOL_NAME),
            config.host,
            request_id,
            effective if continuation == "none" else given,
        )
        body = power_body(
            effective["action"],
            effective["mode"],
            effective["system_name_or_uuid"],
            effective["lpar_name_or_uuid"],
        )
        return engine.submit(
            request,
            body,
            continuation=continuation,
            wait_seconds=wait_seconds,
        )

    return hmc_power_lpar


def register_power_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_power_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(POWER_TOOL_NAME):
        return
    handler = power_handler(tool_security, permits, authorize)
    validate_security(POWER_SECURITY, handler)
    mcp.tool(
        authorized(POWER_TOOL_NAME, POWER_SECURITY, handler, authorize),
        annotations=annotations_for(POWER_SECURITY.effect),
    )
