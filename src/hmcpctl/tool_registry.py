"""Local MCP tool collection for explicit application composition.

Every collected tool carries a :class:`ToolSecurity` record — effect class,
operation identity, target kind, and the public arguments from which connection
and target selectors are read. It is the single authoritative classification:
the MCP ``ToolAnnotations`` shipped to clients are derived from ``effect``, and
``server_tools.catalog.TOOL_SECURITY`` indexes the records for the access-policy layers built
on top of them. See docs/adr/0035-enforceable-tool-security-metadata.md.
"""

from __future__ import annotations

import functools
import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import MISSING, dataclass, is_dataclass, replace
from dataclasses import fields as dataclass_fields
from types import MappingProxyType
from typing import Any, Literal, TypeVar, get_args, get_origin, get_type_hints

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import ToolAnnotations
from pydantic import BaseModel

Effect = Literal["read", "mutate", "destructive", "arbitrary-command"]

TargetKind = Literal[
    "none",
    "console",
    "managed_system",
    "lpar",
    "vios",
    "cluster",
    "shared_storage_pool",
    "user",
    "password_policy",
    "job",
    "template",
    "metric_resource",
]

EFFECTS: frozenset[str] = frozenset(get_args(Effect))
TARGET_KINDS: frozenset[str] = frozenset(get_args(TargetKind))

_OPERATION = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Public argument names that unambiguously identify one resource kind. tool()
# intersects this with each handler signature to build its target selectors, so
# a tool cannot omit a target it accepts an identity for. Deliberately excludes
# `name` (a user on hmc_create_user, a new partition on hmc_create_lpar) and
# sub-resource arguments, which are addressed through their owning resource.
REQUIRED_TARGET_ARGUMENTS: Mapping[str, TargetKind] = MappingProxyType(
    {
        "lpar_name_or_uuid": "lpar",
        "lpar_uuid": "lpar",
        "system_name_or_uuid": "managed_system",
        "target_system_name_or_uuid": "managed_system",
        "vios_name_or_uuid": "vios",
        "vios_uuid": "vios",
        "vios_partition_id": "vios",
        "cluster_uuid": "cluster",
        "ssp_uuid": "shared_storage_pool",
        "console_uuid": "console",
        "job_id": "job",
        "template_uuid": "template",
        "draft_template_uuid": "template",
        "policy_name": "password_policy",
        "resource_name_or_uuid": "metric_resource",
    }
)

# Public argument names that carry the identity of an HMC-side resource no
# allowlist can pin down. A tool accepting one cannot declare
# `exhaustive_targets=True`, so a policy `targets` table never grants it and only
# `all-targets` does.
#
# - `file_path` names a file on the HMC's own filesystem, which no TargetKind
#   expresses — ADR 0036 placed it outside every grant.
# - `cmd` is free-form console command text.
# - `vios_partition_id` is a *slot number within one managed system*, reused
#   across every system in a fleet, so an allowlist entry of "2" names a
#   different VIOS on each of them; unlike a partition name it has no UUID form
#   to fall back on, so there is no way to write it precisely.
# - `job_href` is a caller-supplied URI whose *path* replaces the `job_id`
#   selector entirely (`client.get_job_entry`), so the value authorized and the value
#   fetched are different values.
#
# Membership is decided by whether a `targets` table can bound the identity, and
# a name fails that by either of two routes. It cannot be written down at all
# (`cmd` is free-form text; a `vios_partition_id` of "2" names a different VIOS on
# every system), or it can be written and still designates something the declared
# selectors do not contain (`file_path`, `job_href`). Which filesystem the value
# refers to decides nothing: `file_path` is a member because an absolute console
# path is contained by no selector — for `rstprofdata -f`, which only reads it,
# exactly as for `bkprofdata -f` — and not because the file sits on the HMC.
#
# `backup_name` on `hmc_restore_vios` is the HMC-side name that made that
# explicit and is deliberately *not* a member: the selected VIOS backup catalog
# contains the name, so it is reached through containment from a declared
# selector. That holds only while the value cannot leave the catalog, which
# `server_tools.vios.core._validate_backup_name` is what enforces. ADR 0044 records the
# decision and the two questions it leaves open (#282, #283).
#
# This is not the complement of REQUIRED_TARGET_ARGUMENTS: `vios_partition_id`
# is in both, deliberately. It is a declared selector — so it is extracted and
# compared under `all-targets`, which is what keeps two live tools working —
# *and* an identity no table can bound. The two tables answer different
# questions about the same name and must not be merged.
#
# They are kept adjacent for that reason: together they are one piece of
# knowledge — which public argument names carry which identity — and a name that
# moved between them while they lived in different files would drift silently.
# No runtime path reads this one; the boolean it justifies is what the authorizer
# reads. The ADR 0039 guardrail in tests/app/test_tool_security.py enforces it.
UNBOUNDED_ARGUMENTS: frozenset[str] = frozenset(
    {
        "cmd",
        "file_path",
        "job_href",
        "vios_partition_id",
    }
)

# (readOnlyHint, destructiveHint) per effect class, held as immutable values
# rather than shared ToolAnnotations instances: the model is mutable, so a shared
# instance would let one in-place edit re-flag every tool of that class. `mutate`
# leaves destructiveHint unset — MCP defaults it to true, and asserting false
# would newly invite a client to auto-approve every mutating tool.
_ANNOTATIONS: Mapping[str, tuple[bool, bool | None]] = MappingProxyType(
    {
        "read": (True, None),
        "mutate": (False, None),
        "destructive": (False, True),
        "arbitrary-command": (False, True),
    }
)


@dataclass(frozen=True)
class TargetSelector:
    """A public handler argument carrying the identity of one target resource.

    *argument* is the handler parameter the identity arrives through, except
    when *container* names a structured (dataclass / pydantic-model) parameter:
    then it is a field of that object and the identity arrives one level below
    the signature (#260). ``path`` is the location as denial messages and audit
    records render it — unambiguous for the nested case, where a bare field
    name would not say which argument carries it.
    """

    kind: TargetKind
    argument: str
    required: bool
    container: str | None = None

    @property
    def path(self) -> str:
        """The selector's location, dotted when nested."""
        if self.container is None:
            return self.argument
        return f"{self.container}.{self.argument}"


@dataclass(frozen=True)
class ToolSecurity:
    """The authoritative security classification of one MCP tool.

    ``exhaustive_targets`` answers one question for the access policy: do the
    declared selectors name every resource this tool acts on, so a ``targets``
    table can bound the call? When false, only the ``all-targets`` sentinel can
    grant the tool — a table has either nothing to bind on (no selectors) or
    something it cannot see (a composite reaching an identity nested below the
    signature). See docs/adr/0039-dispatch-time-target-scope.md.

    Its default is the fail-closed value, so a record built by hand — as
    ``server_tools.command`` and ``server_tools.permissions`` build theirs — is safe without
    naming the field. Only :func:`tool_module`'s decorator, which has inspected a
    signature and found selectors, raises it.
    """

    effect: Effect
    operation: str
    target_kind: TargetKind
    targets: tuple[TargetSelector, ...] = ()
    connection_argument: str | None = "profile"
    exhaustive_targets: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    handler: Callable[..., Any]
    security: ToolSecurity

    @functools.cached_property
    def _schema_template(self) -> FunctionTool:
        # FastMCP normalizes injected-parameter signatures in place.
        handler = self.handler
        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def metadata_handler(*args: Any, **kwargs: Any) -> Any:
                return await handler(*args, **kwargs)

        else:

            @functools.wraps(handler)
            def metadata_handler(*args: Any, **kwargs: Any) -> Any:
                return handler(*args, **kwargs)

        return Tool.from_function(
            metadata_handler, annotations=annotations_for(self.security.effect)
        )

    def _bind(self, authorize: Authorize) -> FunctionTool:
        return self._schema_template.model_copy(
            deep=True,
            update={
                "fn": authorized(
                    self.name, self.security, self._schema_template.fn, authorize
                )
            },
        )


# The access policy's dispatch-time question, taken as a callable for the reason
# ADR 0037 takes the ceiling as one: `access_policy` imports this module, so the
# dependency must not run back the other way. It is given the tool name, its
# authoritative classification, and the call's bound arguments; it returns None
# to permit and raises to deny. See ADR 0038.
Authorize = Callable[[str, ToolSecurity, Mapping[str, Any]], None]
ToolHandler = Callable[..., Any]
ToolHandlerT = TypeVar("ToolHandlerT", bound=ToolHandler)
HandlerDecorator = Callable[[ToolHandlerT], ToolHandlerT]
ToolDecoratorFactory = Callable[..., HandlerDecorator]
RegisterTools = Callable[..., None]
ToolSecurityProvider = Callable[[], Mapping[str, ToolSecurity]]
ToolModule = tuple[ToolDecoratorFactory, RegisterTools, ToolSecurityProvider]

# Set on the wrapper `authorized` builds, and read by `is_authorized_wrapper`.
# An attribute rather than a signature or code-object shape: `functools.wraps`
# makes the wrapper indistinguishable from its handler by every attribute it
# copies, and this one is set afterwards, on the object this module created.
_AUTHORIZED_MARKER = "__hmc_dispatch_authorized__"


def is_authorized_wrapper(handler: object) -> bool:
    """True when *handler* is the dispatch-time authorization wrapper itself.

    The witness a caller needs to establish that a registered callable will run
    the connection and target checks (ADR 0038, ADR 0039) before the handler.
    It lives beside :func:`authorized` because the wrapper and its recogniser
    drift the moment they live apart.

    Takes an ``object`` rather than a callable: a caller reading a registry back
    may hold something that is not a function at all, and "not the wrapper" is
    the honest answer for it rather than a type error.
    """
    return getattr(handler, _AUTHORIZED_MARKER, False) is True


def _authorize_call(
    signature: inspect.Signature,
    name: str,
    security: ToolSecurity,
    authorize: Authorize,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    authorize(name, security, bound.arguments)


def _mark_authorized(wrapper: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a wrapper after ``functools.wraps`` copies handler attributes."""
    setattr(wrapper, _AUTHORIZED_MARKER, True)
    return wrapper


def authorized(
    name: str,
    security: ToolSecurity,
    handler: Callable[..., Any],
    authorize: Authorize,
) -> Callable[..., Any]:
    """Return a wrapper that authorizes the call before running *handler*.

    Every registered tool is wrapped, and the wrapper enforces connection and
    target scope before invoking the handler. The wrapper is always supplied an
    authorizer; registration cannot accidentally bypass policy enforcement.

    Arguments are bound against the handler's own signature rather than read out
    of ``kwargs``, so a selector passed positionally or left to its default is
    read correctly. ``functools.wraps`` sets ``__wrapped__``, which both
    ``inspect.signature`` and FastMCP's schema generation follow, so the
    registered tool's name, description, and parameter schema are unchanged.

    A coroutine handler gets a coroutine wrapper. ``functools.wraps`` does not
    copy ``__code__``, so ``inspect.iscoroutinefunction`` reads the wrapper's own
    identity and a sync wrapper around ``hmc_effective_permissions`` — the
    package's one async handler, and connection-less — would be registered as a
    plain function returning an un-awaited coroutine. Both branches name their
    inner function ``guarded`` because that name is a witness the suite reads.
    """
    signature = inspect.signature(handler)

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            _authorize_call(signature, name, security, authorize, args, kwargs)
            return await handler(*args, **kwargs)

        return _mark_authorized(guarded)

    @functools.wraps(handler)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        _authorize_call(signature, name, security, authorize, args, kwargs)
        return handler(*args, **kwargs)

    return _mark_authorized(guarded)


def annotations_for(effect: Effect) -> ToolAnnotations:
    """Return the MCP annotations for an effect class, as a fresh object."""
    read_only, destructive = _ANNOTATIONS[effect]
    return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive)


def build_targets(
    handler: Callable[..., Any],
    extra_targets: Iterable[tuple[TargetKind, str]],
) -> tuple[TargetSelector, ...]:
    """Build target selectors from the argument table plus explicit extras.

    An extra written ``"argument"`` names a parameter, as before. An extra
    written ``"container.field"`` declares a nested selector (#260): the
    identity arrives as *field* of the structured (dataclass / pydantic-model)
    parameter *container*, one level below the signature. The descent is a
    declaration rather than a derivation — ADR 0039 rejected inspecting
    signatures into authority at registration — so nothing a tool does not
    declare is extracted, and a dotted path is validated against the real
    fields at declaration time.
    """
    parameters = inspect.signature(handler).parameters
    selectors = [
        TargetSelector(kind, name, parameter.default is inspect.Parameter.empty)
        for name, parameter in parameters.items()
        if (kind := REQUIRED_TARGET_ARGUMENTS.get(name)) is not None
    ]
    hints = None
    for kind, argument in extra_targets:
        container, _, field = argument.rpartition(".")
        if not container:
            parameter = parameters.get(argument)
            required = (
                parameter is not None and parameter.default is inspect.Parameter.empty
            )
            selectors.append(TargetSelector(kind, argument, required))
            continue
        if hints is None:
            hints = get_type_hints(handler)
        selectors.append(_nested_selector(kind, container, field, parameters, hints))
    return tuple(selectors)


def _structured_fields(annotation: object) -> Mapping[str, bool] | None:
    """The fields of a dataclass or pydantic model, each to its requiredness.

    ``None`` for anything else — the signal that a container parameter cannot
    carry a nested selector.
    """
    if is_dataclass(annotation) and isinstance(annotation, type):
        return {
            field.name: (field.default is MISSING and field.default_factory is MISSING)
            for field in dataclass_fields(annotation)
        }
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {
            name: info.is_required() for name, info in annotation.model_fields.items()
        }
    return None


def _nested_selector(
    kind: TargetKind,
    container: str,
    field: str,
    parameters: Mapping[str, inspect.Parameter],
    hints: Mapping[str, object],
) -> TargetSelector:
    """One validated nested selector, or the ValueError that refuses it."""
    where = f"{container}.{field}"
    parameter = parameters.get(container)
    if parameter is None:
        raise ValueError(
            f"{where}: container {container!r} is not a parameter; "
            f"handler takes {sorted(parameters)}"
        )
    annotation = hints.get(container)
    if parameter.default is None or _is_optional(annotation):
        raise ValueError(
            f"{where}: cannot declare a nested selector on an optional "
            f"container; a call that passes None would be unreadable at "
            "dispatch and deny even under all-targets"
        )
    fields = _structured_fields(annotation)
    if fields is None:
        raise ValueError(
            f"{where}: container {container!r} is not a dataclass or pydantic "
            f"model; only a structured argument can carry a nested selector"
        )
    if field not in fields:
        raise ValueError(f"{where}: no such field; {container} has {sorted(fields)}")
    required = parameter.default is inspect.Parameter.empty and fields[field]
    return TargetSelector(kind, field, required, container=container)


def _is_optional(annotation: object) -> bool:
    """True when the annotation admits None."""
    return get_origin(annotation) is not None and type(None) in get_args(annotation)


def _validate_arguments(
    security: ToolSecurity,
    parameters: Mapping[str, inspect.Parameter],
    name: str,
) -> None:
    """Reject a selector or connection argument the handler does not accept."""
    for target in security.targets:
        if target.container is not None:
            if target.container not in parameters:
                raise ValueError(
                    f"{name}: nested selector container {target.container!r} is "
                    f"not a parameter; handler takes {sorted(parameters)}"
                )
            continue
        if target.argument not in parameters:
            raise ValueError(
                f"{name}: target argument {target.argument!r} is not a parameter; "
                f"handler takes {sorted(parameters)}"
            )
    if (
        security.connection_argument is not None
        and security.connection_argument not in parameters
    ):
        raise ValueError(
            f"{name}: connection argument {security.connection_argument!r} is not a "
            f"parameter; handler takes {sorted(parameters)}"
        )
    paths = [target.path for target in security.targets]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{name}: duplicate target argument in {sorted(paths)}")


def validate_security(security: ToolSecurity, handler: Callable[..., Any]) -> None:
    """Reject a declaration that is malformed or contradicts its handler."""
    name = getattr(handler, "__name__", "<handler>")
    if security.effect not in EFFECTS:
        raise ValueError(f"{name}: unknown effect {security.effect!r}")
    if not _OPERATION.match(security.operation):
        raise ValueError(
            f"{name}: operation {security.operation!r} must be '<domain>.<verb>'"
        )
    kinds = {security.target_kind, *(target.kind for target in security.targets)}
    if unknown := sorted(kinds - TARGET_KINDS):
        raise ValueError(f"{name}: unknown target_kind {unknown}")
    if security.exhaustive_targets and not security.targets:
        raise ValueError(
            f"{name}: exhaustive_targets requires at least one target selector; a "
            "policy targets table would have nothing to bind on"
        )

    _validate_arguments(security, inspect.signature(handler).parameters, name)

    if security.target_kind == "none":
        if security.targets or security.connection_argument is not None:
            raise ValueError(
                f"{name}: target_kind 'none' allows no targets and no connection argument"
            )
        return
    if security.target_kind != "console" and not any(
        target.kind == security.target_kind for target in security.targets
    ):
        raise ValueError(
            f"{name}: target_kind {security.target_kind!r} has no matching target "
            "selector; pass extra_targets when the argument table cannot name it"
        )


def build_tool_security(
    module_mappings: Iterable[Mapping[str, ToolSecurity]],
    extra: Mapping[str, ToolSecurity],
) -> Mapping[str, ToolSecurity]:
    """Merge per-module classifications, rejecting name and identity collisions."""
    index: dict[str, ToolSecurity] = {}
    operations: dict[str, str] = {}
    for mapping in [*module_mappings, extra]:
        for name, security in mapping.items():
            if name in index:
                raise ValueError(f"duplicate tool name {name!r}")
            if (owner := operations.get(security.operation)) is not None:
                raise ValueError(
                    f"duplicate operation {security.operation!r} on {owner!r} and {name!r}"
                )
            index[name] = security
            operations[security.operation] = name
    return MappingProxyType(index)


class _ToolCollector:
    """Collect one handler with its immutable security declaration."""

    def __init__(
        self,
        definitions: list[ToolDefinition],
        *,
        effect: Effect,
        operation: str,
        target_kind: TargetKind,
        extra_targets: Iterable[tuple[TargetKind, str]],
        connection_argument: str | None,
        exhaustive_targets: bool,
    ) -> None:
        self._definitions = definitions
        self._effect = effect
        self._operation = operation
        self._target_kind = target_kind
        self._extra_targets = extra_targets
        self._connection_argument = connection_argument
        self._exhaustive_targets = exhaustive_targets

    def __call__(self, fn: ToolHandlerT) -> ToolHandlerT:
        name = getattr(fn, "__name__", "<handler>")
        security = ToolSecurity(
            effect=self._effect,
            operation=self._operation,
            target_kind=self._target_kind,
            connection_argument=self._connection_argument,
        )
        try:
            targets = build_targets(fn, self._extra_targets)
        except Exception as error:
            raise ValueError(f"{name}: cannot inspect signature: {error!r}") from error
        security = replace(
            security,
            targets=targets,
            exhaustive_targets=self._exhaustive_targets and bool(targets),
        )
        validate_security(security, fn)
        self._definitions.append(ToolDefinition(name, fn, security))
        return fn


class _ToolModuleBuilder:
    """Own the definitions collected by one module-local registration tuple."""

    def __init__(self) -> None:
        self.definitions: list[ToolDefinition] = []

    def tool(
        self,
        *,
        effect: Effect,
        operation: str,
        target_kind: TargetKind,
        extra_targets: Iterable[tuple[TargetKind, str]] = (),
        connection_argument: str | None = "profile",
        exhaustive_targets: bool = True,
    ) -> HandlerDecorator:
        return _ToolCollector(
            self.definitions,
            effect=effect,
            operation=operation,
            target_kind=target_kind,
            extra_targets=extra_targets,
            connection_argument=connection_argument,
            exhaustive_targets=exhaustive_targets,
        )

    def register_tools(
        self,
        mcp: FastMCP,
        *,
        permits: Callable[[str], bool],
        authorize: Authorize,
    ) -> None:
        """Register this module's tools, skipping any the ceiling withholds.

        *permits* is the access policy's ceiling question and *authorize* its
        dispatch-time question. Both are required since ADR 0041: this is the
        bulk registration site, and while they defaulted to ``None`` a caller
        that omitted them registered a module's entire tool set with no ceiling
        and no authorizer, silently and without error. Both are taken as
        callables rather than the policy object because ``access_policy``
        imports this module; see ADR 0037 and ADR 0038.
        """
        for definition in self.definitions:
            if not permits(definition.name):
                continue
            mcp.add_tool(definition._bind(authorize))

    def tool_security(self) -> Mapping[str, ToolSecurity]:
        return MappingProxyType(
            {definition.name: definition.security for definition in self.definitions}
        )


def tool_module() -> ToolModule:
    """Return a module-local decorator, registration function, and classifications."""
    module = _ToolModuleBuilder()
    return module.tool, module.register_tools, module.tool_security
