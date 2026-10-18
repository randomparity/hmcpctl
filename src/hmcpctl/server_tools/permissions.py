"""The read-only effective-permission inspection tool.

Reports the live permissions of one composed MCP application: the tools its
registry holds, their effect classes, and what the selected access policy
declares. It reports a registry rather than recomputing a ceiling, and it
distinguishes the policy dimensions that constrain this registry from the ones
that merely appear in its grants; see
docs/adr/0037-composition-time-capability-ceiling.md,
docs/adr/0038-dispatch-time-connection-scope.md, and
docs/adr/0047-per-dimension-enforcement-labels.md.

This module must not import ``server``: ``server`` imports it, and the
authoritative tool index arrives as a parameter for that reason.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fastmcp import FastMCP
from pydantic import ValidationError

from ..authorization.access_policy import (
    DEFAULT_CONNECTION_TOKEN,
    AccessPolicy,
    AllTargets,
    Grant,
)
from ..config import (
    ConfigDocument,
    ConfigError,
    HMCConfig,
    build_config,
    env_var_value,
    load_config_document,
)
from ..tool_registry import (
    Authorize,
    ToolSecurity,
    annotations_for,
    authorized,
    is_authorized_wrapper,
    validate_security,
)

TOOL_NAME = "hmc_effective_permissions"

# The classification of a name the authoritative index does not carry. Reported
# rather than raised: a tool that describes the surface must not be the first
# thing to break when the surface changes.
UNKNOWN = "unknown"

# Every dimension of an access policy, in report order. ADR 0037 enforces
# "tools" at registration; ADR 0038 "connections" and ADR 0039 "targets" at
# dispatch. Each one is reported in exactly one of the two dimension tuples
# whenever a policy is selected, so the report never drops a dimension it
# is still enumerating grants for (ADR 0047).
DIMENSIONS: tuple[str, ...] = ("tools", "connections", "targets")


@dataclass(frozen=True)
class ToolPermission:
    """One registered tool and its authoritative classification.

    ``exhaustive_targets`` is false when a policy ``targets`` table cannot bound
    the tool, so only ``all-targets`` grants it (ADR 0039). Reported because it
    is otherwise invisible: the ceiling is per-tool and cannot see targets, so a
    table-only policy still registers and advertises such a tool while denying
    every call to it. Without this field an operator meets that as an
    unexplained denial.
    """

    name: str
    effect: str
    operation: str
    target_kind: str
    exhaustive_targets: bool


@dataclass(frozen=True)
class DeclaredGrant:
    """One compiled grant, rendered whole.

    Grants are conjunctive alternatives (ADR 0036), so connections and targets
    are never merged across entries: a union would describe reach no single
    grant confers.

    ``effects`` is what the grant authored — the effect-class names in the
    document — alongside ``tools``, what it resolves to (its effect-class
    expansion unioned with its named tools). #251: without authorship, a grant
    written as `effects = ["read"]` and one written as an equivalent named-tool
    list are indistinguishable, hiding both how the file was written and the
    ADR 0036/0037 upgrade hazard that an effect-class grant silently gains any
    tool a later release adds to the class.
    """

    tools: tuple[str, ...]
    connections: tuple[str, ...]
    all_targets: bool
    targets: dict[str, tuple[str, ...]]
    effects: tuple[str, ...] = ()


#: The environment variable ``HMCConfig`` reads ``authorize_power_operations``
#: from. Spelled out rather than derived from ``env_prefix`` so the report names
#: the variable an operator actually exports.
POWER_GUARD_ENV_VAR = "HMC_AUTHORIZE_POWER_OPERATIONS"

_logger = logging.getLogger(__name__)

#: What a caller is not told, said once to the operator's own channel. The
#: report's ``detail`` is closed by design (see :func:`_power_guard`), so without
#: this line the reason a connection failed to resolve exists nowhere.
_UNRESOLVED_LOG = (
    "hmc_effective_permissions: the configuration for connection %r could not be "
    "built, so its authorize_power_operations is reported as unresolved: %s"
)


@dataclass(frozen=True)
class PowerOwnershipGuard:
    """The effective ``authorize_power_operations`` for one connection.

    The ADR 0092 §4 guard fails *open* and ``HMCConfig`` sets ``extra="ignore"``,
    so a mistyped profile key or environment variable is dropped with no error
    and is observably identical to a correct ``false`` (#470). ``source`` is what
    separates them: a value an operator meant to set reports ``environment`` or
    ``profile``, and one that never arrived reports ``default``. It asserts an
    origin only where one path can supply it. Environment names use the same
    case-insensitive matching as ``HMCConfig``.

    The field keeps the setting's own name, and its polarity: ``true`` means the
    ADR 0011 ownership guard is **enforced** on power operations, ``false`` that
    LPAR power operations read no ownership token and opens no SSH connection. Naming it
    ``authorized`` would have read as "this connection is not authorized" for the
    permissive value — the reassuring misreading of the fail-open state, which is
    the misreading #470 exists to end. `hmcpctl config show` emits the same key
    for the same boolean.

    It is ``None`` only when the connection's configuration could not be built at
    all, which ``source: unresolved`` names and ``detail`` classifies.
    """

    connection: str
    authorize_power_operations: bool | None
    source: str
    detail: str | None


@dataclass(frozen=True)
class EffectivePermissions:
    """What one composed application may currently do."""

    policy_name: str | None
    policy_source: str | None
    ceiling_enforced: bool
    effects: tuple[str, ...]
    tools: tuple[ToolPermission, ...]
    declared_grants: tuple[DeclaredGrant, ...]
    enforced_dimensions: tuple[str, ...]
    declared_only_dimensions: tuple[str, ...]
    power_ownership_guards: tuple[PowerOwnershipGuard, ...]


EFFECTIVE_PERMISSIONS_SECURITY = ToolSecurity(
    effect="read",
    operation="permissions.describe",
    target_kind="none",
    connection_argument=None,
)


def _permission(name: str, tool_security: Mapping[str, ToolSecurity]) -> ToolPermission:
    """Classify one registered name, tolerating a name outside the index."""
    security = tool_security.get(name)
    if security is None:
        # False is the honest value for a name the index does not carry: nothing
        # establishes that a table could bound it.
        return ToolPermission(name, UNKNOWN, UNKNOWN, UNKNOWN, False)
    return ToolPermission(
        name,
        security.effect,
        security.operation,
        security.target_kind,
        security.exhaustive_targets,
    )


def _declared_grant(grant: Grant) -> DeclaredGrant:
    """Render one compiled grant, restoring the default-connection token."""
    all_targets = isinstance(grant.targets, AllTargets)
    return DeclaredGrant(
        tools=tuple(sorted(grant.tools)),
        connections=tuple(
            sorted(
                DEFAULT_CONNECTION_TOKEN if name is None else name
                for name in grant.connections
            )
        ),
        all_targets=all_targets,
        targets=(
            {}
            if all_targets
            else {
                kind: tuple(sorted(values))
                for kind, values in sorted(grant.targets.items())
            }
        ),
        effects=grant.effects,
    )


def _connections_enforced(
    handlers: Mapping[str, object],
    tool_security: Mapping[str, ToolSecurity],
) -> bool:
    """True when every reported tool that routes a connection is guarded.

    A tool declaring no connection argument opens no HMC connection, so this
    dimension has nothing to say about it — ADR 0037 records both such tools as
    local-only by construction.
    """
    for name, handler in handlers.items():
        security = tool_security.get(name)
        if security is None:
            # A name the index does not carry could route a connection with no
            # guard, and nothing here can tell — the fail-closed default
            # `_permission` already applies to `exhaustive_targets`.
            return False
        if security.connection_argument is None:
            continue
        if not is_authorized_wrapper(handler):
            return False
    return True


def _targets_enforced(
    handlers: Mapping[str, object],
    tool_security: Mapping[str, ToolSecurity],
) -> bool:
    """True when every reported tool carries the dispatch wrapper.

    ADR 0047 had to ask a narrower question than the connection one: `authorized`
    keyed its wrapper on the connection argument, so a tool declaring none
    registered unwrapped and escaped the target check, and this label was
    withheld only when the skipped check would have decided something. #297
    closed that by wrapping every tool, so the question is now the same shape as
    `_connections_enforced`'s — with no exemption, because a tool that opens no
    connection still acts on resources a `targets` table either can or cannot
    bound.
    """
    for name, handler in handlers.items():
        if tool_security.get(name) is None:
            # A name the index does not carry could be registered without the
            # wrapper and nothing here can tell; the same fail-closed default
            # `_permission` applies to `exhaustive_targets`.
            return False
        if not is_authorized_wrapper(handler):
            return False
    return True


def _unresolved_detail(exc: Exception) -> str:
    """A closed description of *exc* for a caller: no message, no input.

    A ``ValidationError`` also names the fields it rejected. Those are
    ``HMCConfig`` field names — compiled-in identifiers, not configuration — and
    without them the report reached for as the last word on this variable answers
    a malformed ``HMC_AUTHORIZE_POWER_OPERATIONS`` with a bare word naming no
    setting. Only ``loc`` is read; ``input`` and ``msg``, which quote the rejected
    value, are not, and neither is ``type``.
    """
    if not isinstance(exc, ValidationError):
        return type(exc).__name__
    fields = sorted(
        {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
    )
    if not fields:
        return "ValidationError"
    return "ValidationError: " + ", ".join(fields)


def _log_unresolved(
    reported: set[tuple[str, str]], connection: str, detail: str, reason: str
) -> None:
    """Say why *connection* failed, at WARNING the first time and DEBUG after.

    The reason exists nowhere else — the caller's ``detail`` is closed — so it
    has to be said. It must not be said on every call: the MCP client owns the
    call rate, a stale profile fails on all of them, and the channel that would
    flood is the one an operator reads to diagnose exactly this. *detail* is the
    dedup key rather than *reason*. The set belongs to one registered MCP
    application, so composing another application re-arms its diagnostics.

    **In a served process this line goes through ADR 0043's bounded stderr sink**,
    under the ``hmcpctl:`` producer prefix ``server.install_package_stderr_sink``
    installs (#534). Before that binding it reached fd 2 through
    ``logging.lastResort`` — unbounded, synchronous, unprefixed and unescaped —
    which is why the dedup above exists. The dedup stays: it is worth keeping on a
    bounded channel too, since a queue this floods is the one an operator reads to
    diagnose exactly this, and a drop is counted rather than free. Outside a
    served process — a direct API caller that never installs the sink — the
    ``lastResort`` route is still what carries it, as it does for every other
    library logger.
    """
    seen = (connection, detail)
    if seen in reported:
        _logger.debug(_UNRESOLVED_LOG, connection, reason)
        return
    reported.add(seen)
    _logger.warning(_UNRESOLVED_LOG, connection, reason)


def _guard_source(config: HMCConfig) -> tuple[str, str | None]:
    """Name what supplied *config*'s guard value, and never assert more.

    ``environment`` uses :func:`env_var_value`, the same case-insensitive,
    last-match lookup that filters profile values before ``HMCConfig`` is built.
    ``default`` needs the field to be unset, which no path can fake.

    ``default`` covers only a value omitted by both the selected profile and the
    environment. Configuration read, parse, and validation failures reach
    :func:`_power_guard` and are reported as ``unresolved`` instead.
    """
    if env_var_value(POWER_GUARD_ENV_VAR) is not None:
        return "environment", None
    if "authorize_power_operations" not in config.model_fields_set:
        return "default", None
    return "profile", None


def _power_guard(
    profile: str | None,
    reported_unresolved: set[tuple[str, str]],
    document: ConfigDocument | Exception | None = None,
) -> PowerOwnershipGuard:
    """Resolve the guard for one connection the way a tool call would.

    :func:`~hmcpctl.config.build_config` is the resolution every tool and CLI
    entry point runs, so asking it is what makes the answer *effective* rather
    than merely declared — including the case an operator cannot see from the
    file alone, where an ambient ``HMC_HOST`` sends resolution down the env-only
    path and a profile's ``authorize_power_operations`` never reaches the config
    the guard reads.

    A connection that cannot be resolved is reported, not raised: a tool that
    describes the surface must not be the first thing to break when the surface
    changes, which is the same contract :func:`_permission` keeps for a name
    outside the index.

    No exception message reaches the caller. ``ConfigError`` names the whole
    ``profiles`` and ``nicknames`` inventory of ``config.toml`` and its absolute
    path — a connection inventory this tool would hand out in one call, over a
    channel no access policy can withhold once the tool is granted, which is
    exactly what ``connection_scope``'s closed denial templates and ADR 0038
    refuse. Pydantic quotes the rejected input in a ``ValidationError`` and the
    fields it validates include ``password``. So ``detail`` is closed:
    :func:`_unresolved_detail` builds it from the exception's own class name and,
    for a ``ValidationError``, the compiled-in field names it rejected. The real
    message goes to the server's log, which is the operator's channel rather than
    the caller's — and only for a ``ConfigError``, whose text names paths and keys
    but never their values.

    The second arm is deliberately total. ``build_config`` reaches pydantic and
    ``tomllib`` over operator-authored data and can raise outside any list this
    module could enumerate — a profile key spelled ``_env_file`` collides with
    the keyword ``_load_profile_from_document`` passes and raises ``TypeError``,
    to name one that exists today. This is the only path that builds a config for
    *every* granted connection in one call, so an escaping exception costs the
    operator the guard state of the connections that resolve fine, in exactly the
    situation the report exists to diagnose.
    """
    connection = DEFAULT_CONNECTION_TOKEN if profile is None else profile
    try:
        if isinstance(document, Exception):
            raise document
        config = build_config(profile=profile, document=document)
    except ConfigError as exc:
        _log_unresolved(reported_unresolved, connection, "ConfigError", str(exc))
        return PowerOwnershipGuard(connection, None, "unresolved", "ConfigError")
    except Exception as exc:  # noqa: BLE001 — reported, not raised; see above
        detail = _unresolved_detail(exc)
        _log_unresolved(reported_unresolved, connection, detail, detail)
        return PowerOwnershipGuard(connection, None, "unresolved", detail)
    source, detail = _guard_source(config)
    return PowerOwnershipGuard(
        connection, config.authorize_power_operations, source, detail
    )


def resolve_power_guards(
    policy: AccessPolicy | None,
    reported_unresolved: set[tuple[str, str]] | None = None,
) -> tuple[PowerOwnershipGuard, ...]:
    """Resolve the guard for every connection this server can route a call to.

    One value would be false whenever profiles disagree, and they can: the guard
    is read from the resolved config, so a TOML ``authorize_power_operations``
    binds only the profile that carries it, and both the MCP tools and the CLI
    take a caller-supplied profile selector.

    The set is exactly the policy's connection dimension (ADR 0038), including
    the default resolution only when a grant names it: a connection no grant
    names is denied at dispatch, so reporting its guard state would describe a
    call this server refuses. With no policy — a state ``create_mcp`` cannot
    produce since ADR 0041, so this binds a direct caller — the default
    resolution is the only connection there is.

    The intersection carries the other half of that rule. An ambient ``HMC_HOST``
    collapses *every* connection token to the default one at dispatch
    (``connection_scope.selected_connection`` rule 1), because ``build_config``
    gates its whole TOML branch on it. Without the intersection this report would
    list rows for named profiles nothing can reach and omit the one every
    permitted call resolves to — in precisely the scenario
    ``docs/environment-variables.md`` singles out as the fail-open direction, and
    which this report is now the recommended way to see.

    Reads the environment and the filesystem per request rather than at
    registration. One invocation reads and parses at most one configuration
    document, then resolves every profile through ``build_config`` from that
    snapshot. An edit therefore changes the next call while every row in one
    report comes from the same document version. The blocking read remains off
    the event loop; the tool handler awaits this function in a thread.

    **Constructing a config re-runs ``HMCConfig``'s model validators per
    connection.** The ``audit_memento`` override diagnostic among them is
    throttled by :meth:`HMCConfig._warn_audit_memento_override` after #546.
    """
    if reported_unresolved is None:
        reported_unresolved = set()
    connections: set[str | None] = set()
    if policy is None:
        connections.add(None)
    else:
        for grant in policy.grants:
            connections.update(grant.connections)
    ambient_host = env_var_value("HMC_HOST")
    if ambient_host:
        connections &= {None}
    ordered = sorted(connections, key=lambda name: (name is not None, name or ""))
    document: ConfigDocument | Exception | None = None
    if ordered and not ambient_host:
        try:
            document = load_config_document()
        except Exception as exc:  # noqa: BLE001 — classified per connection below
            document = exc
    return tuple(_power_guard(name, reported_unresolved, document) for name in ordered)


def build_effective_permissions(
    handlers: Mapping[str, object],
    policy: AccessPolicy | None,
    tool_security: Mapping[str, ToolSecurity],
    power_guards: tuple[PowerOwnershipGuard, ...],
) -> EffectivePermissions:
    """Build the report for a registry of *handlers* by name, under *policy*.

    The registered callables, not just their names: the connection and target
    dimensions are enforced by a wrapper around the callable (ADR 0038,
    ADR 0039), so the callable is the only evidence that they are enforced at
    all.

    ``ceiling_enforced`` is checked, not inferred: a policy must be selected and
    every reported name must satisfy it. A registry that has drifted past its
    ceiling therefore reports a policy name with no *tool*-enforcement claim,
    rather than a claim the registry contradicts.

    The two dimension tuples are decided per dimension rather than from
    ``ceiling_enforced`` alone (ADR 0047). Together they cover all three
    dimensions whenever a policy is selected, so a drifted registry says which
    dimensions still constrain it instead of falling silent about every one.
    With no policy selected both are empty: nothing is declared, so nothing is
    declared-only either.

    An empty *handlers* satisfies all three checks vacuously, and deliberately:
    a registry holding nothing cannot exceed any ceiling nor skip any wrapper,
    so a policy denying everything is enforced maximally rather than not at all.
    The state is unreachable through the tool — its own registration is what
    makes *handlers* non-empty — so this only binds a direct caller of
    :func:`build_effective_permissions`.

    *power_guards* arrives resolved rather than being read here, so this stays a
    pure function of its arguments; :func:`resolve_power_guards` owns the
    environment and filesystem reads it needs.
    """
    names = sorted(handlers)
    tools = tuple(_permission(name, tool_security) for name in names)
    ceiling = policy is not None and all(policy.permits_tool(name) for name in names)
    enforced_by_dimension = {
        "tools": ceiling,
        "connections": (
            policy is not None and _connections_enforced(handlers, tool_security)
        ),
        "targets": policy is not None and _targets_enforced(handlers, tool_security),
    }
    enforced = tuple(d for d in DIMENSIONS if enforced_by_dimension[d])
    return EffectivePermissions(
        policy_name=None if policy is None else policy.name,
        policy_source=None if policy is None else policy.source,
        ceiling_enforced=ceiling,
        effects=tuple(
            sorted({tool.effect for tool in tools if tool.effect != UNKNOWN})
        ),
        tools=tools,
        declared_grants=(
            () if policy is None else tuple(_declared_grant(g) for g in policy.grants)
        ),
        enforced_dimensions=enforced,
        declared_only_dimensions=(
            () if policy is None else tuple(d for d in DIMENSIONS if d not in enforced)
        ),
        power_ownership_guards=power_guards,
    )


def register_permissions_tool(
    mcp: FastMCP,
    policy: AccessPolicy,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register the inspection tool on *mcp*, unless the ceiling withholds it.

    The gate lives here rather than in the caller so that this site and the
    domain modules honour ``permits`` under one contract, and no caller can be
    handed a ceiling it forgets to apply.

    The handler closes over *mcp* so it reads the registry it is reporting on.
    It reads ``local_provider`` rather than ``mcp.list_tools()``: the provider is
    what ``configure_arbitrary_command_tool`` mutates, and the server-level call
    runs the ``tools/list`` middleware chain, which this is not.

    The provider is the wider set for a tool that is disabled or filtered by
    app visibility, and the *narrower* one for a tool reached through a mounted
    sub-server. So ``ceiling_enforced`` is accurate for an application nothing
    is mounted onto, which is every application this package composes; nothing
    in ``src/`` or ``scripts/`` calls ``mount()`` or ``as_proxy()``. A future
    mount would need the two accessors reconciled.
    """
    if not permits(TOOL_NAME):
        return
    reported_unresolved: set[tuple[str, str]] = set()

    async def hmc_effective_permissions() -> EffectivePermissions:
        """Report what this MCP server may currently do.

        Returns the tools this server exposes with their effect classes, the
        selected access policy's name and file, and, per grant, the effect
        classes the grant authored beside its resolved tools and its declared
        connection and target constraints. Each of the three dimensions —
        tools, connections, and targets — is reported in exactly one of
        `enforced_dimensions` and `declared_only_dimensions`, checked against
        this registry rather than assumed. A tool reporting
        `exhaustive_targets: false` can only be granted by a grant whose targets
        are the `all-targets` sentinel.

        `power_ownership_guards` reports the effective, post-precedence
        `authorize_power_operations` (ADR 0092 §4) for each connection a call may
        select, with the source that supplied it — so a setting that was dropped
        silently reads as `default` rather than as a deliberate `false`. Contains
        no credentials.
        """
        # `getattr`, because fastmcp declares `fn` on `FunctionTool` and not on
        # the `Tool` base the provider is typed to return. A registration that
        # carries no callable cannot witness the dispatch wrapper, and reports
        # the connection and target dimensions as unenforced rather than
        # assuming them.
        handlers = {
            tool.name: getattr(tool, "fn", None)
            for tool in await mcp.local_provider.list_tools()
        }
        # Off the loop: `resolve_power_guards` re-reads and re-parses
        # `config.toml` once per granted connection, and the call rate belongs to
        # the MCP client, not the operator. Left inline, a generated
        # `legacy-equivalent` policy — whose connections are every profile key in
        # the file — stalls every other in-flight dispatch, the audit path
        # included, for the duration of that I/O.
        power_guards = await asyncio.to_thread(
            resolve_power_guards, policy, reported_unresolved
        )
        return build_effective_permissions(
            handlers, policy, tool_security, power_guards
        )

    validate_security(EFFECTIVE_PERMISSIONS_SECURITY, hmc_effective_permissions)
    # Wrapped like every other tool since #297, and it is this site that made the
    # async branch in `authorized` necessary: this is the package's only coroutine
    # handler, and it was previously left unwrapped only because it declares no
    # connection argument. The target dimension reaches it now — a `targets` table
    # denies it, since `target_kind="none"` leaves nothing for a table to bind.
    mcp.tool(
        authorized(
            TOOL_NAME,
            EFFECTIVE_PERMISSIONS_SECURITY,
            hmc_effective_permissions,
            authorize,
        ),
        annotations=annotations_for(EFFECTIVE_PERMISSIONS_SECURITY.effect),
    )
