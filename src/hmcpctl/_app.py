"""Shared app state and entry points for the hmcpctl server.

Builds empty :class:`FastMCP` instances for explicit composition and provides
the small sync-run / SSH-passthrough helpers used by tool bodies. Tool
capability classification lives on each tool's ``ToolSecurity`` record in
``tool_registry.py``; see docs/adr/0035-enforceable-tool-security-metadata.md.

``server.py`` imports this module and every ``server_tools/`` domain adapter;
those adapters use the helpers defined here, while this module never imports
them (one-way dependency, no cycles). The client-visible instructions live here too, and ``server.py``
qualifies them against its access policy before composing
(docs/adr/0048-ceiling-aware-server-instructions.md).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar, overload

from fastmcp import FastMCP

from .client.client_factory import client_from_env
from .client.core import HMCClient
from .config import HMCConfig, build_config
from .ssh.selectors import resolve_ssh_names

_T = TypeVar("_T")


def serialize_tool_result(value: Any) -> Any:
    """Convert domain dataclasses at the MCP boundary; leave mappings unchanged."""
    return asdict(value) if is_dataclass(value) else value


INSTRUCTIONS = (
    "Tools for querying and operating IBM Power systems via the HMC "
    "(Hardware Management Console) REST API. Managed systems are Power "
    "servers; logical partitions (LPARs) are the AIX/Linux/IBM i virtual "
    "servers running on them.\n\n"
    "## The listed tools\n\n"
    "`tools/list` carries the primary catalog: logical tools that each do a whole "
    "task. Prefer them.\n\n"
    "- **hmc_inventory** — the managed systems and partitions on one connection, "
    "with state, capacity and owner.\n"
    "- **hmc_plan_lpar** — check a new partition's inputs and placement without "
    "writing anything; returns blockers and a plan_digest.\n"
    "- **hmc_provision_lpar(request_id, ...)** — create, stamp and configure an "
    "LPAR as one durable operation (exclusive_writer_window=True required); with "
    "`install`, it also mounts installer media and boots it. It ends at "
    "`boot_started`, when the installer has begun booting: install success and "
    "guest readiness are the caller's to check. dry_run=True validates without "
    "making changes.\n"
    "- **hmc_reconfigure_lpar** — change one LPAR's processors, memory, name, "
    "networks, disks, installer media or boot device; each change reports `live`, "
    "`profile` or `pending_activation`, and nothing reboots the partition.\n"
    "- **hmc_inspect_lpar** — one LPAR's state, RMC, resources and recent "
    "reference codes, with next_actions.\n"
    "- **hmc_power_lpar** — start, stop or restart one LPAR; graceful unless "
    "mode=immediate is stated.\n"
    "- **hmc_decommission_lpar** — remove one LPAR, retaining its storage unless "
    "storage_cleanup=delete_owned.\n"
    "- **hmc_prepare_host_handoff** — hold an LPAR and describe it for an external "
    "manager, or release the hold.\n"
    "- **hmc_operation_status** — read durable operations by operation_id or "
    "request_id, or list your own.\n"
    "- **hmc_search_tools** and **hmc_invoke_tool** — reach every other tool; see "
    "*Specialist tools* below.\n\n"
    "## Resource addressing and asynchronous jobs\n\n"
    "Parameters ending in `*_name_or_uuid` accept either a resource name or UUID. "
    "Parameters\nending in `*_uuid` require a UUID. SSH-passthrough tools usually resolve "
    "UUIDs to HMC CLI\nnames before running the command. VIOS backup catalog tools are "
    "exceptions:\n`hmc_list_vios_backups` passes a VIOS UUID directly. `hmc_backup_vios` "
    "and\n`hmc_restore_vios` are SSH-ready with a direct system name plus VIOS UUID; a "
    "VIOS name or\nsystem UUID requires REST. A system UUID resolves to its unique MTMS "
    "identity, never an HMC\nCLI name, with no `lssyscfg` fallback.\n\n"
    "For tools that expose asynchronous wait controls, `wait=False` is the default "
    "and returns\nthe submitted job for later polling; `wait=True` polls until the "
    "job reaches a terminal\nstate. Install tools use `wait_timeout_seconds=None` "
    "by default, deriving the client-side\npolling budget from "
    "`hmc_timeout_minutes` plus one poll interval. Other wait-capable tools\nuse "
    "`timeout_seconds=300` by default. `poll_interval=5` is the default number of "
    "seconds\nbetween status requests.\n\n"
    "## Recommended workflows\n\n"
    "**Survey:** hmc_inventory → hmc_inspect_lpar for one partition.\n\n"
    "**Create and install:** hmc_plan_lpar → hmc_provision_lpar with the same "
    "inputs, a request_id and expected_plan_digest → hmc_operation_status until "
    "the operation is terminal.\n\n"
    "**Resume:** a mutating logical tool returns within wait_seconds while its "
    "operation continues. Repeat the call with the same request_id and "
    'continuation="resume" to continue an interrupted or paused operation, or '
    'continuation="boot" to boot a provision paused at ready_to_boot.\n\n'
    "**Change, power or remove:** hmc_reconfigure_lpar, hmc_power_lpar or "
    "hmc_decommission_lpar, each with its own request_id.\n\n"
    "**Diagnose a failed install:** hmc_inspect_lpar with refcodes, then the "
    "specialist hmc_capture_lpar_console, which returns a bounded, write-sealed "
    "snapshot of the partition console. It holds the partition's single vterm slot "
    "while it runs, fails with a distinct contention error when an operator "
    "already holds the console, and never sends input to the partition.\n\n"
    "## Specialist tools\n\n"
    "Every other tool this server exposes is a specialist: raw resource lists, "
    "single-endpoint writes, jobs, network, storage, templates, metrics and users. "
    "Specialists are not in `tools/list`. hmc_search_tools(query) finds them by "
    "intent and hmc_search_tools(name) returns one tool's full input schema. Call "
    "a found tool by name, or through hmc_invoke_tool(name, arguments) where it is "
    "granted; either way that tool's own validation and authorization apply.\n\n"
    "## Multi-agent ownership protocol\n\n"
    "When multiple agents share this server, LPAR ownership is tracked via "
    "a description-field token: ``[hmcpctl owner:<agent_id> created:<date>]``.\n\n"
    "**On create:** Ownership tokens are stamped automatically by "
    "hmc_create_lpar and hmc_provision_lpar; a provision whose stamp does not "
    "land fails. A completed "
    "hmc_deploy_partition_template call also stamps automatically when wait=True "
    "and its before/after LPAR snapshots identify exactly one new partition. If "
    "ownership_stamped is None, follow the returned warning: resolve the LPAR name "
    "(e.g. via hmc_inventory) and call the specialist "
    "hmc_set_lpar_description to write the ownership token.\n\n"
    "**Before delete / rename / description-overwrite:** Check the owner "
    "hmc_inventory reports, or read the LPAR description with the specialist "
    "hmc_get_lpar_description. If it "
    "contains ``[hmcpctl owner:<id> ...]`` and <id> differs from your "
    "HMC_AGENT_ID, stop and ask the operator before proceeding.\n\n"
    "**Absent token:** An LPAR with no token was created before this feature "
    "or through a path that does not stamp. Treat it as unowned and proceed "
    "with caution — ask the operator if in doubt.\n\n"
    "**Set HMC_AGENT_ID** in the environment for per-agent attribution in "
    "HMC audit logs (X-Audit-Memento: hmcpctl:<agent_id>)."
)

_TOOL_MENTION = re.compile(r"\bhmc_[a-z0-9_]+\b")

CEILING_HEADING = "## Tools this server's access policy withholds"


def _mentioned_tools(index: Iterable[str]) -> frozenset[str]:
    """The names in *index* that :data:`INSTRUCTIONS` recommends by name.

    Derived from the prose rather than restated as a list beside it, so that
    editing the prose cannot leave a hand-maintained roster stale. Intersecting
    with the authoritative tool index is what keeps the pattern honest: it also
    matches ``hmc_timeout_minutes``, a configuration setting the conventions
    block names, which is not a tool and must not be reported as withheld.
    """
    return frozenset(_TOOL_MENTION.findall(INSTRUCTIONS)) & frozenset(index)


def ceiling_aware_instructions(
    permits: Callable[[str], bool], index: Iterable[str]
) -> str:
    """:data:`INSTRUCTIONS`, qualified when *permits* withholds a tool it names.

    The instructions are fixed at composition and shipped whole to every client
    at ``initialize``, while ``server.create_mcp`` filters registration by the
    same *permits* predicate. A client under a narrow ceiling would otherwise be
    told to prefer tools it cannot call; see
    docs/adr/0048-ceiling-aware-server-instructions.md. When nothing the prose
    names is withheld — the legacy-equivalent policy, and any policy admitting
    all of them — the string is returned unchanged.

    *permits* is the ceiling, not the served registry: ``hmc_run_command`` also
    needs ``--enable-arbitrary-command``, so the ceiling can admit a name the
    registry omits. The prose names no such tool, and the divergence can only
    under-report, never invent a withheld name.
    """
    withheld = sorted(name for name in _mentioned_tools(index) if not permits(name))
    if not withheld:
        return INSTRUCTIONS
    inspect = (
        " Call hmc_effective_permissions for the policy that decided it."
        if permits("hmc_effective_permissions")
        else ""
    )
    rest = (
        "hmc_search_tools finds the rest it exposes."
        if permits("hmc_search_tools")
        else "the rest it exposes are callable only by a client that knows their names."
    )
    return (
        f"{INSTRUCTIONS}\n\n{CEILING_HEADING}\n\n"
        "The guidance above is written for the full tool set. This server's access "
        "policy withholds these tools it names: "
        f"{', '.join(withheld)}. They cannot be called by any route, so disregard "
        "any recommendation above to use one. `tools/list` lists this server's "
        f"primary tools; {rest}{inspect}"
    )


def create_mcp(instructions: str = INSTRUCTIONS) -> FastMCP:
    """Create an empty MCP application for explicit domain composition.

    *instructions* defaults to the unqualified prose; ``server.create_mcp``
    passes the ceiling-aware form built by :func:`ceiling_aware_instructions`.
    """
    return FastMCP(name="hmcpctl", instructions=instructions)


def run_sync(fn: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run a coroutine-returning closure from a sync tool function."""
    return asyncio.run(fn())


def with_client(
    fn: Callable[[HMCClient], Awaitable[_T]], *, profile: str | None = None
) -> _T:
    """Run one REST operation with a profile-selected managed client."""

    async def operation() -> _T:
        async with client_from_env(profile) as hmc:
            return await fn(hmc)

    return run_sync(operation)


def with_config(
    fn: Callable[[HMCConfig], Awaitable[_T]], *, profile: str | None = None
) -> _T:
    """Run one SSH-only operation with profile-selected configuration."""
    config = build_config(profile=profile)

    async def operation() -> _T:
        return await fn(config)

    return run_sync(operation)


def require_valid_limit(limit: int | None) -> None:
    """Reject a negative collection limit before any HMC request."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be greater than or equal to 0")


def run_limited_collection(
    fn: Callable[[HMCClient], Awaitable[list[_T]]],
    limit: int | None,
    *,
    profile: str | None = None,
) -> list[_T]:
    """Run a full collection request, then cap its agent-facing result."""
    require_valid_limit(limit)
    entries = with_client(fn, profile=profile)
    return entries if limit is None else entries[:limit]


@overload
def ssh_with_client(
    fn: Callable[[HMCConfig, str, str], Awaitable[_T]],
    *,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> _T: ...


@overload
def ssh_with_client(
    fn: Callable[[HMCConfig, str, None], Awaitable[_T]],
    *,
    system_name_or_uuid: str,
    lpar_name_or_uuid: None = None,
    profile: str | None = None,
) -> _T: ...


@overload
def ssh_with_client(
    fn: Callable[[HMCConfig, None, str], Awaitable[_T]],
    *,
    system_name_or_uuid: None = None,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> _T: ...


@overload
def ssh_with_client(
    fn: Callable[[HMCConfig, None, None], Awaitable[_T]],
    *,
    system_name_or_uuid: None = None,
    lpar_name_or_uuid: None = None,
    profile: str | None = None,
) -> _T: ...


def ssh_with_client(
    fn: Callable[[HMCConfig, str | None, str | None], Awaitable[_T]],
    *,
    system_name_or_uuid: str | None = None,
    lpar_name_or_uuid: str | None = None,
    profile: str | None = None,
) -> _T:
    """Resolve name-or-uuid args to CLI names, then run an SSH tool body.

    Collapses the pervasive ``async def _go`` + name resolution + ``run_sync``
    scaffold in the SSH-passthrough tools, mirroring :func:`with_client` for
    the SSH seam. *system_name_or_uuid* and *lpar_name_or_uuid* may each be a
    CLI name (passed through untouched) or a UUID (resolved via REST, falling
    back to an ``lssyscfg`` name lookup over SSH when the REST transport is
    unreachable). *fn* is called with the profile-selected ``HMCConfig``
    followed by the resolved system and LPAR CLI names (``None`` when the
    matching arg was not supplied) and returns an awaitable for the tool result
    — a ``run_hmc_command(...)`` call, or a call into an :mod:`ssh` helper that
    runs the command itself.

    *profile* selects the HMC connection profile; when ``None`` the env-default
    resolution order is used.  The same profile is used for both SSH and the
    REST name-resolution leg so that both operations target the same HMC.
    Selection is local to this call with no cross-call shared state.
    """

    async def _go() -> _T:
        config = build_config(profile=profile)
        system_name, lpar_name = await resolve_ssh_names(
            config, system_name_or_uuid, lpar_name_or_uuid
        )
        return await fn(config, system_name, lpar_name)

    return run_sync(_go)
