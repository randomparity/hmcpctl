"""MCP tool reporting durable logical operations (ADR 0190)."""

from __future__ import annotations

from ..config import build_config
from ..operations.logical.models import (
    OperationOutcome,
    OperationPage,
    OperationState,
)
from ..operations.logical.store import connection_label, operation_status
from ..tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


# "console", as hmc_get_console_info: no target selector, but `profile` stays under the
# policy's connection scope, because it chooses whose operations are read. Not exhaustive:
# there is no selector for a targets table to bind on.
@tool(
    effect="read",
    operation="operation.status",
    target_kind="console",
    exhaustive_targets=False,
)
def hmc_operation_status(
    operation_id: str | None = None,
    request_id: str | None = None,
    state: OperationState | None = None,
    outcome: OperationOutcome | None = None,
    limit: int = 50,
    cursor: str | None = None,
    profile: str | None = None,
) -> OperationPage:
    """List this agent's logical operations, newest first, from the local operation store.

    Reads only the store in ``HMCPCTL_STATE_DIR`` (or the platform state directory); it
    makes no HMC call and never creates the store, so an empty page means no operation has
    been recorded. Only records whose agent id matches the selected profile's ``agent_id``
    (``hmcpctl`` when unset) and whose connection and HMC host are this call's
    profile (or the environment connection when ``profile`` is omitted) are returned. A lookup by
    ``operation_id`` or ``request_id`` returns at most one record. Each record carries its
    effects, its newest 200 events with ``events_truncated``, its warnings, and
    ``next_actions``: the continuations its state accepts. Pass ``next_cursor`` back as
    ``cursor`` for the next page.

    Args:
        operation_id: Optional operation id (32 lower-case hex digits) to look up.
        request_id: Optional caller request id to look up.
        state: Optional state filter: running, interrupted, paused or terminal.
        outcome: Optional outcome filter, such as needs_attention or completed.
        limit: Page size, 1 to 50.
        cursor: Optional ``next_cursor`` from a previous page.
        profile: Optional configured HMC profile name; selects the agent id and the
            connection this call is authorized against.
    """
    config = build_config(profile=profile)
    return operation_status(
        agent_id=config.agent_id or "hmcpctl",
        connection=connection_label(profile, tool="hmc_operation_status"),
        host=config.host,
        operation_id=operation_id,
        request_id=request_id,
        state=state,
        outcome=outcome,
        limit=limit,
        cursor=cursor,
    )
