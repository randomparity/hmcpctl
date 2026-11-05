"""Direct contract tests for module-local MCP tool collection."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from hmcpctl._app import create_mcp
from hmcpctl.tool_registry import (
    EFFECTS,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    authorized,
    build_tool_security,
    tool_module,
    validate_security,
)

# Both gates are required since ADR 0041: `register_tools` is the bulk registration
# site, and while they defaulted to None a caller that omitted them registered a
# module's whole tool set with no ceiling and no authorizer. These tests are about
# collection and isolation rather than about the policy, so they pass gates that admit
# everything and wrap nothing.
_GATES = {"permits": lambda _name: True, "authorize": lambda *_args: None}


def _tool_names(application) -> set[str]:
    return {tool.name for tool in asyncio.run(application.list_tools())}


def test_tool_modules_collect_definitions_in_isolation():
    first_tool, first_register, _ = tool_module()
    second_tool, second_register, _ = tool_module()

    @first_tool(
        effect="read",
        operation="first.read",
        target_kind="console",
        connection_argument=None,
    )
    def first_handler() -> str:
        return "first"

    @second_tool(
        effect="read",
        operation="second.read",
        target_kind="console",
        connection_argument=None,
    )
    def second_handler() -> str:
        return "second"

    first_application = create_mcp()
    second_application = create_mcp()
    first_register(first_application, **_GATES)
    second_register(second_application, **_GATES)

    assert _tool_names(first_application) == {"first_handler"}
    assert _tool_names(second_application) == {"second_handler"}


def test_tool_module_derives_annotations_and_preserves_handler():
    tool, register_tools, security = tool_module()

    @tool(
        effect="read",
        operation="status.read",
        target_kind="console",
        connection_argument=None,
    )
    def status() -> str:
        return "ok"

    application = create_mcp()
    register_tools(application, **_GATES)
    registered = asyncio.run(application.list_tools())

    assert status() == "ok"
    assert len(registered) == 1
    assert registered[0].name == "status"
    assert registered[0].annotations == annotations_for("read")
    assert security()["status"].operation == "status.read"


def test_same_definitions_register_on_independent_applications():
    tool, register_tools, _ = tool_module()

    @tool(
        effect="read",
        operation="ping.read",
        target_kind="console",
        connection_argument=None,
    )
    def ping() -> str:
        return "pong"

    first = create_mcp()
    second = create_mcp()
    register_tools(first, **_GATES)
    register_tools(second, **_GATES)

    assert _tool_names(first) == {"ping"}
    assert _tool_names(second) == {"ping"}


def test_targets_are_built_from_the_argument_table():
    tool, _register, security = tool_module()

    @tool(effect="mutate", operation="lpar.migrate", target_kind="lpar")
    def migrate(
        lpar_name_or_uuid: str,
        target_system_name_or_uuid: str,
        system_name_or_uuid: str | None = None,
        profile: str | None = None,
    ) -> str:
        return "ok"

    targets = security()["migrate"].targets
    assert [(t.kind, t.argument, t.required) for t in targets] == [
        ("lpar", "lpar_name_or_uuid", True),
        ("managed_system", "target_system_name_or_uuid", True),
        ("managed_system", "system_name_or_uuid", False),
    ]


def test_extra_targets_supply_a_kind_the_table_cannot_name():
    tool, _register, security = tool_module()

    @tool(
        effect="destructive",
        operation="user.delete",
        target_kind="user",
        extra_targets=(("user", "name"),),
    )
    def remove_user(name: str, profile: str | None = None) -> str:
        return "ok"

    targets = security()["remove_user"].targets
    assert [(t.kind, t.argument, t.required) for t in targets] == [
        ("user", "name", True)
    ]


def test_affinity_operations_bind_only_the_managed_system_selector():
    from hmcpctl.server import TOOL_SECURITY

    names = (
        "hmc_get_system_memopt_score",
        "hmc_plan_lpar_memopt_scores",
        "hmc_plan_system_memopt_score",
    )
    for name in names:
        security = TOOL_SECURITY[name]
        assert security.effect == "read"
        assert [(target.kind, target.argument) for target in security.targets] == [
            ("managed_system", "system_name_or_uuid")
        ]


# ---------------------------------------------------------------------------
# #260 — declared nested target selectors, one level below the signature
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Storage:
    """One fleet-unique VIOS identity field, as ProvisionStorage carries."""

    vios_uuid: str
    storage_name: str = "disk1"


@dataclass(frozen=True)
class _Network:
    """Every identity-bearing field defaulted, as a slot number can be."""

    port_vlan_id: int = 1
    vios_partition_id: int = 3


@dataclass(frozen=True)
class _Model:
    """Module-level so `get_type_hints` can resolve the annotation."""

    vios_uuid: str


class _PydanticModel(BaseModel):
    """The pydantic half of the structured-container contract."""

    vios_uuid: str


def test_a_dotted_extra_declares_a_nested_selector():
    tool, _register, security = tool_module()

    @tool(
        effect="mutate",
        operation="provision.lpar",
        target_kind="managed_system",
        extra_targets=(("vios", "storage.vios_uuid"),),
    )
    def provision(
        system_name_or_uuid: str, storage: _Storage, profile: str | None = None
    ) -> str:
        return "ok"

    assert [
        (t.kind, t.argument, t.required) for t in security()["provision"].targets
    ] == [
        ("managed_system", "system_name_or_uuid", True),
        ("vios", "vios_uuid", True),
    ]
    selector = security()["provision"].targets[1]
    assert selector.container == "storage"
    assert selector.path == "storage.vios_uuid"


def test_a_nested_field_with_a_default_is_optional():
    tool, _register, security = tool_module()

    @tool(
        effect="mutate",
        operation="provision.lpar",
        target_kind="managed_system",
        extra_targets=(
            ("vios", "adapters.vios_partition_id"),
            ("vios", "storage.vios_uuid"),
        ),
    )
    def provision(
        system_name_or_uuid: str,
        adapters: _Network,
        storage: _Storage,
        profile: str | None = None,
    ) -> str:
        return "ok"

    assert [(t.argument, t.required) for t in security()["provision"].targets] == [
        ("system_name_or_uuid", True),
        ("vios_partition_id", False),
        ("vios_uuid", True),
    ]


def test_a_nested_selector_on_a_pydantic_model_is_declared():
    tool, _register, security = tool_module()

    @tool(
        effect="mutate",
        operation="provision.lpar",
        target_kind="managed_system",
        extra_targets=(("vios", "model.vios_uuid"),),
    )
    def provision(
        system_name_or_uuid: str, model: _PydanticModel, profile: str | None = None
    ) -> str:
        return "ok"

    selector = security()["provision"].targets[1]
    assert (selector.container, selector.argument) == ("model", "vios_uuid")


@pytest.mark.parametrize(
    "extra, message",
    [
        ((("vios", "absent.vios_uuid"),), "absent"),
        ((("vios", "storage.nothing"),), "nothing"),
        ((("vios", "name.vios_uuid"),), "structured"),
        ((("vios", "spare.vios_uuid"),), "optional"),
    ],
    ids=[
        "absent-container",
        "absent-field",
        "unstructured-container",
        "optional-container",
    ],
)
def test_tool_rejects_impossible_nested_declarations(extra, message):
    tool, _register, _security = tool_module()

    with pytest.raises(ValueError, match=message):

        @tool(
            effect="read",
            operation="a.b",
            target_kind="console",
            extra_targets=extra,
        )
        def sample(
            name: str,
            storage: _Storage,
            spare: _Storage | None = None,
            profile: str | None = None,
        ) -> str:
            return "ok"


def test_two_identical_dotted_extras_are_rejected():
    tool, _register, _security = tool_module()

    with pytest.raises(ValueError, match="duplicate"):

        @tool(
            effect="read",
            operation="a.b",
            target_kind="lpar",
            extra_targets=(
                ("vios", "storage.vios_uuid"),
                ("vios", "storage.vios_uuid"),
            ),
        )
        def sample(
            vios_uuid: str, storage: _Storage, profile: str | None = None
        ) -> str:
            return "ok"


def test_validate_security_rejects_a_nested_container_that_is_not_a_parameter():
    def handler(storage: _Storage, profile: str | None = None) -> str:
        return "ok"

    security = ToolSecurity(
        effect="mutate",
        operation="a.b",
        target_kind="lpar",
        targets=(TargetSelector("vios", "vios_uuid", True, container="elsewhere"),),
    )
    with pytest.raises(ValueError, match="elsewhere"):
        validate_security(security, handler)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {"effect": "nonsense", "operation": "a.b", "target_kind": "console"},
            "effect",
        ),
        (
            {"effect": "read", "operation": "a.b", "target_kind": "nowhere"},
            "target_kind",
        ),
        (
            {"effect": "read", "operation": "nodot", "target_kind": "console"},
            "operation",
        ),
        (
            {
                "effect": "read",
                "operation": "a.b",
                "target_kind": "console",
                "extra_targets": (("lpar", "absent_argument"),),
            },
            "absent_argument",
        ),
        (
            {
                "effect": "read",
                "operation": "a.b",
                "target_kind": "console",
                "connection_argument": "absent_profile",
            },
            "absent_profile",
        ),
        ({"effect": "read", "operation": "a.b", "target_kind": "none"}, "none"),
        ({"effect": "read", "operation": "a.b", "target_kind": "user"}, "user"),
        (
            {
                "effect": "read",
                "operation": "a.b",
                "target_kind": "console",
                "extra_targets": (("lpar", "lpar_name_or_uuid"),),
            },
            "lpar_name_or_uuid",
        ),
    ],
    ids=[
        "v2-effect",
        "v3-kind",
        "v4-operation",
        "v5-extra-arg",
        "v6-connection",
        "v7-none",
        "v8-no-subject",
        "v9-duplicate",
    ],
)
def test_tool_rejects_contradictory_declarations(kwargs, message):
    tool, _register, _security = tool_module()

    with pytest.raises(ValueError, match=message):

        @tool(**kwargs)
        def sample(lpar_name_or_uuid: str, profile: str | None = None) -> str:
            return "ok"


def test_tool_requires_the_three_mandatory_fields():
    tool, _register, _security = tool_module()

    with pytest.raises(TypeError):

        @tool(effect="read")
        def sample(profile: str | None = None) -> str:
            return "ok"


def test_annotations_cover_exactly_the_effect_vocabulary():
    expected = {
        "read": (True, None),
        "mutate": (False, None),
        "destructive": (False, True),
        "arbitrary-command": (False, True),
    }
    assert set(expected) == EFFECTS
    for effect, (read_only, destructive) in expected.items():
        wire = annotations_for(effect).model_dump(by_alias=True)
        assert wire["readOnlyHint"] is read_only, effect
        assert wire["destructiveHint"] is destructive, effect
    with pytest.raises(KeyError):
        annotations_for("invented")  # ty: ignore[invalid-argument-type]


def test_required_uses_absence_of_a_default_not_a_none_default():
    tool, _register, security = tool_module()

    @tool(effect="read", operation="pinned.read", target_kind="managed_system")
    def pinned(
        system_name_or_uuid: str = "Server-1", profile: str | None = None
    ) -> str:
        return "ok"

    selector = security()["pinned"].targets[0]
    assert selector.required is False
    assert (
        inspect.signature(pinned).parameters["system_name_or_uuid"].default
        == "Server-1"
    )


def test_build_tool_security_rejects_duplicate_names_and_operations():
    console = ToolSecurity(effect="read", operation="a.read", target_kind="console")
    other = ToolSecurity(effect="read", operation="b.read", target_kind="console")

    with pytest.raises(ValueError, match="duplicate tool name"):
        build_tool_security([{"one": console}, {"one": other}], {})

    with pytest.raises(ValueError, match="duplicate operation"):
        build_tool_security([{"one": console}, {"two": console}], {})

    with pytest.raises(ValueError, match="duplicate tool name"):
        build_tool_security([{"one": console}], {"one": other})


def test_build_tool_security_merges_modules_and_extras():
    first = ToolSecurity(effect="read", operation="a.read", target_kind="console")
    second = ToolSecurity(effect="mutate", operation="b.write", target_kind="console")

    index = build_tool_security([{"one": first}], {"two": second})

    assert index == {"one": first, "two": second}


def test_module_classifications_are_read_only():
    tool, _register, security = tool_module()

    @tool(
        effect="read",
        operation="probe.read",
        target_kind="console",
        connection_argument=None,
    )
    def probe() -> str:
        return "ok"

    with pytest.raises(TypeError):
        security()["probe"] = ToolSecurity(
            effect="destructive", operation="probe.wipe", target_kind="console"
        )


def test_a_handler_whose_signature_cannot_be_read_is_named_in_the_error():
    tool, _register, _security = tool_module()

    class Unreadable:
        __name__ = "hmc_unreadable_tool"

        @property
        def __signature__(self):
            raise RuntimeError("signature unavailable")

        def __call__(self, profile: str | None = None) -> str:
            return "ok"

    with pytest.raises(ValueError, match="hmc_unreadable_tool"):
        tool(effect="read", operation="a.b", target_kind="console")(Unreadable())


def test_validate_security_accepts_a_console_declaration_with_no_targets():
    def handler(profile: str | None = None) -> str:
        return "ok"

    validate_security(
        ToolSecurity(effect="read", operation="console.info", target_kind="console"),
        handler,
    )


# ---------------------------------------------------------------------------
# R7 (#223) — exhaustive_targets: can a policy `targets` table bound this tool?
# ---------------------------------------------------------------------------


def test_a_decorated_tool_with_selectors_is_exhaustive_by_default():
    tool, _register, security = tool_module()

    @tool(effect="destructive", operation="lpar.delete", target_kind="lpar")
    def delete_lpar(lpar_name_or_uuid: str, profile: str | None = None) -> str:
        return "ok"

    assert security()["delete_lpar"].exhaustive_targets is True


def test_a_selector_less_tool_is_never_exhaustive():
    """A `targets` table has nothing to bind on, so no default can make it True.

    This is the fail-open ADR 0039 refuses: without it, a grant reading
    `targets = {lpar = ["scratch-01"]}` would still authorize a console-wide
    destructive tool because the table never gets to say no.
    """
    tool, _register, security = tool_module()

    @tool(effect="destructive", operation="ldap.remove", target_kind="console")
    def remove_ldap(resource: str, profile: str | None = None) -> str:
        return "ok"

    assert security()["remove_ldap"].targets == ()
    assert security()["remove_ldap"].exhaustive_targets is False


def test_a_composite_may_declare_itself_unbounded_despite_having_selectors():
    tool, _register, security = tool_module()

    @tool(
        effect="mutate",
        operation="provision.lpar",
        target_kind="managed_system",
        exhaustive_targets=False,
    )
    def provision(system_name_or_uuid: str, profile: str | None = None) -> str:
        return "ok"

    assert security()["provision"].targets != ()
    assert security()["provision"].exhaustive_targets is False


def test_a_directly_constructed_record_is_not_exhaustive():
    """The stored default is the fail-closed one, for records built by hand.

    `HMC_RUN_COMMAND_SECURITY` and `EFFECTIVE_PERMISSIONS_SECURITY` are built
    this way; so is every record a test writes. Only the decorator, which has
    inspected a signature, may raise it.
    """
    assert (
        ToolSecurity(
            effect="mutate", operation="a.b", target_kind="console"
        ).exhaustive_targets
        is False
    )


def test_exhaustive_targets_without_a_selector_is_rejected():
    def handler(profile: str | None = None) -> str:
        return "ok"

    security = ToolSecurity(
        effect="mutate",
        operation="a.b",
        target_kind="console",
        exhaustive_targets=True,
    )
    with pytest.raises(ValueError, match="exhaustive_targets"):
        validate_security(security, handler)


def test_the_wrapper_of_a_coroutine_handler_is_itself_a_coroutine_function():
    """#297: wrapping every tool put an async handler through `authorized`.

    `hmc_effective_permissions` is the package's only coroutine handler and was
    left unwrapped until #297 precisely because it declares no connection
    argument. `functools.wraps` does not copy `__code__`, so a synchronous
    wrapper would leave `inspect.iscoroutinefunction` false on the registered
    callable while it returned a coroutine — the wrapper is signature-transparent
    by design, and coroutine-ness is part of what a caller reads off it.
    """
    calls: list[str] = []

    async def handler(profile: str | None = None) -> str:
        return "ok"

    security = ToolSecurity(effect="read", operation="a.b", target_kind="console")
    guarded = authorized("t", security, handler, lambda name, *_a: calls.append(name))

    assert inspect.iscoroutinefunction(guarded)
    assert asyncio.run(guarded("lab")) == "ok"
    assert calls == ["t"], "the check must run before the coroutine is awaited"


def test_the_wrapper_authorizes_before_a_coroutine_handler_runs():
    """A denial from an async tool must not reach the handler at all."""
    reached: list[str] = []

    async def handler(profile: str | None = None) -> str:
        reached.append("ran")
        return "ok"

    def deny(*_args) -> None:
        raise RuntimeError("denied")

    security = ToolSecurity(effect="read", operation="a.b", target_kind="console")
    guarded = authorized("t", security, handler, deny)

    with pytest.raises(RuntimeError, match="denied"):
        asyncio.run(guarded("lab"))
    assert reached == []


@pytest.mark.parametrize(
    "effect,kind,argument",
    [
        ("read", "managed_system", "system_name_or_uuid"),
        ("mutate", "lpar", "system_name_or_uuid"),
        ("mutate", "managed_system", "other"),
    ],
)
def test_system_write_marker_requires_mutation_and_required_system_selector(
    effect, kind, argument
):
    def handler(**kwargs):
        pass

    handler.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                argument, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
        ]
    )
    tool, _, _ = tool_module()
    with pytest.raises(ValueError, match="system_partition_write"):
        tool(
            effect=effect,
            operation="system.write",
            target_kind=kind,
            connection_argument=None,
            system_partition_write=True,
        )(handler)
