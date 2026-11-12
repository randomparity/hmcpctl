"""MCP tools for portable LPAR snapshot capture and local validation."""

from __future__ import annotations

from hmcpctl._app import run_sync, serialize_tool_result, with_client
from hmcpctl.operations.affinity.rest import PolicyState
from hmcpctl.snapshots.models import inspect_snapshot, parse_snapshot
from hmcpctl.snapshots.operations import assess_snapshot_affinity, capture_lpar_snapshot
from hmcpctl.tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(
    effect="read",
    operation="snapshot.assess_affinity",
    target_kind="none",
    connection_argument=None,
)
def hmc_snapshot_assess_affinity(
    document: str,
    current_score: int,
    predicted_score: int,
    policy_state: PolicyState = "absent",
    configured_minimum: int | None = None,
    regression_threshold: int | None = None,
    optimization_threshold: int | None = None,
    stale_after_seconds: int = 86400,
) -> dict[str, object]:
    """Assess snapshot affinity evidence locally without HMC I/O or mutation.

    Args:
        document: Valid portable LPAR snapshot JSON text.
        current_score: Current LPAR affinity score from 0 through 100.
        predicted_score: Potential LPAR affinity score from 0 through 100.
        policy_state: Whether current configured policy is present, absent, or unsupported.
        configured_minimum: Current configured minimum score when present.
        regression_threshold: Caller-owned maximum acceptable score decrease.
        optimization_threshold: Caller-owned minimum worthwhile potential gain.
        stale_after_seconds: Maximum permitted snapshot age in seconds.
    """
    result = run_sync(
        lambda: assess_snapshot_affinity(
            document,
            current_score=current_score,
            predicted_score=predicted_score,
            policy_state=policy_state,
            configured_minimum=configured_minimum,
            regression_threshold=regression_threshold,
            optimization_threshold=optimization_threshold,
            stale_after_seconds=stale_after_seconds,
        )
    )
    return serialize_tool_result(result)


@tool(effect="read", operation="snapshot.capture", target_kind="lpar")
def hmc_snapshot_capture(
    lpar_name_or_uuid: str,
    profile_name: str,
    *,
    system_name_or_uuid: str,
    profile: str | None = None,
) -> dict[str, object]:
    """Capture replayable profile configuration and separate placement observations.

    Args:
        system_name_or_uuid: Managed-system name or UUID.
        lpar_name_or_uuid: LPAR name or UUID within the managed system.
        profile_name: Named HMC LPAR profile to capture.
        profile: Optional local HMC connection-profile name.
    """

    async def _go(hmc):
        snapshot = await capture_lpar_snapshot(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            profile_name,
        )
        return snapshot.model_dump(mode="json", exclude_none=True)

    return with_client(_go, profile=profile)


@tool(
    effect="read",
    operation="snapshot.validate",
    target_kind="none",
    connection_argument=None,
)
def hmc_snapshot_validate(document: str) -> dict[str, object]:
    """Validate bounded snapshot JSON locally without HMC I/O.

    Args:
        document: UTF-8 snapshot JSON text, limited to 1 MiB.
    """
    snapshot = parse_snapshot(document)
    return {"valid": True, "format": snapshot.format, "version": snapshot.version}


@tool(
    effect="read",
    operation="snapshot.inspect",
    target_kind="none",
    connection_argument=None,
)
def hmc_snapshot_inspect(document: str) -> dict[str, object]:
    """Inspect a snapshot format and version locally without accepting it.

    Args:
        document: UTF-8 snapshot JSON text, limited to 1 MiB.
    """
    return inspect_snapshot(document).model_dump(mode="json")
