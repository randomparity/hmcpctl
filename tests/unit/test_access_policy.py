"""Unit tests for server access-policy loading, validation, and compilation."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tomllib

import pytest

from hmcpctl.authorization.access_policy import (
    ACCESS_POLICY_FILENAME,
    ALL_TARGETS,
    GRANT_EFFECTS,
    AccessPolicy,
    AccessPolicyError,
    _parse_document,
    compile_access_policy,
    load_access_policy,
    resolve_access_policy_path,
    unboundable_effect_tools,
)
from hmcpctl.server import TOOL_SECURITY
from hmcpctl.tool_registry import TargetSelector, ToolSecurity


def _document(**grant: object) -> dict[str, object]:
    """A one-policy document whose single grant is `grant`."""
    return {"policies": {"lab": {"grants": [grant]}}}


VALID_GRANT: dict[str, object] = {
    "effects": ["read"],
    "connections": ["lab"],
    "targets": "all-targets",
}


def _compile(document: dict[str, object], name: str = "lab") -> AccessPolicy:
    return compile_access_policy(document, name, TOOL_SECURITY, "access-policy.toml")


def test_parse_document_accepts_a_minimal_policy() -> None:
    parsed = _parse_document(_document(**VALID_GRANT), "access-policy.toml")

    assert set(parsed.policies) == {"lab"}
    grant = parsed.policies["lab"].grants[0]
    assert grant.effects == ("read",)
    assert grant.tools == ()
    assert grant.connections == ("lab",)
    assert grant.targets == "all-targets"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            {"policies": {"lab": {"grants": [VALID_GRANT]}}, "version": 1},
            "unknown key 'version'",
            id="unknown-top-level-key",
        ),
        pytest.param(
            {"policies": {"lab": {"grants": [VALID_GRANT], "note": "x"}}},
            "unknown key 'note'",
            id="unknown-policy-key",
        ),
        pytest.param(
            _document(**VALID_GRANT, targt="all-targets"),
            "unknown key 'targt'",
            id="unknown-grant-key",
        ),
        pytest.param(
            {"policies": {"lab": {}}},
            "missing required key 'grants'",
            id="grants-absent",
        ),
        pytest.param(
            {"policies": {}},
            "policies must define at least one policy",
            id="empty-policies",
        ),
        pytest.param(
            {"policies": {" ": {"grants": []}}},
            "is empty or padded with whitespace",
            id="blank-policy-name",
        ),
        pytest.param(
            _document(effects=["read"], targets="all-targets"),
            "missing required key 'connections'",
            id="connections-missing",
        ),
        pytest.param(
            _document(effects=["read"], connections=[], targets="all-targets"),
            "connections must name at least one connection",
            id="connections-empty",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab "], targets="all-targets"),
            "padded with whitespace",
            id="connection-padded",
        ),
        pytest.param(
            _document(
                effects=["read"], connections=["lab", "lab"], targets="all-targets"
            ),
            "connections contains a duplicate entry",
            id="connections-duplicate",
        ),
        pytest.param(
            _document(
                effects=["arbitrary-command"],
                connections=["lab"],
                targets="all-targets",
            ),
            "name 'hmc_run_command' in tools instead",
            id="arbitrary-command-effect",
        ),
        pytest.param(
            _document(effects=["write"], connections=["lab"], targets="all-targets"),
            "unknown effect 'write'",
            id="unknown-effect",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets=["all-targets"]),
            "must be the string",
            id="targets-bare-list",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets="all_targets"),
            "got 'all_targets'",
            id="targets-misspelled-sentinel",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={}),
            "targets table must not be empty",
            id="targets-empty-table",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={"none": ["x"]}),
            "unknown target kind 'none'",
            id="targets-kind-none",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={"lpar": []}),
            "names no selector",
            id="targets-kind-empty",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={"lpar": [""]}),
            "is empty or padded with whitespace",
            id="selector-empty",
        ),
        pytest.param(
            _document(
                tools=["hmc_get_job", "hmc_get_job"],
                connections=["lab"],
                targets="all-targets",
            ),
            "tools contains a duplicate entry",
            id="tools-duplicate",
        ),
        pytest.param(
            _document(connections=["lab"], targets="all-targets"),
            "names no tool",
            id="grant-names-no-tool",
        ),
        pytest.param(
            _document(
                effects=["read"], connections=["lab"], targets={"lpar": ["a", "a"]}
            ),
            "targets.lpar contains a duplicate entry",
            id="selector-duplicate",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={"lpar": "db-01"}),
            "must be an array of selector strings",
            id="selector-not-an-array",
        ),
        pytest.param(
            _document(effects=["read"], connections=["lab"], targets={"lpar": [1]}),
            "must contain only selector strings",
            id="selector-not-a-string",
        ),
        pytest.param(
            _document(effects=["read"], connections="lab", targets="all-targets"),
            "'connections': Input should be a valid tuple",
            id="connections-not-an-array",
        ),
        pytest.param(
            _document(effects="read", connections=["lab"], targets="all-targets"),
            "'effects': Input should be a valid tuple",
            id="effects-not-an-array",
        ),
        pytest.param(
            {
                "policies": {
                    "lab": {"grants": [VALID_GRANT]},
                    "unselected": {
                        "grants": [dict(VALID_GRANT, targets={"none": ["x"]})]
                    },
                }
            },
            "unknown target kind 'none'",
            id="targets-kind-none-in-an-unselected-policy",
        ),
    ],
)
def test_shape_tier_rejects(document: dict[str, object], expected: str) -> None:
    """Driven through the public entry point, not the private shape tier.

    The shape tier is completion criterion 3, and the specification words its
    requirement as "fails the load" — so a rejection that is only ever asserted
    against ``_parse_document`` leaves the public path's behaviour untested, and
    would stay green if document validation stopped running before selection.
    """
    with pytest.raises(AccessPolicyError) as raised:
        _compile(document)

    assert expected in str(raised.value)
    assert str(raised.value).startswith("access-policy.toml")


def test_shape_tier_binds_every_policy_not_just_one() -> None:
    document = {
        "policies": {
            "selected": {"grants": [VALID_GRANT]},
            "unselected": {"grants": [dict(VALID_GRANT, targt="x")]},
        }
    }

    with pytest.raises(AccessPolicyError) as raised:
        _compile(document, "selected")

    assert "policy 'unselected'" in str(raised.value)
    assert "grant 0" in str(raised.value)


def test_document_validation_precedes_policy_selection() -> None:
    """A malformed sibling beats a not-found error; pin the order.

    Selecting first would give the clearer 'policy not found' message at the cost
    of loading a document with a malformed sibling policy.
    """
    document = {"policies": {"lab": {"grants": [dict(VALID_GRANT, targt="x")]}}}

    with pytest.raises(AccessPolicyError) as raised:
        compile_access_policy(document, "absent", TOOL_SECURITY, "access-policy.toml")

    assert "unknown key 'targt'" in str(raised.value)
    assert "not found" not in str(raised.value)


def test_read_only_policy_ceiling_is_exactly_the_read_tools() -> None:
    policy = _compile(_document(**VALID_GRANT))

    expected = {name for name, sec in TOOL_SECURITY.items() if sec.effect == "read"}
    assert policy.tools == expected
    assert policy.name == "lab"
    for name, security in TOOL_SECURITY.items():
        assert policy.permits_tool(name) is (security.effect == "read")


def test_effect_class_plus_named_tool_unions_the_ceiling() -> None:
    document = {
        "policies": {
            "lab": {
                "grants": [
                    VALID_GRANT,
                    {
                        "tools": ["hmc_create_lpar"],
                        "connections": ["lab"],
                        "targets": "all-targets",
                    },
                ]
            }
        }
    }

    policy = _compile(document)

    reads = {name for name, sec in TOOL_SECURITY.items() if sec.effect == "read"}
    assert policy.tools == reads | {"hmc_create_lpar"}
    assert policy.permits_tool("hmc_delete_lpar") is False
    assert policy.grants_for("hmc_create_lpar") == (policy.grants[1],)


def test_arbitrary_command_needs_its_own_name() -> None:
    broad = _compile(
        _document(
            effects=["read", "mutate", "destructive"],
            connections=["lab"],
            targets="all-targets",
        )
    )
    assert broad.permits_tool("hmc_run_command") is False
    assert broad.tools == {
        name for name, sec in TOOL_SECURITY.items() if sec.effect != "arbitrary-command"
    }

    named = _compile(
        _document(tools=["hmc_run_command"], connections=["lab"], targets="all-targets")
    )
    assert named.permits_tool("hmc_run_command") is True


def test_default_connection_token_compiles_to_none() -> None:
    policy = _compile(
        _document(
            effects=["read"], connections=["<default>", "lab"], targets="all-targets"
        )
    )

    assert policy.grants[0].connections == frozenset({None, "lab"})


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(AccessPolicyError, match="unknown tool 'hmc_create_lpars'"):
        _compile(
            _document(
                tools=["hmc_create_lpars"], connections=["lab"], targets="all-targets"
            )
        )


def test_unknown_tool_error_names_the_regeneration_remedy() -> None:
    """A tool a later release retired or renamed must not fail with only the bare
    name. The message must lead with the direct fix (remove or rename the stale
    entry, no generation required) and frame the generator as scoped discovery
    only, carrying the same "widest policy" caveat cli_commands/app.py's sibling startup
    refusal already gives the generator — never bare, which would read as "copy
    this document in". The bare command would collide besides: the file already
    exists, since it was just read and compiled, and the generator never
    overwrites (ADR 0041), so the scratch path is a concrete example, not a
    placeholder that invites a same-path retry.
    """
    with pytest.raises(AccessPolicyError) as excinfo:
        _compile(
            _document(
                tools=["hmc_create_lpars"], connections=["lab"], targets="all-targets"
            )
        )
    message = str(excinfo.value)
    assert "hmc_create_lpars" in message
    assert "remove this entry or replace it with the tool's current name" in message
    assert "init-access-policy --output /tmp/access-policy.new" in message
    assert "widest policy" in message


def test_targets_kind_no_granted_tool_declares_is_rejected() -> None:
    with pytest.raises(AccessPolicyError, match="could never match"):
        _compile(
            _document(
                tools=["hmc_list_systems"],
                connections=["lab"],
                targets={"managed_system": ["S1"]},
            )
        )


def test_inert_console_constraint_on_the_escape_hatch_is_rejected() -> None:
    with pytest.raises(AccessPolicyError, match="could never match"):
        _compile(
            _document(
                tools=["hmc_run_command"],
                connections=["lab"],
                targets={"console": ["c1"]},
            )
        )


def test_coverage_rule_binds_explicitly_named_tools_only() -> None:
    """The optional-selector coverage rule binds a grant's named tools only.

    `hmc_delete_lpar` is `destructive` and requires 'lpar' beside 'managed_system'
    (ADR 0039 supersedes A7). Naming it directly against a table missing 'lpar' is
    refused for that reason. An index change to the `destructive` effect class
    must not turn an unedited file's coverage math into a refusal `hmc_delete_lpar`
    never earned, so reaching it only through `effects` must not inherit the
    requirement: the same table under the effect class validates, and permits
    `hmc_delete_lpar`.

    `destructive` also resolves to `hmc_backup_lpar_profiles`, `exhaustive_targets`
    ``False``. That does not refuse the grant (#279: a *mixed* resolved set is
    diagnosed at startup, not refused at load — see
    ``test_a_mixed_effect_grant_loads_and_warns_at_startup`` below), so
    `hmc_delete_lpar`'s exemption from the coverage rule is visible here too.
    """
    with pytest.raises(AccessPolicyError) as raised:
        _compile(
            _document(
                tools=["hmc_delete_lpar"],
                connections=["lab"],
                targets={"managed_system": ["S1"]},
            )
        )
    assert "hmc_delete_lpar" in str(raised.value)
    assert "'lpar'" in str(raised.value)

    policy = _compile(
        _document(
            effects=["destructive"],
            connections=["lab"],
            targets={"managed_system": ["S1"]},
        )
    )
    assert policy.permits_tool("hmc_delete_lpar") is True


def test_optional_selectors_must_be_covered_too() -> None:
    """ADR 0039 supersedes ADR 0036 acceptance criterion A7.

    A7 said an optional selector needs no coverage, as a placeholder for the
    decision ADR 0036 deferred to #223. That decision is that an omitted optional
    selector *denies* under a table — so this grant would authorize nothing at
    all, and a dead grant in a security artifact is the authoring error ADR 0036
    invented the coverage rule to catch.

    `hmc_power_off_lpar` is the live instance: `destructive`, and one of the
    tools whose `managed_system` selector is optional — which is exactly the
    LPAR-name-collision case ADR 0039 had to close.
    """
    with pytest.raises(AccessPolicyError) as raised:
        _compile(
            _document(
                tools=["hmc_power_off_lpar"],
                connections=["lab"],
                targets={"lpar": ["db-01"]},
            )
        )

    assert "'hmc_power_off_lpar'" in str(raised.value)
    assert "'managed_system'" in str(raised.value)

    # Covering it loads, and is the remedy the message names.
    policy = _compile(
        _document(
            tools=["hmc_power_off_lpar"],
            connections=["lab"],
            targets={"lpar": ["db-01"], "managed_system": ["S1"]},
        )
    )
    assert policy.permits_tool("hmc_power_off_lpar") is True


@pytest.mark.parametrize(
    "tool", ["hmc_provision_lpar", "hmc_create_lpar", "hmc_create_vios"]
)
def test_a_composite_a_table_cannot_bound_is_refused_at_load(tool) -> None:
    """Same rule, reached through `exhaustive_targets=False` rather than through
    an empty selector tuple — the composite case, where the grant *looks* covered.
    """
    with pytest.raises(AccessPolicyError) as raised:
        _compile(
            _document(
                tools=[tool],
                connections=["lab"],
                targets={"managed_system": ["S1"]},
            )
        )

    assert repr(tool) in str(raised.value)
    assert "all-targets" in str(raised.value)


def test_a_mixed_effect_grant_loads_and_warns_at_startup() -> None:
    """#279: a mixed effect-resolved set loads; its dead subset is diagnosed later.

    `mutate` resolves `hmc_provision_lpar` and siblings that are
    `exhaustive_targets=False`, alongside the tools this table binds correctly.
    Before the fix, the exhaustiveness check ignored effect-resolved tools
    entirely, so this grant loaded with no diagnostic at all -- every
    non-exhaustive one is silently dead at dispatch under `target-unboundable`
    (target_scope.py). Refusing the whole grant over them, mirroring the
    named-tool rule, would instead discard the working tools to diagnose the
    dead ones -- so the fix is a load-clean warning naming exactly the dead
    ones, not a refusal.
    """
    policy = _compile(
        _document(
            effects=["mutate"],
            connections=["lab"],
            targets={"managed_system": ["S1"]},
        )
    )
    assert policy.permits_tool("hmc_provision_lpar") is True
    assert policy.permits_tool("hmc_add_vfc_adapter") is True

    warnings = unboundable_effect_tools(policy, TOOL_SECURITY)
    assert len(warnings) == 1
    message = warnings[0]
    for offender in (
        "hmc_add_vfc_adapter",
        "hmc_add_vscsi_adapter",
        "hmc_provision_lpar",
    ):
        assert repr(offender) in message
    # #1086: its selectors bound it, so a table reaches it and it is not dead.
    assert "'hmc_attach_disk_to_lpar'" not in message
    assert "all-targets" in message


def test_the_startup_warning_names_the_connectionless_tools_a_table_kills() -> None:
    """#297: they are now part of the dead subset, so they must be named in it.

    `unboundable_effect_tools` filtered on the connection argument, because a
    tool declaring none was reachable under a table and so was not dead. It is
    dead now, and this is the one place an operator is told before the first
    denial — which matters most for `hmc_effective_permissions`, since a
    table-only policy leaves them no way to ask the server itself.
    """
    policy = _compile(
        _document(effects=["read"], connections=["lab"], targets={"lpar": ["db-01"]})
    )

    warnings = unboundable_effect_tools(policy, TOOL_SECURITY)

    assert len(warnings) == 1
    for offender in ("hmc_effective_permissions", "hmc_list_configured_hosts"):
        assert repr(offender) in warnings[0]


def test_a_wholly_dead_effect_grant_is_refused_at_load() -> None:
    """The refusal `unboundable_effect_tools` exists beside: nothing works at all.

    No live effect class is wholly `exhaustive_targets=False` today -- every one
    of `read`/`mutate`/`destructive` carries a bindable majority (confirmed
    against the full registry: 37/18, 41/5, 25/3 exhaustive-to-not) -- so this
    branch has no real-registry trigger to reproduce against. A synthetic
    one-tool registry exercises it directly: the shape ADR 0039 already refused
    for a *named* tool (`test_a_composite_a_table_cannot_bound_is_refused_at_load`),
    now refused the same way when a grant reaches it only by naming its effect
    class and nothing else is reachable to redeem the grant.
    """
    fake_security = {
        "widget_mutate": ToolSecurity(
            effect="mutate",
            operation="widget.mutate",
            target_kind="managed_system",
            targets=(
                TargetSelector(kind="managed_system", argument="system", required=True),
            ),
            exhaustive_targets=False,
        ),
    }

    with pytest.raises(AccessPolicyError) as raised:
        compile_access_policy(
            _document(
                effects=["mutate"],
                connections=["lab"],
                targets={"managed_system": ["S1"]},
            ),
            "lab",
            fake_security,
            "access-policy.toml",
        )

    assert "'widget_mutate'" in str(raised.value)
    assert "all-targets" in str(raised.value)


@pytest.mark.parametrize(
    "tool", ["hmc_effective_permissions", "hmc_list_configured_hosts"]
)
def test_a_connectionless_tool_named_beside_a_table_is_refused(tool: str) -> None:
    """#297: the exemption ADR 0039 wrote for these two is gone, because it was
    an exemption from a rule about dead grants and the grant is dead now.

    ADR 0039 exempted them on the grounds that `tool_registry.authorized` left
    them unwrapped, so the grant was not dead — it worked, bounded by the ceiling
    alone. Every tool is wrapped now and a `targets` table denies one it cannot
    bound, so naming one here can never authorize it, and that is exactly the
    authoring error this rule refuses the load over for every other tool.
    """
    with pytest.raises(AccessPolicyError, match="no target selector that a targets"):
        _compile(
            _document(
                tools=["hmc_delete_lpar", tool],
                connections=["lab"],
                targets={"lpar": ["db-01"], "managed_system": ["S1"]},
            )
        )


def test_a_connectionless_tool_named_under_all_targets_still_loads() -> None:
    """#297: the refusal above is about the table, not about the two tools."""
    policy = _compile(
        _document(
            tools=[
                "hmc_delete_lpar",
                "hmc_effective_permissions",
                "hmc_list_configured_hosts",
            ],
            connections=["lab"],
            targets="all-targets",
        )
    )

    assert policy.permits_tool("hmc_effective_permissions") is True
    assert policy.permits_tool("hmc_list_configured_hosts") is True
    for name in ("hmc_effective_permissions", "hmc_list_configured_hosts"):
        assert TOOL_SECURITY[name].connection_argument is None


def test_identical_grants_are_rejected() -> None:
    with pytest.raises(AccessPolicyError, match="grants 0 and 1 are identical"):
        _compile({"policies": {"lab": {"grants": [VALID_GRANT, dict(VALID_GRANT)]}}})


def test_grants_differing_only_in_text_are_rejected() -> None:
    document = {
        "policies": {
            "lab": {
                "grants": [
                    VALID_GRANT,
                    dict(VALID_GRANT, tools=["hmc_list_systems"]),
                ]
            }
        }
    }

    with pytest.raises(AccessPolicyError, match="identical after compilation"):
        _compile(document)


def test_empty_grants_list_permits_nothing() -> None:
    policy = _compile({"policies": {"lab": {"grants": []}}})

    assert policy.tools == frozenset()
    assert policy.grants == ()
    assert policy.permits_tool("hmc_list_systems") is False


def test_missing_policy_names_the_available_ones() -> None:
    document = {
        "policies": {
            "lab": {"grants": [VALID_GRANT]},
            "read-only": {"grants": [VALID_GRANT]},
        }
    }

    with pytest.raises(AccessPolicyError) as raised:
        compile_access_policy(document, "typo", TOOL_SECURITY, "access-policy.toml")

    assert "policy 'typo' not found" in str(raised.value)
    assert "'lab', 'read-only'" in str(raised.value)


def test_index_dependent_rules_bind_only_the_selected_policy() -> None:
    document = {
        "policies": {
            "lab": {"grants": [VALID_GRANT]},
            "other": {
                "grants": [
                    dict(VALID_GRANT, effects=[], tools=["hmc_no_such_tool"]),
                ]
            },
        }
    }

    policy = _compile(document)

    assert policy.name == "lab"


def test_grants_for_returns_whole_grants_not_merged_dimensions() -> None:
    document = {
        "policies": {
            "lab": {
                "grants": [
                    {
                        "effects": ["read"],
                        "connections": ["prod"],
                        "targets": "all-targets",
                    },
                    {
                        "tools": ["hmc_delete_lpar"],
                        "connections": ["lab"],
                        "targets": {
                            "managed_system": ["S1"],
                            "lpar": ["scratch-01"],
                        },
                    },
                ]
            }
        }
    }

    policy = _compile(document)
    found = policy.grants_for("hmc_delete_lpar")

    assert len(found) == 1
    assert found[0].connections == frozenset({"lab"})
    assert found[0].targets is not ALL_TARGETS
    assert all("prod" not in grant.connections for grant in found)


def test_compiled_grant_retains_authored_effects() -> None:
    """The authored `effects` tuple survives compilation alongside the resolution.

    #251: inspection renders each grant's resolved `tools`, which unions the
    effect-class expansion with named tools. Without retaining what was authored,
    a grant written as `effects = ["read"]` is indistinguishable from one written
    as a ninety-name list.
    """
    document = {
        "policies": {
            "lab": {
                "grants": [
                    dict(VALID_GRANT, effects=["mutate", "read"]),
                    {
                        "tools": ["hmc_delete_lpar"],
                        "connections": ["lab"],
                        "targets": {
                            "managed_system": ["S1"],
                            "lpar": ["scratch-01"],
                        },
                    },
                ]
            }
        }
    }

    policy = _compile(document)

    assert policy.grants[0].effects == ("mutate", "read")
    assert policy.grants[1].effects == ()


def test_compiled_policy_is_immutable() -> None:
    policy = _compile(
        _document(
            tools=["hmc_power_off_lpar"],
            connections=["lab"],
            targets={"lpar": ["db-01"], "managed_system": ["S1"]},
        )
    )
    grant = policy.grants[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.name = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.tools = frozenset()  # type: ignore[misc]
    with pytest.raises(TypeError):
        grant.targets["lpar"] = frozenset()  # type: ignore[index]
    with pytest.raises(TypeError):
        hash(grant)
    with pytest.raises(TypeError):
        hash(_compile(_document(**VALID_GRANT)).grants[0])

    assert isinstance(policy.tools, frozenset)
    assert isinstance(grant.connections, frozenset)
    assert repr(ALL_TARGETS) == "ALL_TARGETS"


def test_compile_does_not_retain_the_caller_containers() -> None:
    """Mutate the nested lists a compiled Grant could alias, not the outer key.

    Replacing ``document["policies"]`` proves nothing: no compiled object holds a
    reference to that key. The retention risk is one and two levels down — the
    selector list inside a ``targets`` table, and the ``connections`` array.
    """
    selectors = ["db-01"]
    connections = ["lab", "prod"]
    document = _document(
        tools=["hmc_power_off_lpar"],
        connections=connections,
        targets={"lpar": selectors, "managed_system": ["S1"]},
    )

    policy = _compile(document)
    grant = policy.grants[0]

    selectors.append("prod-01")
    connections.append("staging")

    assert grant.targets["lpar"] == frozenset({"db-01"})
    assert grant.connections == frozenset({"lab", "prod"})


POLICY_FILE = """
[[policies.lab.grants]]
effects = ["read"]
connections = ["lab"]
targets = "all-targets"
"""


def test_load_round_trips_a_written_file(tmp_path) -> None:
    path = tmp_path / ACCESS_POLICY_FILENAME
    path.write_text(POLICY_FILE, encoding="utf-8")

    loaded = load_access_policy("lab", TOOL_SECURITY, path=path)
    compiled = compile_access_policy(
        tomllib.loads(POLICY_FILE), "lab", TOOL_SECURITY, str(path)
    )

    assert loaded == compiled
    assert loaded.source == str(path)


def test_module_exposes_no_mutator() -> None:
    import inspect

    from hmcpctl.authorization import access_policy

    # Filter to functions this module *defines*. `vars()` also carries what it
    # imported — `dataclass`, `field_validator`, `config_dir` are all public
    # functions — so an unfiltered check can never pass.
    functions = {
        name
        for name, value in vars(access_policy).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == access_policy.__name__
    }
    assert functions == {
        "resolve_access_policy_path",
        "compile_access_policy",
        "load_access_policy",
        "unboundable_effect_tools",
    }

    methods = {
        name
        for name, value in vars(AccessPolicy).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert methods == {"permits_tool", "grants_for"}


def test_resolve_path_sits_beside_config_toml(monkeypatch, tmp_path) -> None:
    """Pin the relationship, not the implementation.

    Asserting ``resolve_access_policy_path() == config_dir() / FILENAME`` would
    restate the function body and pass on any platform. What the spec claims is
    that the policy file sits *beside* ``config.toml`` — and ``config_dir()``
    duplicates ``resolve_config_path()``'s platform branching rather than sharing
    it, so the two can drift apart silently.
    """
    from hmcpctl.config import config_dir, resolve_config_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text("", encoding="utf-8")

    config_path = resolve_config_path()
    assert config_path is not None
    assert resolve_access_policy_path().parent == config_path.parent
    assert resolve_access_policy_path().name == ACCESS_POLICY_FILENAME


def test_load_uses_the_resolved_path_when_none_is_given(monkeypatch, tmp_path) -> None:
    target = tmp_path / ACCESS_POLICY_FILENAME
    target.write_text(POLICY_FILE, encoding="utf-8")
    monkeypatch.setattr(
        "hmcpctl.authorization.access_policy.resolve_access_policy_path", lambda: target
    )

    policy = load_access_policy("lab", TOOL_SECURITY)

    assert policy.source == str(target)


def test_absent_file_names_the_resolved_path(tmp_path) -> None:
    missing = tmp_path / ACCESS_POLICY_FILENAME

    with pytest.raises(AccessPolicyError) as raised:
        load_access_policy("lab", TOOL_SECURITY, path=missing)

    assert str(missing) in str(raised.value)
    assert "cannot be read" in str(raised.value)


def test_non_utf8_file_is_an_access_policy_error(tmp_path) -> None:
    path = tmp_path / ACCESS_POLICY_FILENAME
    path.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(AccessPolicyError, match="not valid UTF-8"):
        load_access_policy("lab", TOOL_SECURITY, path=path)


def test_toml_syntax_error_is_an_access_policy_error(tmp_path) -> None:
    path = tmp_path / ACCESS_POLICY_FILENAME
    path.write_text("[policies.lab\n", encoding="utf-8")

    with pytest.raises(AccessPolicyError, match="TOML parse error"):
        load_access_policy("lab", TOOL_SECURITY, path=path)


def test_deeply_nested_document_is_an_access_policy_error(tmp_path) -> None:
    """tomllib recurses, so a nested document exhausts the stack before parsing."""
    path = tmp_path / ACCESS_POLICY_FILENAME
    depth = 6000
    path.write_text("x = " + "[" * depth + "]" * depth, encoding="utf-8")

    with pytest.raises(AccessPolicyError, match="nesting is too deep"):
        load_access_policy("lab", TOOL_SECURITY, path=path)


def test_directory_in_place_of_the_file_is_an_access_policy_error(tmp_path) -> None:
    directory = tmp_path / ACCESS_POLICY_FILENAME
    directory.mkdir()

    with pytest.raises(AccessPolicyError, match="cannot be read"):
        load_access_policy("lab", TOOL_SECURITY, path=directory)


def test_module_does_not_import_server() -> None:
    script = (
        "import sys\n"
        "from hmcpctl.authorization.access_policy import load_access_policy\n"
        "assert load_access_policy is not None\n"
        "assert 'hmcpctl.server' not in sys.modules, sorted(sys.modules)\n"
    )

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )


def test_module_imports_only_the_declared_first_party_modules() -> None:
    import ast
    from pathlib import Path as _Path

    from hmcpctl.authorization import access_policy as module

    assert module.__file__ is not None
    tree = ast.parse(_Path(module.__file__).read_text(encoding="utf-8"))
    first_party = {
        node.module
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.startswith("hmcpctl.")
        )
    }
    third_party = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    }

    assert first_party == {"hmcpctl.config", "hmcpctl.tool_registry"}
    assert third_party & {"fastmcp", "mcp", "rich", "typer"} == set()
    assert "pydantic" in third_party


def test_grant_effects_track_the_registry_vocabulary() -> None:
    """GRANT_EFFECTS is hand-written, so pin it to the vocabulary it mirrors.

    Deriving it from ``EFFECTS`` instead would make a future maximum-risk effect
    class grantable by default, which is what epic #218 requirement 6 forbids for
    ``arbitrary-command``. A test converts silent drift into a red build without
    that risk.
    """
    from hmcpctl.tool_registry import EFFECTS

    assert GRANT_EFFECTS | {"arbitrary-command"} == EFFECTS


def test_unresolvable_default_path_is_an_access_policy_error(monkeypatch) -> None:
    def _explode() -> object:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(
        "hmcpctl.authorization.access_policy.resolve_access_policy_path", _explode
    )

    with pytest.raises(AccessPolicyError, match="cannot resolve the access-policy"):
        load_access_policy("lab", TOOL_SECURITY)


def test_unusable_path_string_is_an_access_policy_error() -> None:
    with pytest.raises(AccessPolicyError, match="cannot be read"):
        load_access_policy("lab", TOOL_SECURITY, path="a\x00b")


def test_api_surface_is_unchanged() -> None:
    from hmcpctl import api

    assert not any("access_policy" in name for name in api.__all__)
