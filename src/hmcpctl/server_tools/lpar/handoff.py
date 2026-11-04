"""``hmc_prepare_host_handoff``: place, report and release a host-handoff hold (ADR 0193).

``prepare`` reads the partition as ``hmc_get_lpar`` and ``hmc_list_lpar_ownership`` and
each document section as the tool it delegates to, through the served
``dispatch_authorizer`` (ADR 0329), so the handler is built by a factory, as
``hmc_inspect_lpar`` is. The tool is exempt from holds (``HOLD_EXEMPT_TOOLS``), so its
``hold_id`` is its own parameter rather than the registry's injected one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from fastmcp import FastMCP

from ..._app import with_client
from ...config import build_config
from ...operations.logical.store import connection_label
from ...operations.lpar.handoff import (
    HandoffResult,
    check_inputs,
    prepare_handoff,
    release_handoff,
)
from ...tool_registry import (
    Authorize,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    authorized,
    hold_guarded,
    validate_security,
)
from ..delegation import authorize_as
from .inspect import admitter

HANDOFF_TOOL_NAME = "hmc_prepare_host_handoff"
# Not exhaustive, as hmc_inspect_lpar: the VIOS and adapter reads sit below the
# signature, so the delegated tools carry the target bound (ADR 0329 Decision 1).
HANDOFF_SECURITY = ToolSecurity(
    effect="mutate",
    operation="lpar.prepare_host_handoff",
    target_kind="lpar",
    targets=(
        TargetSelector("lpar", "lpar_name_or_uuid", True),
        TargetSelector("managed_system", "system_name_or_uuid", True),
    ),
)
# Admitted for the partition before any read; the VIOS detail tool is then admitted per VIOS.
REQUIRED = ("hmc_get_lpar", "hmc_list_lpar_ownership")
PERMITTED = (*REQUIRED, "hmc_get_vios_storage_detail")


def unchecked_tools(tool_security: Mapping[str, ToolSecurity]) -> list[str]:
    """The non-read tools the hold hook never checks (ADR 0327 Decision 5)."""
    return sorted(
        name
        for name, security in tool_security.items()
        if security.effect != "read"
        and not hold_guarded(name, security)
        and name != HANDOFF_TOOL_NAME
    )


def handoff_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., HandoffResult]:
    """The unwrapped ``hmc_prepare_host_handoff`` handler bound to the served gates."""
    unchecked = unchecked_tools(tool_security)

    def hmc_prepare_host_handoff(
        action: Literal["prepare", "release"],
        lpar_name_or_uuid: str,
        system_name_or_uuid: str,
        hold: bool = False,
        consumer_label: str | None = None,
        hold_id: str | None = None,
        profile: str | None = None,
    ) -> HandoffResult:
        """Prepare a partition for an external consumer such as kdive, or release it.

        ``prepare`` returns ``{action, hold, document}``. With ``hold=true`` and a
        ``consumer_label`` (1-64 printable characters) it places the partition's one
        hold, which refuses this host's hmcpctl partition mutations that do not present
        its ``hold_id``; a repeat from the same agent with the same label returns the
        same hold, so repeat the call if its response was lost. ``hold.hold_id`` is
        returned only to the agent that placed the hold, which passes it to the
        consumer; ``document`` never carries it. The hold is refused while a logical
        operation runs on the partition or another agent or label holds it. Without
        ``hold`` it reports any existing hold. Needs ``hmc_get_lpar``,
        ``hmc_list_lpar_ownership`` and ``hmc_get_vios_storage_detail``; the
        ``storage`` section is also read as ``hmc_list_vios`` and each VIOS as
        ``hmc_get_vios_storage_detail``, and ``adapters`` as ``hmc_list_adapters``,
        each reported ``ok``, ``unavailable`` or ``denied``. The document's
        ``observed`` facts come from reads made during the call; ``declared`` is what
        this host's provision requested; ``unverified`` lists the guest facts hmcpctl
        does not check (kdive's doctor/adopt does); ``unfenced`` and
        ``unchecked_tools`` say what a hold does not stop. ``release`` drops the hold
        given its ``hold_id`` or when called by the placing agent, and returns a null
        ``document``. Neither action writes to the HMC or deletes the partition.

        Args:
            action: prepare or release.
            lpar_name_or_uuid: PartitionName or UUID of the logical partition.
            system_name_or_uuid: The managed system holding it.
            hold: Place a hold; prepare only, with consumer_label.
            consumer_label: Who the partition is handed to, 1-64 characters.
            hold_id: The hold to release; release only.
            profile: HMC connection profile.
        """
        check_inputs(action, hold, consumer_label, hold_id)
        agent_id = build_config(profile=profile).agent_id or "hmcpctl"
        if action == "release":
            return with_client(
                lambda hmc: release_handoff(
                    hmc,
                    agent_id=agent_id,
                    system=system_name_or_uuid,
                    lpar=lpar_name_or_uuid,
                    hold_id=hold_id,
                ),
                profile=profile,
            )
        for name in PERMITTED:
            if not permits(name):
                raise PermissionError(
                    f"{name} is not permitted by this server's access policy; "
                    f"{HANDOFF_TOOL_NAME} needs it (ADR 0329)"
                )
        targets = {"lpar": lpar_name_or_uuid, "managed_system": system_name_or_uuid}
        for name in REQUIRED:
            authorize_as(tool_security, authorize, name, targets, profile)
        connection = connection_label(profile, tool=HANDOFF_TOOL_NAME)
        admit = admitter(tool_security, permits, authorize, profile)
        return with_client(
            lambda hmc: prepare_handoff(
                hmc,
                connection=connection,
                agent_id=agent_id,
                admit=admit,
                system=system_name_or_uuid,
                lpar=lpar_name_or_uuid,
                consumer_label=consumer_label,
                unchecked_tools=unchecked,
            ),
            profile=profile,
        )

    return hmc_prepare_host_handoff


def register_handoff_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_prepare_host_handoff`` on *mcp* when the ceiling admits it."""
    if not permits(HANDOFF_TOOL_NAME):
        return
    handler = handoff_handler(tool_security, permits, authorize)
    validate_security(HANDOFF_SECURITY, handler)
    mcp.tool(
        authorized(HANDOFF_TOOL_NAME, HANDOFF_SECURITY, handler, authorize),
        annotations=annotations_for(HANDOFF_SECURITY.effect),
    )
