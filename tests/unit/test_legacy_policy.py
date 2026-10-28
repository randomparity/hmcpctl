"""The legacy-equivalent policy generator: what it grants, and that it loads.

Covers docs/workflow/specs/2026-08-19-fail-closed-startup-design.md.

Spec item -> node id:
  R7   test_the_grant_names_every_ordinary_tool
  R7   test_the_document_grants_no_effect_class
  R7a  test_the_grant_omits_the_escape_hatch_unless_opted_in
  R8   test_connections_put_the_default_first_and_drop_a_colliding_key
  R8   test_connections_are_the_default_alone_without_a_config_file
  R8   test_connections_exclude_nicknames
  R8   test_an_unreadable_config_raises_config_error
  R9   test_rendering_round_trips_through_the_real_loader
  R9   test_the_header_carries_the_five_things_the_operator_needs
  R9a  test_rendering_escapes_a_hostile_profile_key
  R9a  test_rendering_leaves_c1_alone
  R10  test_compile_legacy_policy_needs_no_filesystem
  R9b  test_compiling_a_document_that_would_not_load_raises
  R9b  test_the_rendered_text_is_what_gets_compiled
  R9b  test_rendered_text_with_an_illegal_entry_is_rejected
  --   test_the_recorded_unboundable_count_matches_the_registry

The inventory guard below is per-module by construction, as the one in
``tests/unit/test_audit.py`` is: it reads this file and asserts every node id named
above still exists. R11/R11a live in ``tests/app/test_cli_commands/config.py`` and R1-R5b in
``tests/app/test_fail_closed_startup.py``, each with its own header and guard.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from hmcpctl.authorization.access_policy import (
    ALL_TARGETS_TOKEN,
    DEFAULT_CONNECTION_TOKEN,
    AccessPolicyError,
    AllTargets,
    compile_access_policy,
)
from hmcpctl.cli_commands.legacy_policy import (
    GENERATED_SOURCE,
    LEGACY_POLICY_NAME,
    compile_legacy_policy,
    compile_rendered_policy,
    legacy_connections,
    legacy_document,
    legacy_tools,
    render_legacy_policy,
)
from hmcpctl.config import ConfigError
from hmcpctl.server import TOOL_SECURITY

DEL = "\x7f"
C1_NEL = "\x85"


@pytest.fixture
def steered_config(tmp_path, monkeypatch):
    """Point ``config_dir()`` at a temporary directory, on every platform.

    ``HOME`` alone does not steer it: on Linux ``config_dir()`` reads
    ``XDG_CONFIG_HOME`` first, and on darwin it reads ``Path.home()`` with no
    environment override at all. Without all four steps a test that writes would
    create a real ``access-policy.toml`` in the developer's own config directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    from hmcpctl.config import config_dir

    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_config(directory: Path, body: str) -> None:
    (directory / "config.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# R7 — the granted tool set
# ---------------------------------------------------------------------------


def test_the_grant_names_every_ordinary_tool():
    """R7: legacy exposure, minus the escape hatch, named rather than implied."""
    tools = legacy_tools(TOOL_SECURITY)

    assert set(tools) == set(TOOL_SECURITY) - {"hmc_run_command"}
    assert list(tools) == sorted(tools)


def test_the_document_grants_no_effect_class():
    """R7: an effects grant would confer every tool a later release adds."""
    grant = legacy_document(TOOL_SECURITY, ("<default>",))["policies"][
        LEGACY_POLICY_NAME
    ]["grants"][0]

    assert "effects" not in grant
    assert grant["targets"] == ALL_TARGETS_TOKEN
    assert grant["connections"] == ["<default>"]


# ---------------------------------------------------------------------------
# R7a — the escape hatch is opt-in, and the renderer cannot emit it
# ---------------------------------------------------------------------------


def test_the_grant_omits_the_escape_hatch_unless_opted_in():
    """R7a/R14: no file the generator writes can grant hmc_run_command."""
    assert "hmc_run_command" not in legacy_tools(TOOL_SECURITY)
    assert "hmc_run_command" in legacy_tools(
        TOOL_SECURITY, include_arbitrary_command=True
    )
    # The grant, not the whole text: the header names the tool in prose, telling the
    # operator how to add it by hand, which is the point of leaving it out of the grant.
    rendered = tomllib.loads(render_legacy_policy(TOOL_SECURITY, ("<default>",)))
    granted = rendered["policies"][LEGACY_POLICY_NAME]["grants"][0]["tools"]
    assert "hmc_run_command" not in granted


# ---------------------------------------------------------------------------
# R8 — the connection list
# ---------------------------------------------------------------------------


def test_connections_put_the_default_first_and_drop_a_colliding_key(steered_config):
    """R8: the sentinel appears once, even when a profile is literally named it."""
    _write_config(
        steered_config,
        '[profiles.lab]\nhost = "a"\n'
        '[profiles.prod]\nhost = "b"\n'
        '[profiles."<default>"]\nhost = "c"\n',
    )

    assert legacy_connections() == ("<default>", "lab", "prod")


def test_connections_are_the_default_alone_without_a_config_file(steered_config):
    """R8: no config file is not an error; it is a one-connection deployment."""
    assert legacy_connections() == (DEFAULT_CONNECTION_TOKEN,)


def test_connections_exclude_nicknames(steered_config):
    """R8: ADR 0030 resolves a nickname away before ADR 0038 compares it."""
    # `nicknames` at TOP level. Written inside `[profiles.lab]` it is a key of that
    # profile, `list_profiles_and_nicknames` returns an empty nickname table, and the
    # assertion below holds for every implementation — including one that unions the
    # nicknames in, which is exactly what this test claims to forbid.
    _write_config(
        steered_config,
        '[profiles.lab]\nhost = "a"\n\n[nicknames]\nbig-iron = "lab"\n',
    )

    assert legacy_connections() == ("<default>", "lab")
    assert "big-iron" not in legacy_connections()


def test_an_unreadable_config_raises_config_error(steered_config):
    """R8: propagated, not swallowed into a policy granting less than it claims."""
    _write_config(steered_config, "this is not = = valid toml\n")

    with pytest.raises(ConfigError):
        legacy_connections()


# ---------------------------------------------------------------------------
# R9 / R9a — rendering
# ---------------------------------------------------------------------------


def test_rendering_round_trips_through_the_real_loader():
    """R9: proved through compile_access_policy, not a private constructor."""
    text = render_legacy_policy(TOOL_SECURITY, ("<default>", "lab"))

    policy = compile_access_policy(
        tomllib.loads(text), LEGACY_POLICY_NAME, TOOL_SECURITY, "test"
    )

    assert policy.name == LEGACY_POLICY_NAME
    assert policy.tools == frozenset(legacy_tools(TOOL_SECURITY))
    assert len(policy.grants) == 1
    assert isinstance(policy.grants[0].targets, AllTargets)
    assert policy.grants[0].connections == frozenset({None, "lab"})


def test_the_header_carries_the_five_things_the_operator_needs():
    """R9: the round-trip test passes identically on a header-free document.

    So without this the header can be truncated or dropped with the suite green,
    while R11a's regeneration story and R16's fresh-install guidance both rest on
    it.
    """
    comments = "\n".join(
        line
        for line in render_legacy_policy(TOOL_SECURITY, ("<default>",)).splitlines()
        if line.startswith("#")
    )

    assert "hmc_run_command" in comments
    assert "--output" in comments
    assert "migration aid" in comments.lower()


def test_rendering_escapes_a_hostile_profile_key():
    """R9a: no key can terminate its string, so none can inject a grant table."""
    hostile = ('a"b', "c\\d", "e\nf", "g" + DEL + "h", "i\tj")
    connections = (DEFAULT_CONNECTION_TOKEN, *hostile)

    text = render_legacy_policy(TOOL_SECURITY, connections)
    parsed = tomllib.loads(text)

    grant = parsed["policies"][LEGACY_POLICY_NAME]["grants"][0]
    assert tuple(grant["connections"]) == connections
    assert "[[policies" not in text.split("\n", 1)[1].replace(
        "[[policies." + LEGACY_POLICY_NAME + ".grants]]", "", 1
    )


def test_rendering_leaves_c1_alone():
    """R9a: TOML forbids U+0000-U+001F and U+007F; C1 is an ordinary character.

    Checked against this checkout's ``tomllib``: a raw U+007F in a basic string is
    rejected and a raw U+0085 parses. Escaping C1 anyway would be harmless, but
    the boundary is stated rather than approximated, so it is pinned.
    """
    text = render_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN, "a" + C1_NEL))

    assert C1_NEL in text
    assert DEL not in text
    grant = tomllib.loads(text)["policies"][LEGACY_POLICY_NAME]["grants"][0]
    assert "a" + C1_NEL in grant["connections"]


# ---------------------------------------------------------------------------
# R10 — compiling without a filesystem
# ---------------------------------------------------------------------------


def test_compile_legacy_policy_needs_no_filesystem():
    """R10: the source is a fixed label, so no message renders a path."""
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    assert policy.source == GENERATED_SOURCE
    assert "/" not in policy.source and "\\" not in policy.source
    assert policy.permits_tool("hmc_list_systems")
    assert not policy.permits_tool("hmc_run_command")
    assert compile_legacy_policy(
        TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=True
    ).permits_tool("hmc_run_command")


def test_compiling_a_document_that_would_not_load_raises():
    """R9b's precondition: an illegal connection entry fails at compile time."""
    with pytest.raises(AccessPolicyError):
        compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN, " padded"))


def test_the_rendered_text_is_what_gets_compiled():
    """R9b: the artifact is the rendering, so that is what the check must admit.

    Compiling the mapping instead would prove nothing about the escaping, which is the
    only step between an operator's profile key and a file a server has to load.
    """
    text = render_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN, 'a"b'))

    policy = compile_rendered_policy(text, TOOL_SECURITY)

    assert policy.source == GENERATED_SOURCE
    assert policy.grants[0].connections == frozenset({None, 'a"b'})


def test_rendered_text_with_an_illegal_entry_is_rejected():
    """R9b: escaping makes it parse; ADR 0036's entry rules still have to pass.

    ``[profiles." prod"]`` is legal TOML that ``load_profile`` resolves today, so this is
    a working deployment whose generation must fail before any file is created rather
    than after.
    """
    text = render_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN, " padded"))

    # It parses — the escaping did its job — and still refuses to compile.
    assert tomllib.loads(text)

    with pytest.raises(AccessPolicyError, match="padded"):
        compile_rendered_policy(text, TOOL_SECURITY)


def test_uom_user_replacement_reduces_unboundable_tool_count():
    """ADR 0076 replaces six selector-less tools with console-bound tools."""
    ordinary = {
        name: security
        for name, security in TOOL_SECURITY.items()
        if name != "hmc_run_command"
    }
    unboundable = {
        name for name, security in ordinary.items() if not security.exhaustive_targets
    }
    connection_bearing = {
        name for name in unboundable if ordinary[name].connection_argument is not None
    }
    connectionless = unboundable - connection_bearing
    all_nonexhaustive = {
        name
        for name, security in TOOL_SECURITY.items()
        if not security.exhaustive_targets
    }

    decision = (
        Path(__file__).parents[2]
        / "docs"
        / "adr"
        / "0076-use-uom-user-profile-and-remote-access.md"
    )
    assert decision.is_file()
    # #1202 removed hmc_list_recent_jobs, one connection-bearing unboundable tool;
    # #1218 added hmc_operation_status, one more; #1219 added the two
    # connectionless gateway tools, hmc_search_tools and hmc_invoke_tool; #1220
    # added hmc_inventory, #1221 hmc_plan_lpar, #1223 hmc_power_lpar and #1224
    # hmc_inspect_lpar, each one more connection-bearing.
    # #1726 adds one connection-bearing all-targets artifact deletion tool.
    # #1725 adds four connection-bearing all-targets system-profile mutations.
    # #1772 makes VIOS creation nonexhaustive: serial peers require all-targets,
    # adding exactly one connection-bearing tool (ADR 0303).
    # #1777 adds one connection-bearing all-targets statistics reset.
    assert len(unboundable) == 39
    assert len(connection_bearing) == 32
    assert len(connectionless) == 7
    assert len(all_nonexhaustive) == 40


# ---------------------------------------------------------------------------
# Inventory guard — see the module docstring
# ---------------------------------------------------------------------------


def test_every_spec_numbered_test_named_in_the_header_still_exists():
    """A header naming a test that was deleted is worse than no header.

    On #224 a slice-replacement refactor removed the only test that could observe a
    regression and left the suite green. This is the guard against that: the module
    docstring's map is checked against the functions actually defined here.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    header = source.split('"""', 2)[1]

    named = set(re.findall(r"^  \w+\s+(test_\w+)$", header, flags=re.MULTILINE))
    defined = set(re.findall(r"^def (test_\w+)", source, flags=re.MULTILINE))

    assert named, "the header maps no test; the guard would pass vacuously"
    assert named <= defined, (
        f"named in the header but not defined: {sorted(named - defined)}"
    )
