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
    "servers running on them. Most read tools return parsed uom Atom "
    "entries as JSON.\n\n"
    "## Composite tools — prefer these for common tasks\n\n"
    "These tools aggregate multiple HMC endpoints in a single call and "
    "should be the first choice when their scope matches the task:\n\n"
    "- **hmc_lpar_summary(lpar_name_or_uuid)** — state, RMC status, "
    "memory/CPU, OS version, and adapter count for one LPAR. Use instead "
    "of hmc_list_lpars + hmc_list_adapters when you need a quick health check "
    "or status snapshot of a single partition.\n"
    "- **hmc_system_summary(system_name_or_uuid)** — system state, total "
    "resources, LPAR count and state breakdown, and VIOS state for one "
    "managed system. Use instead of hmc_list_systems + hmc_list_lpars + hmc_list_vios "
    "when you need an overview of a single server.\n"
    "- **hmc_capacity_report()** — total, assigned, and free memory (MiB) "
    "and processor units for every managed system, plus running/total LPAR "
    "counts. Use to survey available capacity across the whole HMC.\n"
    "- **hmc_fleet_health()** — issue-only estate view covering systems "
    "not operating, VIOS not running, and inactive LPAR RMC. Use for a bounded "
    "fleet health check instead of composing raw lists.\n"
    "- **hmc_find_placement(desired_memory_mib, desired_proc_units)** — "
    "returns systems that can host a new LPAR of the given size, sorted by "
    "free memory. Use before provisioning to choose a target system.\n"
    "- **hmc_provision_lpar(request_id, ...)** — end-to-end LPAR creation as one "
    "durable operation: creates and stamps the partition, attaches a virtual "
    "network adapter and vSCSI storage, saves the result into a new default "
    "partition profile (exclusive_writer_window=True required), and optionally "
    "powers it on from that profile. A repeat "
    "with the same request_id returns the same operation; poll "
    "hmc_operation_status. Supports dry_run=True to validate inputs without "
    "making changes. For lower-level creation without adapters, use "
    "hmc_create_lpar.\n\n"
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
    "**Check LPAR status:** hmc_lpar_summary → inspect state/rmc_state/os_version.\n\n"
    "**Survey a server:** hmc_system_summary → inspect system state, LPAR "
    "counts, and VIOS health in one call.\n\n"
    "**Plan new LPAR placement:** hmc_capacity_report (see all systems) → "
    "hmc_find_placement(mem, cpu) (filter to candidates) → pick system_uuid.\n\n"
    "**Provision a new LPAR:** hmc_find_placement → hmc_provision_lpar with "
    "dry_run=True (validate) → hmc_provision_lpar with a request_id (execute) "
    "→ hmc_lpar_summary (confirm).\n\n"
    "**Track an operation:** retain the JobID and the stable jobs/{JobID} job_href returned by the "
    "submitting tool → hmc_get_job(job_id, job_href=job_href) for detail → "
    "hmc_wait_for_job(job_id, job_href=job_href) to poll until done. An entry UUID "
    "stored by an earlier release reads only through its job_href.\n\n"
    "**Migrate safely:** hmc_migrate_lpar validates to a successful terminal "
    "state before migration by default; set validate_first=False only to "
    "request direct submission explicitly.\n\n"
    "**Diagnose a failed install:** hmc_capture_lpar_console returns a bounded,\n"
    "write-sealed snapshot of the partition console (SMS menu, firmware prompt,\n"
    "BOS installer error). It holds the partition's single vterm slot while it\n"
    "runs and fails with a distinct contention error when an operator already\n"
    "holds the console; it never sends input to the partition.\n\n"
    "## Lower-level tools\n\n"
    "Use the individual tools (hmc_list_systems, hmc_list_lpars, hmc_list_vios, "
    "hmc_list_adapters, etc.) when you need raw resource data, fields not "
    "returned by the composite tools, or operations outside the composite "
    "tool scope (network, storage, templates, metrics, users).\n\n"
    "## Finding and calling tools\n\n"
    "hmc_search_tools(query) finds tools this server exposes by intent; "
    "hmc_search_tools(name) returns one tool's full input schema. "
    "hmc_invoke_tool(name, arguments) calls a found tool with that tool's own "
    "validation and authorization; calling the tool directly works too.\n\n"
    "## Multi-agent ownership protocol\n\n"
    "When multiple agents share this server, LPAR ownership is tracked via "
    "a description-field token: ``[hmcpctl owner:<agent_id> created:<date>]``.\n\n"
    "**On create:** Ownership tokens are stamped automatically by "
    "hmc_create_lpar and hmc_provision_lpar; a provision whose stamp does not "
    "land fails. A completed "
    "hmc_deploy_partition_template call also stamps automatically when wait=True "
    "and its before/after LPAR snapshots identify exactly one new partition. If "
    "ownership_stamped is None, follow the returned warning: resolve the LPAR name "
    "(e.g. via hmc_list_lpars or hmc_lpar_summary) and call "
    "hmc_set_lpar_description to write the ownership token.\n\n"
    "**Before delete / rename / description-overwrite:** Read the LPAR "
    "description with hmc_get_lpar_description or hmc_lpar_summary. If it "
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
    told to prefer tools absent from ``tools/list``; see
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
    return (
        f"{INSTRUCTIONS}\n\n{CEILING_HEADING}\n\n"
        "The guidance above is written for the full tool set. This server's access "
        "policy withholds these tools it names: "
        f"{', '.join(withheld)}. They are absent from `tools/list` and cannot be "
        "called, so disregard any recommendation above to use one. `tools/list` is "
        f"the authoritative set of what this server exposes.{inspect}"
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
