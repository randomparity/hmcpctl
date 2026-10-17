"""Composition-time capability ceiling and effective-permission inspection.

Covers docs/workflow/specs/2026-08-18-capability-ceiling-design.md; the decision
record is docs/adr/0037-composition-time-capability-ceiling.md.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP
from rendered_output import collapse_whitespace

from hmcpctl.audit import sink as audit_sink
from hmcpctl.authorization.access_policy import (
    DEFAULT_CONNECTION_TOKEN,
    compile_access_policy,
)
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.operations.inventory.capacity import CapacitySummary
from hmcpctl.server import (
    PERMISSIONS_TOOL_NAME,
    TOOL_SECURITY,
    create_mcp,
)
from hmcpctl.server_tools.inventory import capacity


def _legacy(*, include_arbitrary_command: bool = False):
    """The policy that replaced the unfiltered default composition (ADR 0041)."""
    return compile_legacy_policy(
        TOOL_SECURITY,
        (DEFAULT_CONNECTION_TOKEN,),
        include_arbitrary_command=include_arbitrary_command,
    )


SOURCE = "test-access-policy.toml"


def _policy(grants: list[dict], name: str = "test"):
    """Compile one named policy from grant tables, against the live tool index."""
    return compile_access_policy(
        {"policies": {name: {"grants": grants}}}, name, TOOL_SECURITY, SOURCE
    )


def _names(application) -> set[str]:
    return {tool.name for tool in asyncio.run(application.list_tools())}


def _call(application, tool: str, arguments: dict):
    async def _go():
        async with Client(application) as client:
            result = await client.call_tool(tool, arguments)
            return result.structured_content

    return asyncio.run(_go())


READ_ONLY_GRANT = [
    {"effects": ["read"], "connections": ["<default>"], "targets": "all-targets"}
]


def test_fastmcp_serializes_capacity_dataclasses_without_adapter_conversion(
    monkeypatch,
):
    summary = CapacitySummary(
        system_uuid="system-id",
        system_name="system",
        total_memory_mib=1024,
        assigned_memory_mib=512,
        free_memory_mib=512,
        total_proc_units=2.0,
        assigned_proc_units=1.0,
        free_proc_units=1.0,
        total_lpars=1,
        running_lpars=1,
    )
    monkeypatch.setattr(
        capacity, "with_client", lambda _operation, *, profile=None: [summary]
    )
    application = FastMCP("capacity-serialization")
    capacity.register_tools(
        application,
        permits=lambda _name: True,
        authorize=lambda *_args: None,
    )

    result = _call(application, "hmc_capacity_report", {})

    assert result == {"result": [summary.__dict__]}


def test_a_read_only_policy_registers_only_read_tools():
    """R1: the registry is exactly the ceiling, minus the escape hatch."""
    policy = _policy(READ_ONLY_GRANT)
    names = _names(create_mcp(policy))

    assert names == {name for name in TOOL_SECURITY if policy.permits_tool(name)} - {
        "hmc_run_command"
    }
    assert names
    assert all(TOOL_SECURITY[name].effect == "read" for name in names)
    assert "hmc_delete_lpar" not in names


def test_a_withheld_tool_cannot_be_called_by_name():
    """R1: absence is the control — unlisted is not enough, it must not dispatch.

    Listing and dispatch read the same registry today, so this holds by
    construction. #222 adds authorization at the dispatch boundary, which is the
    change that could filter the listing while leaving dispatch open; this is the
    invariant it has to preserve.
    """
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    application = create_mcp(_policy(READ_ONLY_GRANT))

    async def _call():
        async with Client(application) as client:
            return await client.call_tool(
                "hmc_delete_lpar", {"lpar_name_or_uuid": "victim"}
            )

    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(_call())


def test_no_policy_applies_no_ceiling():
    """R2, inverted by ADR 0041: there is no unfiltered composition to have a ceiling.

    This asserted that `create_mcp()` registered every tool but `hmc_run_command`. A
    policy is mandatory now, so the ceiling-free registry this described cannot be
    built at all. The claim it made about the *surface* did not disappear with it — it
    moved to the legacy-equivalent policy, where G2 in
    tests/app/test_fail_closed_startup.py pins the same set.
    """
    with pytest.raises(TypeError):
        create_mcp()  # ty: ignore[missing-argument]

    with pytest.raises(TypeError, match="init-access-policy"):
        create_mcp(None)  # ty: ignore[invalid-argument-type]


def test_applications_composed_with_different_policies_are_independent():
    """R3: a restrictive composition does not leak into a later one."""
    restricted = _names(create_mcp(_policy(READ_ONLY_GRANT)))
    unrestricted = _names(create_mcp(_legacy()))
    restricted_again = _names(create_mcp(_policy(READ_ONLY_GRANT)))

    assert restricted == restricted_again
    assert restricted < unrestricted


def _inspect(application):
    """Call the registered inspection tool through the application.

    Reads ``structured_content`` (a plain dict), not ``data`` — on
    fastmcp 3.4.7 ``data`` is a pydantic model reconstructed from the output
    schema, which is neither subscriptable nor iterable.
    """
    from fastmcp import Client

    async def _go():
        async with Client(application) as client:
            result = await client.call_tool("hmc_effective_permissions", {})
            return result.structured_content

    return asyncio.run(_go())


def test_inspection_is_registered_and_classified():
    """R10: an ordinary read-effect record with no connection argument."""
    security = TOOL_SECURITY["hmc_effective_permissions"]

    assert security.effect == "read"
    assert security.operation == "permissions.describe"
    assert security.target_kind == "none"
    assert security.targets == ()
    assert security.connection_argument is None
    assert "hmc_effective_permissions" in _names(create_mcp(_legacy()))


def test_inspection_is_subject_to_the_ceiling():
    """R11: an effects grant reaches it; a tools-only grant can withhold it."""
    assert "hmc_effective_permissions" in _names(create_mcp(_policy(READ_ONLY_GRANT)))

    withheld = _policy(
        [
            {
                "tools": ["hmc_list_systems"],
                "connections": ["lab"],
                "targets": "all-targets",
            }
        ]
    )
    assert "hmc_effective_permissions" not in _names(create_mcp(withheld))


def test_inspection_matches_the_registry_and_the_policy():
    """R12: equal to the live registry, and to the policy-derived expectation."""
    policy = _policy(READ_ONLY_GRANT)
    application = create_mcp(policy)

    reported = [tool["name"] for tool in _inspect(application)["tools"]]
    live = sorted(tool.name for tool in asyncio.run(application.list_tools()))
    expected = sorted(
        {name for name in TOOL_SECURITY if policy.permits_tool(name)}
        - {"hmc_run_command"}
    )

    assert reported == live == expected


def test_inspection_reports_effects_source_and_dimensions():
    """R13, R14, R15, R16: what the payload says about the policy."""
    policy = _policy(READ_ONLY_GRANT)
    result = _inspect(create_mcp(policy))

    assert result["effects"] == ["read"]
    assert result["policy_name"] == "test"
    assert result["policy_source"] == SOURCE
    assert result["ceiling_enforced"] is True
    assert result["enforced_dimensions"] == ["tools", "connections", "targets"]
    assert result["declared_only_dimensions"] == []

    grant = result["declared_grants"][0]
    assert grant["connections"] == ["<default>"]
    assert grant["all_targets"] is True
    assert grant["targets"] == {}
    assert "hmc_list_systems" in grant["tools"]


def _guarded_stub(name: str = "hmc_list_systems"):
    """A registered callable built the way composition builds one.

    Through `authorized` rather than by setting the marker by hand: a stub that
    forged the witness would let the recogniser rot without a test noticing.
    """
    from hmcpctl.tool_registry import authorized

    def handler(profile: str | None = None) -> str:
        return "ok"

    return authorized(name, TOOL_SECURITY[name], handler, lambda *_args: None)


def test_inspection_reports_no_policy_honestly():
    """R14, R16, retargeted by ADR 0041: no *composed* application reaches this arm.

    The behaviour is unchanged and still worth pinning — `build_effective_permissions` documents itself as
    binding a direct caller, and ADR 0041 deliberately kept its `AccessPolicy | None`
    parameter rather than narrowing the public report's schema. What changed is the
    route: `create_mcp()` no longer produces an application to ask, so the honest
    null-reporting is asserted against `build_effective_permissions` itself.

    Both dimension tuples are empty here and that is not the partition ADR 0047
    imposes elsewhere: with no policy selected nothing is declared, so nothing can
    be declared-only either.
    """
    from dataclasses import asdict

    from hmcpctl.server_tools.permissions import build_effective_permissions

    result = asdict(
        build_effective_permissions(
            {"hmc_list_systems": _guarded_stub()}, None, TOOL_SECURITY, ()
        )
    )

    assert result["policy_name"] is None
    assert result["policy_source"] is None
    assert result["ceiling_enforced"] is False
    assert result["enforced_dimensions"] == ()
    assert result["declared_only_dimensions"] == ()
    assert result["declared_grants"] == ()


def test_inspection_reports_grants_separately_without_merging():
    """R15: one entry per grant, in document order, never unioned."""
    policy = _policy(
        [
            {"effects": ["read"], "connections": ["lab"], "targets": "all-targets"},
            {
                "tools": ["hmc_delete_lpar"],
                "connections": ["scratch"],
                "targets": {"lpar": ["db-01"], "managed_system": ["sys-a"]},
            },
        ]
    )
    grants = _inspect(create_mcp(policy))["declared_grants"]

    assert [grant["connections"] for grant in grants] == [["lab"], ["scratch"]]
    assert grants[1]["all_targets"] is False
    assert grants[1]["targets"] == {"lpar": ["db-01"], "managed_system": ["sys-a"]}


def test_inspection_reports_each_grant_authored_effects():
    """#251: each grant discloses the effect classes it authored, not only the
    resolved tool union. An `effects = ["read"]` grant and a named-tool list
    render identically in `tools`, so authorship must be its own field."""
    policy = _policy(
        [
            {"effects": ["read"], "connections": ["lab"], "targets": "all-targets"},
            {
                "tools": ["hmc_delete_lpar"],
                "connections": ["scratch"],
                "targets": {"lpar": ["db-01"], "managed_system": ["sys-a"]},
            },
        ]
    )
    grants = _inspect(create_mcp(policy))["declared_grants"]

    assert [grant["effects"] for grant in grants] == [["read"], []]


def test_inspection_reports_a_mixed_grant_effects_besides_named_tools():
    """#251: a grant authored with both an effect class and named tools reports
    both — the authored classes in `effects`, everything it resolves to in
    `tools`."""
    policy = _policy(
        [
            {
                "effects": ["read"],
                "tools": ["hmc_power_off_lpar"],
                "connections": ["lab"],
                "targets": "all-targets",
            },
        ]
    )
    grants = _inspect(create_mcp(policy))["declared_grants"]

    assert grants[0]["effects"] == ["read"]
    assert "hmc_power_off_lpar" in grants[0]["tools"]
    assert "hmc_list_systems" in grants[0]["tools"]


def test_inspection_does_not_raise_on_a_tool_outside_the_index():
    """R13: an unindexed registered name is reported, not fatal."""
    application = create_mcp(_legacy())

    def hmc_not_in_the_index() -> str:
        """A tool registered outside the authoritative index."""
        return "ok"

    application.tool(hmc_not_in_the_index)
    result = _inspect(application)
    reported = {tool["name"]: tool for tool in result["tools"]}

    assert reported["hmc_not_in_the_index"]["effect"] == "unknown"
    assert reported["hmc_not_in_the_index"]["operation"] == "unknown"
    assert reported["hmc_not_in_the_index"]["target_kind"] == "unknown"
    assert "unknown" not in result["effects"]


def test_inspection_carries_only_allowlisted_value_sources(monkeypatch):
    """R17: the payload's keys are an allowlist, and no connection value leaks.

    The sentinels must exist in the process for their absence to mean anything —
    an unset variable is absent from every payload, correct or leaking — and they
    are checked as *values*, since a leak of HMC_PASSWORD carries its value and
    not its name.

    `power_ownership_guards` is the one key that reads configuration, and #470 is
    why: the ADR 0092 §4 guard fails open, so its effective value has to be
    readable from the process that would act on it. It carries that one boolean
    and the connection names a grant already declares — never a host, user,
    credential, or any other `HMC_*` value. The sentinel check below now covers
    that key too.
    """
    monkeypatch.setenv("HMC_HOST", "sentinel-host-do-not-leak")
    monkeypatch.setenv("HMC_USER", "sentinel-user-do-not-leak")
    monkeypatch.setenv("HMC_PASSWORD", "sentinel-password-do-not-leak")

    policy = _policy(READ_ONLY_GRANT)
    result = _inspect(create_mcp(policy))

    assert set(result) == {
        "policy_name",
        "policy_source",
        "ceiling_enforced",
        "effects",
        "tools",
        "declared_grants",
        "enforced_dimensions",
        "declared_only_dimensions",
        "power_ownership_guards",
    }
    rendered = repr(result)
    for sentinel in (
        "sentinel-host-do-not-leak",
        "sentinel-user-do-not-leak",
        "sentinel-password-do-not-leak",
    ):
        assert sentinel not in rendered


def _configure(application, enabled, permits=None, policy=None):
    """Drive the escape-hatch toggle the way `_serve_application` drives it.

    Both gates are required since ADR 0041: this is the one registration site outside
    `create_mcp`, and while they defaulted to None a call here registered
    `hmc_run_command` with no ceiling and no authorizer whatever the policy said.
    `permits` stays a separate parameter because these tests vary the ceiling
    independently of the authorizer to isolate which gate withheld the tool.
    """
    from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
    from hmcpctl.server_tools.command import configure_arbitrary_command_tool

    effective = (
        policy if policy is not None else _legacy(include_arbitrary_command=True)
    )
    asyncio.run(
        configure_arbitrary_command_tool(
            enabled,
            application,
            permits=permits if permits is not None else effective.permits_tool,
            authorize=dispatch_authorizer(effective),
        )
    )


# Reaches the escape hatch by name (ADR 0036 forbids granting it by effect
# class) and the rest of the read class by effect — including
# hmc_effective_permissions, without which _inspect has no tool to call.
GRANT_RUN_COMMAND = [
    {
        "effects": ["read"],
        "tools": ["hmc_run_command"],
        "connections": ["<default>"],
        "targets": "all-targets",
    }
]


def test_escape_hatch_needs_both_the_flag_and_the_grant():
    """R5: the flag and the ceiling compose conjunctively."""
    granted = _policy(GRANT_RUN_COMMAND)
    application = create_mcp(granted)

    _configure(application, True, granted.permits_tool)
    assert "hmc_run_command" in _names(application)

    _configure(application, False, granted.permits_tool)
    assert "hmc_run_command" not in _names(application)


def test_escape_hatch_is_withheld_when_the_policy_omits_it():
    """R5, R6: an effect-class policy cannot reach it, flag or no flag."""
    every_effect = _policy(
        [
            {
                "effects": ["read", "mutate", "destructive"],
                "connections": ["<default>"],
                "targets": "all-targets",
            }
        ]
    )
    assert every_effect.permits_tool("hmc_run_command") is False

    application = create_mcp(every_effect)
    _configure(application, True, every_effect.permits_tool)

    assert "hmc_run_command" not in _names(application)


def test_inspection_tracks_the_arbitrary_command_toggle():
    """R12, R13: the report follows the registry across a post-composition change."""
    granted = _policy(GRANT_RUN_COMMAND)
    application = create_mcp(granted)
    _configure(application, True, granted.permits_tool)

    result = _inspect(application)
    reported = [tool["name"] for tool in result["tools"]]

    assert reported == sorted(
        tool.name for tool in asyncio.run(application.list_tools())
    )
    assert "hmc_run_command" in reported
    assert "arbitrary-command" in result["effects"]
    assert result["ceiling_enforced"] is True


def test_a_registry_that_drifted_past_its_ceiling_demotes_only_the_tool_dimension():
    """R14, R16 as amended by ADR 0047: drift is a fact about one dimension.

    Before that amendment both tuples derived from `ceiling_enforced`, so this
    state reported `enforced_dimensions == []` *and*
    `declared_only_dimensions == []`: all three dimensions vanished from a report
    that went on enumerating connections and targets in `declared_grants`. The
    live denial below is the evidence that the silence was wrong — the connection
    dimension is enforcing, under the very policy the report names.
    """
    from fastmcp.exceptions import ToolError

    policy = _policy(READ_ONLY_GRANT)
    application = create_mcp(policy)
    _configure(application, True)  # no predicate: the fail-open call shape

    result = _inspect(application)

    assert "hmc_run_command" in [tool["name"] for tool in result["tools"]]
    assert result["policy_name"] == "test"
    assert result["ceiling_enforced"] is False
    assert result["enforced_dimensions"] == ["connections", "targets"]
    assert result["declared_only_dimensions"] == ["tools"]

    async def _ungranted_connection():
        from fastmcp import Client

        async with Client(application) as client:
            return await client.call_tool("hmc_list_systems", {"profile": "prod"})

    with pytest.raises(ToolError, match="not permitted on connection 'prod'"):
        asyncio.run(_ungranted_connection())


def test_the_two_dimension_tuples_partition_every_dimension_under_a_policy():
    """ADR 0047: a selected policy never leaves a dimension out of both tuples.

    The property the old encoding broke. Asserted over the enforcing composition
    and the drifted one together, since the bug was visible only in the second.
    """
    from hmcpctl.server_tools.permissions import DIMENSIONS

    enforcing = create_mcp(_policy(READ_ONLY_GRANT))
    drifted = create_mcp(_policy(READ_ONLY_GRANT))
    _configure(drifted, True)

    for application in (enforcing, drifted):
        result = _inspect(application)
        enforced = result["enforced_dimensions"]
        declared_only = result["declared_only_dimensions"]

        assert set(enforced).isdisjoint(declared_only)
        assert sorted([*enforced, *declared_only]) == sorted(DIMENSIONS)


def test_an_unwrapped_connection_bearing_tool_withholds_the_dispatch_claim():
    """ADR 0047: closes the unsafe direction ADR 0038 left open.

    A registry inside its ceiling whose connection-bearing tool never went
    through `authorized` used to report all three dimensions enforced, because
    `ceiling_enforced` re-checks the tool dimension only. The claim now rests on
    the registered callable, which is the only thing that can carry the check.
    """
    from hmcpctl.server_tools.permissions import build_effective_permissions

    def hmc_list_systems(profile: str | None = None) -> str:
        return "ok"

    result = build_effective_permissions(
        {"hmc_list_systems": hmc_list_systems}, _legacy(), TOOL_SECURITY, ()
    )

    assert result.ceiling_enforced is True
    assert result.enforced_dimensions == ("tools",)
    assert result.declared_only_dimensions == ("connections", "targets")


def test_a_name_outside_the_index_withholds_every_enforcement_claim():
    """ADR 0047: nothing establishes an unindexed name is bounded or guarded.

    The same fail-closed default `_permission` applies to `exhaustive_targets`.
    """
    from hmcpctl.server_tools.permissions import build_effective_permissions

    def hmc_not_in_the_index() -> str:
        return "ok"

    result = build_effective_permissions(
        {"hmc_not_in_the_index": hmc_not_in_the_index}, _legacy(), TOOL_SECURITY, ()
    )

    assert result.ceiling_enforced is False
    assert result.enforced_dimensions == ()
    assert result.declared_only_dimensions == ("tools", "connections", "targets")


def test_an_unwrapped_tool_costs_only_the_target_label():
    """ADR 0047 as #297 leaves it: an unwrapped tool escapes the target check.

    A registered callable that is not `authorized`'s wrapper has its selectors
    checked by nothing — including the unreadable-value denial `targets_permitted`
    applies even under `all-targets`. The connection dimension is untouched here
    because the fixture's tool routes no connection, so that dimension has nothing
    to say about it, which is what isolates the target label. `create_mcp` cannot
    produce this registry any more; a direct caller of `build_effective_permissions` still can, and
    the label has to stay honest for one.
    """
    from dataclasses import replace

    from hmcpctl.server_tools.permissions import build_effective_permissions

    def hmc_list_lpars(profile: str | None = None) -> str:
        return "ok"

    security = TOOL_SECURITY["hmc_list_lpars"]
    assert security.targets, "the fixture needs a tool that declares selectors"
    index = {
        **TOOL_SECURITY,
        "hmc_list_lpars": replace(security, connection_argument=None),
    }

    result = build_effective_permissions(
        {"hmc_list_lpars": hmc_list_lpars}, _legacy(), index, ()
    )

    assert result.ceiling_enforced is True
    assert result.enforced_dimensions == ("tools", "connections")
    assert result.declared_only_dimensions == ("targets",)


TABLE_GRANT = [
    {"effects": ["read"], "connections": ["<default>"], "targets": {"lpar": ["db-01"]}}
]


@pytest.mark.parametrize(
    "tool", ["hmc_effective_permissions", "hmc_list_configured_hosts"]
)
def test_a_table_grant_denies_a_tool_it_cannot_bound_without_a_connection(tool):
    """#297: the two spellings of "a table cannot bound this" decide alike.

    `hmc_effective_permissions` and `hmc_list_configured_hosts` declare no
    connection argument. ADR 0039 keyed the dispatch wrapper on that argument, so
    they registered unwrapped and no target check ran for them, and this test
    asserted the resulting permit — a table grant bounded to one LPAR disclosed
    the whole access policy and every configured profile's host, user, port and
    TLS setting. `authorized` wraps every tool now, so `targets_permitted`'s
    existing refusal of a non-exhaustive tool reaches them and the table denies,
    naming `all-targets` as the remedy.

    Note what the denial costs, and why the pair below is not optional: under a
    table-only policy the inspection tool denies itself, so an operator who wants
    it must write a second grant. That is ADR 0039's stated shape for every
    selector-less tool, now including these two.
    """
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    application = create_mcp(_policy(TABLE_GRANT))

    async def _connectionless_call():
        async with Client(application) as client:
            return await client.call_tool(tool, {})

    with pytest.raises(ToolError) as error:
        asyncio.run(_connectionless_call())
    assert "no targets table can bound every resource it acts on" in str(error.value)
    assert 'targets = "all-targets"' in str(error.value)


def test_a_table_grant_registry_reports_targets_enforced():
    """#297: nothing escapes the target check now, so the label is earned.

    ADR 0047 withheld this label for exactly the registry below, because the two
    connection-less tools registered unwrapped and skipped the check. `build_effective_permissions`
    is called directly rather than through the tool: under a table-only policy the
    inspection tool denies itself, which is the denial the test above pins.
    """
    from hmcpctl.server_tools.permissions import build_effective_permissions

    policy = _policy(TABLE_GRANT)
    application = create_mcp(policy)
    handlers = {
        tool.name: getattr(tool, "fn", None)
        for tool in asyncio.run(application.local_provider.list_tools())
    }
    assert "hmc_effective_permissions" in handlers

    result = build_effective_permissions(handlers, policy, TOOL_SECURITY, ())

    assert result.ceiling_enforced is True
    assert result.enforced_dimensions == ("tools", "connections", "targets")
    assert result.declared_only_dimensions == ()


def test_an_all_targets_grant_over_the_same_registry_permits_both_tools():
    """#297: the denial above is about the table, not about the two tools.

    Same two tools, same registry shape, an `all-targets` grant instead —
    `targets_permitted` returns true for them under it, so both are reachable and
    the label is earned. Without this pair the fix could deny them unconditionally
    and leave a client with no way to inspect its own permissions (#255).
    """
    from fastmcp import Client

    application = create_mcp(_policy(READ_ONLY_GRANT))

    async def _call(tool):
        async with Client(application) as client:
            return await client.call_tool(tool, {})

    for tool in ("hmc_effective_permissions", "hmc_list_configured_hosts"):
        assert asyncio.run(_call(tool)) is not None

    result = _inspect(application)

    assert result["enforced_dimensions"] == ["tools", "connections", "targets"]
    assert result["declared_only_dimensions"] == []


CONNECTIONLESS_TOOLS = ("hmc_effective_permissions", "hmc_list_configured_hosts")

NAMED_ALL_TARGETS_GRANT = [
    {
        "tools": list(CONNECTIONLESS_TOOLS),
        "connections": ["<default>"],
        "targets": "all-targets",
    }
]


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("effects with all-targets", lambda: _policy(READ_ONLY_GRANT)),
        (
            "a ceiling naming only the two tools",
            lambda: _policy(NAMED_ALL_TARGETS_GRANT),
        ),
        ("the legacy-equivalent policy", _legacy),
        (
            "a table grant beside an all-targets grant naming them",
            lambda: _policy(TABLE_GRANT + NAMED_ALL_TARGETS_GRANT),
        ),
    ],
)
@pytest.mark.parametrize("tool", CONNECTIONLESS_TOOLS)
def test_the_connectionless_tools_stay_reachable_under_every_granting_shape(
    label, build, tool
):
    """#297's risk, pinned: the fix must deny only what a table cannot bound.

    Over-denying `hmc_effective_permissions` would leave a client with no way to
    inspect its own permissions, which ADR 0047 and #255 both treat as important.
    Each shape here reaches both tools before the fix and must still reach them
    after it; the table-plus-all-targets pair also checks that the per-grant
    conjunction is still a union *across* grants (ADR 0036).
    """
    from fastmcp import Client

    application = create_mcp(build())

    async def _call():
        async with Client(application) as client:
            return await client.call_tool(tool, {})

    assert asyncio.run(_call()) is not None, label


def test_the_authorization_witness_is_not_forged_by_functools_wraps():
    """ADR 0047: `__wrapped__` is set by any decorator; the marker is not.

    `is_authorized_wrapper` is what `build_effective_permissions` believes, so a witness an
    unrelated decorator could set would make the enforcement label meaningless.
    """
    import functools

    from hmcpctl.tool_registry import authorized, is_authorized_wrapper

    def handler(profile: str | None = None) -> str:
        return "ok"

    @functools.wraps(handler)
    def unrelated(*args, **kwargs):
        return handler(*args, **kwargs)

    security = TOOL_SECURITY["hmc_list_systems"]
    guarded = authorized("hmc_list_systems", security, handler, lambda *_args: None)
    local_only = authorized(
        "hmc_effective_permissions",
        TOOL_SECURITY["hmc_effective_permissions"],
        handler,
        lambda *_args: None,
    )

    assert is_authorized_wrapper(guarded) is True
    assert getattr(unrelated, "__wrapped__", None) is not None
    assert is_authorized_wrapper(unrelated) is False
    assert is_authorized_wrapper(handler) is False
    # A tool with no connection argument is wrapped like any other since #297.
    assert local_only is not handler
    assert is_authorized_wrapper(local_only) is True
    # A registration carrying no callable at all — what `build_effective_permissions` reads when a
    # provider hands back a `Tool` that is not a `FunctionTool`.
    assert is_authorized_wrapper(None) is False


@pytest.mark.parametrize("entry_point", ["main_stdio", "main_http"])
def test_entry_points_serve_a_freshly_composed_filtered_application(entry_point):
    """R9, R9a: main_stdio serves its own application, wired to the ceiling."""
    from unittest.mock import patch

    import hmcpctl.server as server_module

    every_effect = _policy(
        [
            {
                "effects": ["read", "mutate", "destructive"],
                "connections": ["<default>"],
                "targets": "all-targets",
            }
        ]
    )
    served = {}

    def _capture(self, **_kwargs):
        served["app"] = self
        served["names"] = {tool.name for tool in asyncio.run(self.list_tools())}

    served_apps = []

    def _capture_twice(self, **_kwargs):
        _capture(self, **_kwargs)
        served_apps.append(self)

    with patch.object(FastMCP, "run", _capture_twice):
        getattr(server_module, entry_point)(every_effect, enable_arbitrary_command=True)
        getattr(server_module, entry_point)(every_effect, enable_arbitrary_command=True)

    # R9 is object identity, not set difference: an all-three-effects grant
    # resolves to every tool but hmc_run_command, which create_mcp never
    # registers anyway, so the two name sets are deliberately equal here.
    # Two invocations, two applications. Comparing against a freshly-composed one
    # instead would pass unconditionally — `create_mcp` returns a new object every
    # call — so it could not fail even for an entry point that cached and reused one,
    # which is the property this line exists to check.
    assert len(served_apps) == 2
    assert served_apps[0] is not served_apps[1]
    assert "hmc_run_command" not in served["names"]
    assert "hmc_delete_lpar" in served["names"]


def _warnings(tool_count, policy, enable_arbitrary_command=False):
    from hmcpctl.server import _startup_warnings

    return _startup_warnings(tool_count, policy, enable_arbitrary_command)


DENY_EVERYTHING: list[dict] = []


def test_an_empty_served_surface_is_warned_and_suppresses_the_other_line():
    """R10a: keyed on the served registry, and it replaces R18's line."""
    policy = _policy(DENY_EVERYTHING, name="locked")
    assert len(policy.tools) == 0
    assert _names(create_mcp(policy)) == set()

    lines = _warnings(0, policy)

    assert len(lines) == 1
    assert "no tools" in lines[0]
    # Cause-neutral: R10a covers an empty surface however it arose, and the
    # withheld-inspection line is suppressed rather than printed beside it.
    assert "hmc_effective_permissions" not in lines[0]


def test_a_withheld_inspection_tool_is_warned():
    """R18: named, with the policy that withheld it."""
    policy = _policy(
        [
            {
                "tools": ["hmc_list_systems"],
                "connections": ["lab"],
                "targets": "all-targets",
            }
        ]
    )

    lines = _warnings(1, policy)

    assert any("hmc_effective_permissions" in line and "test" in line for line in lines)


def test_an_authored_but_unselected_policy_file_is_warned():
    """R19, retired by ADR 0041: a file can no longer be authored-but-unselected.

    The warning fired when `access-policy.toml` existed and no policy was chosen. A
    server that starts has chosen one, so the condition is unreachable and both the
    line and `_unselected_policy_file` are gone.
    """
    import hmcpctl.server as server_app

    assert not hasattr(server_app, "_unselected_policy_file")
    assert not any("access-policy.toml" in line for line in _warnings(129, _legacy()))


def test_an_unresolvable_policy_path_never_fails_the_start():
    """R19, moved by ADR 0041: the guard is still needed, one layer up.

    `Path.home()` raises under a uid with no passwd entry and no HOME. That used to
    threaten a *warning* rendered during startup; now it threatens the *refusal*
    rendered by `serve`, which resolves the same path to name it. The successor
    assertion is
    tests/app/test_fail_closed_startup.py::test_an_unresolvable_config_home_refuses_without_a_traceback;
    what remains here is that the warning function no longer touches a path at all.
    """
    import inspect as inspect_module

    import hmcpctl.server as server_app

    source = inspect_module.getsource(server_app._startup_warnings)

    assert "resolve_access_policy_path" not in source


def test_a_withheld_escape_hatch_is_warned_only_when_requested():
    """R19a: the explicit request answered with silence gets a line."""
    policy = _policy(READ_ONLY_GRANT)

    requested = _warnings(90, policy, enable_arbitrary_command=True)
    assert any("hmc_run_command" in line for line in requested)

    assert not any("hmc_run_command" in line for line in _warnings(90, policy))


POLICY_FILE = """
[[policies.lab.grants]]
effects = ["read"]
connections = ["<default>"]
targets = "all-targets"
"""


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["serve"], "main_stdio"),
        (["serve", "--http"], "main_http"),
    ],
)
def test_serve_forwards_the_compiled_policy_to_the_entry_point(
    argv, target, tmp_path, monkeypatch
):
    """R7: the selected policy reaches the server, not just the loader."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    import hmcpctl.authorization.access_policy as access_policy_module
    from hmcpctl.authorization.access_policy import AccessPolicy
    from hmcpctl.cli import app

    path = tmp_path / "access-policy.toml"
    path.write_text(POLICY_FILE, encoding="utf-8")
    monkeypatch.setattr(
        access_policy_module, "resolve_access_policy_path", lambda: path
    )

    with patch(f"hmcpctl.server.{target}") as entry_point:
        result = CliRunner().invoke(app, [*argv, "--access-policy", "lab"])

    assert result.exit_code == 0, result.output
    forwarded = entry_point.call_args.args[0]
    assert isinstance(forwarded, AccessPolicy)
    assert forwarded.name == "lab"
    assert forwarded.permits_tool("hmc_list_systems")
    assert not forwarded.permits_tool("hmc_delete_lpar")


ESCAPE_HATCH_ONLY = [
    {
        "tools": ["hmc_run_command"],
        "connections": ["<default>"],
        "targets": "all-targets",
    }
]


def test_the_serve_path_counts_after_the_toggle_and_writes_only_to_stderr(capsys):
    """R10a: ordering, suppression, and the stdout constraint, in one test.

    A ceiling of only ``hmc_run_command`` composes zero tools, so counting
    before the toggle would emit a false empty-surface line. Started with the
    flag, the served surface is exactly the escape hatch.

    Since ADR 0043 the diagnostic lands on the sink's writer thread, so a test
    that reads its content waits for it. Without the drain this asserts against
    whatever happened to have been written by then, which on a loaded runner is
    nothing at all.
    """
    import hmcpctl.server as server_app

    policy = _policy(ESCAPE_HATCH_ONLY, name="hatch")
    application = server_app._serve_application(True, policy)
    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink must settle, not stall"
    )

    assert _names(application) == {"hmc_run_command"}

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no tools" not in captured.err
    assert "hmc_effective_permissions" in captured.err


def test_the_serve_path_warns_once_on_a_genuinely_empty_surface(capsys):
    """R10a: the empty-surface line suppresses the withheld-inspection line.

    Drained before reading for the reason the test above gives.
    """
    import hmcpctl.server as server_app

    policy = _policy(ESCAPE_HATCH_ONLY, name="hatch")
    application = server_app._serve_application(False, policy)
    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink must settle, not stall"
    )

    assert _names(application) == set()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no tools" in captured.err
    assert "hmc_effective_permissions" not in captured.err


class _BrokenStderr:
    """A stream whose every write raises, as a closed pipe's does."""

    def write(self, _text):
        raise BrokenPipeError("stderr is gone")

    def flush(self):
        raise BrokenPipeError("stderr is gone")


def _unusable_stderr(state, tmp_path):
    """One of the three ways stderr is unusable when the server starts."""
    if state == "broken":
        return _BrokenStderr()
    if state == "none":
        # CPython sets sys.stderr to None when fd 2 is not open at interpreter
        # start, which is what `serve 2>&-` produces.
        return None
    handle = (tmp_path / "closed").open("w", encoding="utf-8")
    handle.close()
    return handle


@pytest.mark.parametrize("state", ["broken", "none", "closed"])
def test_an_unusable_stderr_neither_fails_the_start_nor_reaches_stdout(
    state, tmp_path, monkeypatch, capsys
):
    """R10a: a diagnostic nobody asked for must not abort a start.

    Nor may it land on stdout, which carries JSON-RPC under stdio. Each state
    fails differently — BrokenPipeError is an OSError, a closed file raises
    ValueError, and an absent stream raises nothing at all — so asserting only
    "did not raise" would miss the one that leaks.

    Since ADR 0043 the stream is resolved by ``audit``'s sink rather than by
    ``server._warn``, on a writer thread, so this patches the process-wide
    ``sys.stderr`` and settles the sink before reading stdout. The three states
    are now three counted drops rather than three silent ones.
    """
    import sys

    import hmcpctl.server as server_app

    policy = _policy(ESCAPE_HATCH_ONLY, name="hatch")
    monkeypatch.setattr(sys, "stderr", _unusable_stderr(state, tmp_path))

    application = server_app._serve_application(False, policy)
    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink must settle, not stall"
    )

    assert _names(application) == set()
    assert capsys.readouterr().out == ""


def test_serve_reports_an_unloadable_policy_and_starts_nothing(tmp_path, monkeypatch):
    """R7, R8: an explicit selection that cannot be loaded exits non-zero.

    The file is now *present and malformed* rather than absent. ADR 0041 intercepts the
    absent case earlier and answers it with the migration message, because an operator
    who edited the launcher before generating the file is the likeliest arrival at this
    refusal and `cannot be read: [Errno 2]` named neither the generator nor the remedy.
    tests/app/test_fail_closed_startup.py::test_serve_with_an_absent_policy_file_exits_1
    is where that case is now pinned; this keeps the malformed-content path.
    """
    from typer.testing import CliRunner

    import hmcpctl.authorization.access_policy as access_policy_module
    from hmcpctl.cli import app

    present = tmp_path / "access-policy.toml"
    present.write_text("this is not = = valid toml\n", encoding="utf-8")
    monkeypatch.setattr(
        access_policy_module, "resolve_access_policy_path", lambda: present
    )
    result = CliRunner().invoke(app, ["serve", "--access-policy", "missing"])

    # `fail` renders through a rich Console that wraps a non-tty line at 80 columns,
    # and the line carries tmp_path, so the wrap can land inside the phrase (#1536).
    assert result.exit_code == 1
    assert "TOML parse error" in collapse_whitespace(result.output)


def test_inspection_reports_which_tools_a_targets_table_cannot_bound():
    """R19 (#223): `exhaustive_targets` is the only signal an operator gets.

    ADR 0037's ceiling is per-tool and structurally cannot see targets, so a
    policy whose only grant carries a table still registers and advertises the
    tools that grant will always deny. This field is what lets an operator find
    them without calling one and reading the denial.
    """
    result = _inspect(create_mcp(_policy(READ_ONLY_GRANT)))
    by_name = {tool["name"]: tool for tool in result["tools"]}

    # Selector-less, so a table has nothing to bind on.
    assert by_name["hmc_list_systems"]["exhaustive_targets"] is False
    assert by_name["hmc_effective_permissions"]["exhaustive_targets"] is False
    # Selectors that do name every resource the call acts on.
    assert by_name["hmc_get_lpar"]["exhaustive_targets"] is True
    assert by_name["hmc_list_lpars"]["exhaustive_targets"] is True

    reported = {
        tool["name"] for tool in result["tools"] if not tool["exhaustive_targets"]
    }
    assert reported == {
        name for name in by_name if not TOOL_SECURITY[name].exhaustive_targets
    }


def test_a_name_outside_the_index_is_reported_as_unbounded():
    """R19: the fail-closed default for an unindexed name, which nothing else pins.

    `_permission` returns `exhaustive_targets=False` for a name the authoritative
    index does not carry, on the ground that nothing establishes a table could
    bound it. Mutating that default to `True` left the whole suite green, so the
    justification had no test behind it.
    """
    from hmcpctl.server_tools.permissions import UNKNOWN, _permission

    reported = _permission("hmc_not_in_the_index", {})

    assert reported.effect == UNKNOWN
    assert reported.exhaustive_targets is False


TOOLS_ONLY_GRANT = [
    {"tools": ["hmc_list_systems"], "connections": ["lab"], "targets": "all-targets"}
]


def _initialize(application):
    """The instructions and the tool set one client session actually receives.

    Both read from the same session, because the defect ADR 0048 records is a
    disagreement between metadata and tools from one negotiated session; comparing a
    string from one composition against a registry from another could not see it.
    """
    from fastmcp import Client

    async def _go():
        async with Client(application) as client:
            tools = await client.list_tools()
            return client.instructions, {tool.name for tool in tools}

    return asyncio.run(_go())


def _indexed_names(text: str) -> set[str]:
    """Whole tool names in *text* that the authoritative index carries.

    Whole names, not substrings: `hmc_get_lpar` occurs inside
    `hmc_get_lpar_description`, so a containment test would read a correction
    naming the second as also covering the first.
    """
    import re

    return set(re.findall(r"\bhmc_[a-z0-9_]+\b", text)) & set(TOOL_SECURITY)


def _split_correction(instructions: str) -> tuple[str, str]:
    """The recommendation prose and the ceiling correction appended to it."""
    from hmcpctl._app import CEILING_HEADING

    prose, _heading, correction = instructions.partition(CEILING_HEADING)
    return prose, correction


def test_the_instructions_shipped_at_initialize_recommend_no_withheld_tool():
    """ADR 0048: the self-description agrees with the registry it ships beside.

    The reproduction from #255, as a test. Against `main` at 4a633a9 this policy
    delivered a 5314-character instructions string recommending 18 indexed tools
    while `tools/list` carried exactly one, with no correction anywhere in the
    text — and a `tools`-only grant is the shape that can also withhold
    `hmc_effective_permissions`, so the client had no second opinion to consult.
    """
    from hmcpctl._app import CEILING_HEADING

    policy = _policy(TOOLS_ONLY_GRANT)
    instructions, names = _initialize(create_mcp(policy))
    prose, correction = _split_correction(instructions)

    assert names == {"hmc_list_systems"}
    assert PERMISSIONS_TOOL_NAME not in names
    assert CEILING_HEADING in instructions

    recommended = _indexed_names(prose)
    assert len(recommended) > 1, "the prose must still recommend tools by name"
    assert recommended - names - _indexed_names(correction) == set()
    # The correction cannot recommend a tool this very policy shape withholds.
    assert PERMISSIONS_TOOL_NAME not in correction
    assert "`tools/list` is the authoritative set" in correction


def test_a_legacy_ceiling_ships_the_instructions_unqualified():
    """ADR 0048: the common case pays nothing — byte-identical to the prose.

    The legacy-equivalent policy admits all 18 names the prose recommends, so it
    is the shape that reaches this code and changes nothing. A `read`-only grant
    is deliberately not asserted here: it withholds `hmc_create_lpar` and
    `hmc_provision_lpar`, which the composite section recommends as bullets, and
    the test below pins the suffix it earns.
    """
    from hmcpctl._app import CEILING_HEADING, INSTRUCTIONS

    instructions, _names = _initialize(create_mcp(_legacy()))

    assert instructions == INSTRUCTIONS
    assert CEILING_HEADING not in instructions


def test_a_read_only_ceiling_corrects_the_write_recommendations_it_withholds():
    """ADR 0048: a partial ceiling names what it withholds, and points at the tool.

    `hmc_effective_permissions` is a read tool, so an effects grant reaches it —
    the arm where the extra sentence is earned, opposite the tools-only one above.
    """
    from hmcpctl._app import CEILING_HEADING

    instructions, names = _initialize(create_mcp(_policy(READ_ONLY_GRANT)))
    _prose, correction = _split_correction(instructions)
    named = _indexed_names(correction)

    assert CEILING_HEADING in instructions
    assert PERMISSIONS_TOOL_NAME in names
    assert f"Call {PERMISSIONS_TOOL_NAME}" in correction
    for withheld in ("hmc_create_lpar", "hmc_provision_lpar", "hmc_migrate_lpar"):
        assert withheld not in names
        assert withheld in named
    for granted in ("hmc_lpar_summary", "hmc_capacity_report", "hmc_fleet_health"):
        assert granted in names
        assert granted not in named


POWER_OFF_ONLY = [
    {
        "tools": ["hmc_power_off_lpar"],
        "connections": ["<default>"],
        "targets": "all-targets",
    }
]


def test_a_power_off_grant_does_not_serve_dump_restart():
    """ADR 0188: granting the stop no longer grants the crash; naming both does."""
    stop_only = _names(create_mcp(_policy(POWER_OFF_ONLY)))
    both = _names(
        create_mcp(
            _policy(
                [
                    {
                        **POWER_OFF_ONLY[0],
                        "tools": ["hmc_power_off_lpar", "hmc_dump_restart_lpar"],
                    }
                ]
            )
        )
    )

    assert "hmc_power_off_lpar" in stop_only
    assert "hmc_dump_restart_lpar" not in stop_only
    assert {"hmc_power_off_lpar", "hmc_dump_restart_lpar"} <= both


def test_served_power_off_admits_no_dump_restart():
    """ADR 0188: the served schema cannot express the crash on the stop tool."""
    application = create_mcp(_policy(POWER_OFF_ONLY))
    (tool,) = [
        tool
        for tool in asyncio.run(application.list_tools())
        if tool.name == "hmc_power_off_lpar"
    ]
    properties = tool.parameters["properties"]

    assert properties["operation"]["enum"] == ["shutdown", "osshutdown"]
    assert "allow_dump_restart" not in properties
