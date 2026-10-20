"""System-profile membership and lifecycle MCP readers."""

from __future__ import annotations

from ..._app import with_client
from ...operations.systems.profiles import (
    Action,
    SystemProfileInventory,
    SystemProfileOutcome,
    SystemProfilePair,
    activate_system_profile,
    create_system_profile,
    delete_system_profile,
    list_system_profiles,
    update_system_profile,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="system_profile.list", target_kind="managed_system")
def hmc_list_system_profiles(
    system_name_or_uuid: str,
    name: str | None = None,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> SystemProfileInventory:
    """Read complete system-profile membership before optional exact-name lookup.

    Uses the fixed SSH inventory route and current contained profile identities.
    Invalid or unreadable whole inventory returns records=null, never empty success.
    profile selects the configured HMC connection; it is not a member profile.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        name: Exact system-profile name.
        timeout_seconds: Positive overall operation deadline in seconds; timeout requires reconciliation before replay.
        poll_interval: Positive native job polling interval in seconds, bounded by the deadline.
        profile: Optional configured HMC connection profile; distinct from every contained member profile.
    """
    return with_client(
        lambda hmc: list_system_profiles(
            hmc,
            system_name_or_uuid,
            name=name,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(
    effect="mutate",
    operation="system_profile.create",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_create_system_profile(
    system_name_or_uuid: str,
    name: str,
    members: list[SystemProfilePair],
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> SystemProfileOutcome:
    """Create one system profile from explicit contained partition/profile pairs.

    Requires all-targets authorization and every member's normal ownership policy.
    exclusive_writer_window attests caller coordination through terminal reconciliation;
    it does not fence other writers. No replay or rollback follows an uncertain result.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        name: Exact system-profile name.
        members: Explicit partition/profile selector pairs; each profile must be contained by its partition.
        exclusive_writer_window: Attest caller coordination over affected members and namespace through terminal reconciliation.
        ownership_override: Use existing audited ownership override; cannot waive missing identity or containment.
        timeout_seconds: Positive overall operation deadline in seconds; timeout requires reconciliation before replay.
        poll_interval: Positive native job polling interval in seconds, bounded by the deadline.
        profile: Optional configured HMC connection profile; distinct from every contained member profile.
    """
    return with_client(
        lambda hmc: create_system_profile(
            hmc,
            system_name_or_uuid,
            name,
            members,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(
    effect="mutate",
    operation="system_profile.update",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_update_system_profile(
    system_name_or_uuid: str,
    name: str,
    new_name: str | None = None,
    members: list[SystemProfilePair] | None = None,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> SystemProfileOutcome:
    """Rename, replace membership, or both after authorizing the old/new member union.

    Requires caller coordination and current contained profile evidence. An ownership
    override follows existing audited policy and cannot waive missing evidence.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        name: Exact system-profile name.
        new_name: Optional collision-free replacement name; omitted keeps the current name.
        members: Explicit partition/profile selector pairs; each profile must be contained by its partition.
        exclusive_writer_window: Attest caller coordination over affected members and namespace through terminal reconciliation.
        ownership_override: Use existing audited ownership override; cannot waive missing identity or containment.
        timeout_seconds: Positive overall operation deadline in seconds; timeout requires reconciliation before replay.
        poll_interval: Positive native job polling interval in seconds, bounded by the deadline.
        profile: Optional configured HMC connection profile; distinct from every contained member profile.
    """
    return with_client(
        lambda hmc: update_system_profile(
            hmc,
            system_name_or_uuid,
            name,
            new_name=new_name,
            members=members,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(
    effect="destructive",
    operation="system_profile.delete",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_delete_system_profile(
    system_name_or_uuid: str,
    name: str,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> SystemProfileOutcome:
    """Delete one exact system profile through the fixed documented CLI route.

    Every current member is authorized before one command. Independent readback
    cannot turn a failed or uncertain command into successful deletion. No fallback.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        name: Exact system-profile name.
        exclusive_writer_window: Attest caller coordination over affected members and namespace through terminal reconciliation.
        ownership_override: Use existing audited ownership override; cannot waive missing identity or containment.
        profile: Optional configured HMC connection profile; distinct from every contained member profile.
    """
    return with_client(
        lambda hmc: delete_system_profile(
            hmc,
            system_name_or_uuid,
            name,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(
    effect="destructive",
    operation="system_profile.activate",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_activate_system_profile(
    system_name_or_uuid: str,
    name: str,
    action: Action = "activate",
    continue_on_error: bool = False,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> SystemProfileOutcome:
    """Validate or activate once, preserving native facts and bounded member witnesses.

    action is validate, activate, or validate-and-activate. Validation-only omits the
    writer assertion; activating actions require it through reconciliation. Continuation
    is activation-only. Unknown application evidence remains unknown, including stale
    before-matching profiles. Completion is no universal saved-field or guest-boot proof.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        name: Exact system-profile name.
        action: validate, activate, or validate-and-activate; validation-only does not activate members.
        continue_on_error: Activation-only continuation after a member failure; never automatic retry.
        exclusive_writer_window: Attest caller coordination over affected members and namespace through terminal reconciliation.
        ownership_override: Use existing audited ownership override; cannot waive missing identity or containment.
        timeout_seconds: Positive overall operation deadline in seconds; timeout requires reconciliation before replay.
        poll_interval: Positive native job polling interval in seconds, bounded by the deadline.
        profile: Optional configured HMC connection profile; distinct from every contained member profile.
    """
    return with_client(
        lambda hmc: activate_system_profile(
            hmc,
            system_name_or_uuid,
            name,
            action=action,
            continue_on_error=continue_on_error,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )
