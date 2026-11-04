"""``hmc_inspect_lpar``: the primary read-only partition inspection tool (ADR 0200).

The base read runs as ``hmc_get_lpar`` and each section as the specialist it
delegates to, through the served ``dispatch_authorizer``, so a handler needs the
application's ``permits`` and ``authorize`` gates; that is why it is built by a
factory, as ``hmc_plan_lpar`` is.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from fastmcp import FastMCP

from ..._app import with_client
from ...authorization.connection_scope import ConnectionScopeError
from ...authorization.target_scope import TargetScopeError
from ...operations.logical.store import connection_label
from ...operations.lpar.inspect import (
    BASE_TOOL,
    DEFAULT_INCLUDE,
    Admit,
    LparInspection,
    Section,
    inspect_lpar,
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

INSPECT_TOOL_NAME = "hmc_inspect_lpar"
# Not exhaustive, as hmc_plan_lpar: the VIOS reads sit below the signature, so the
# delegated tools carry the target bound (ADR 0200 Decision 6).
INSPECT_SECURITY = ToolSecurity(
    effect="read",
    operation="lpar.inspect",
    target_kind="lpar",
    targets=(
        TargetSelector("lpar", "lpar_name_or_uuid", True),
        TargetSelector("managed_system", "system_name_or_uuid", True),
    ),
)


def admitter(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
    profile: str | None,
) -> Admit:
    """Admit a delegated section read as its own tool; the denial text, or None."""

    def admit(name: str, by_kind: Mapping[str, str | None]) -> str | None:
        if not permits(name):
            return f"{name} is not permitted by this server's access policy"
        try:
            authorize_as(tool_security, authorize, name, by_kind, profile)
        except (TargetScopeError, ConnectionScopeError) as exc:
            return str(exc)
        return None

    return admit


def inspect_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., LparInspection]:
    """The unwrapped ``hmc_inspect_lpar`` handler bound to the served gates."""

    def hmc_inspect_lpar(
        lpar_name_or_uuid: str,
        system_name_or_uuid: str,
        include: list[Section] | None = None,
        profile: str | None = None,
    ) -> LparInspection:
        """Inspect one LPAR: state, RMC, resources, reference codes and next actions.

        Read-only: nothing is written and no console is opened. The partition is read
        as ``hmc_get_lpar``; if the policy withholds that tool or this partition, the
        call is refused. Each section in ``include`` is then authorized as the tool it
        uses and reports its own ``source``: ``ok``, ``unavailable`` (the read failed)
        or ``denied`` (the policy withholds it), with no data unless ``ok``.
        ``refcodes`` (at most 20, newest first) needs ``hmc_read_lpar_refcodes`` and the
        SSH transport. ``resources`` needs ``hmc_list_vios`` for the system and
        ``hmc_get_vios_storage_detail`` for each VIOS (by UUID, at most 16); its
        ``mappings`` cover only the VIOSes read. ``rmc`` comes from the partition read.
        ``profile_drift`` is always ``unavailable`` until a read-only profile read
        exists. ``next_actions`` names tools the policy permits, chosen from the state
        and RMC; ``hmc_capture_lpar_console`` is named, never run.

        Args:
            lpar_name_or_uuid: PartitionName or UUID of the logical partition.
            system_name_or_uuid: The managed system holding it.
            include: Sections to read: resources, rmc, profile_drift, refcodes;
                omitted reads rmc and refcodes, an empty list reads none.
            profile: HMC connection profile.
        """
        sections = DEFAULT_INCLUDE if include is None else tuple(dict.fromkeys(include))
        if not permits(BASE_TOOL):
            raise PermissionError(
                f"{BASE_TOOL} is not permitted by this server's access policy; "
                f"{INSPECT_TOOL_NAME} needs it (ADR 0200)"
            )
        authorize_as(
            tool_security,
            authorize,
            BASE_TOOL,
            {"lpar": lpar_name_or_uuid, "managed_system": system_name_or_uuid},
            profile,
        )
        admit = admitter(tool_security, permits, authorize, profile)
        connection = connection_label(profile, tool=INSPECT_TOOL_NAME)
        inspection = with_client(
            lambda hmc: inspect_lpar(
                hmc,
                connection=connection,
                admit=admit,
                system=system_name_or_uuid,
                lpar=lpar_name_or_uuid,
                include=sections,
            ),
            profile=profile,
        )
        actions = [name for name in inspection.next_actions if permits(name)]
        return dataclasses.replace(inspection, next_actions=actions)

    return hmc_inspect_lpar


def register_inspect_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_inspect_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(INSPECT_TOOL_NAME):
        return
    handler = inspect_handler(tool_security, permits, authorize)
    validate_security(INSPECT_SECURITY, handler)
    mcp.tool(
        authorized(INSPECT_TOOL_NAME, INSPECT_SECURITY, handler, authorize),
        annotations=annotations_for(INSPECT_SECURITY.effect),
    )
