"""C04 operator boundaries, actual MCP validation and bounded private diagnostics."""

import asyncio
import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner
from unit.test_lpar_security_policy_operations import PARTITION, SYSTEM, policy_target  # noqa: F401 -- shared boundary fixture

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.documents.lpar_security_policy import (
    LparSecurityPolicy,
    SecurityPolicyResult,
    SecurityPolicyState,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp

MARKER = "PRIVATE-POLICY-MARKER"
SETTINGS = LparSecurityPolicy(secure_boot=0)
STATE = SecurityPolicyState("AIX/Linux", "not activated", SETTINGS, 2)
RESULT = SecurityPolicyResult("verified", SETTINGS, STATE, ("secure_boot",), ())


def policy_app(*names):
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": list(names),
                            "connections": ["<default>"],
                            "targets": "all-targets",
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )
    return create_mcp(policy)


@pytest.mark.asyncio
async def test_strict_typed_schema():
    names = [f"hmc_configure_{kind}_security_policy" for kind in ("lpar", "vios")]
    async with Client(policy_app(*names)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in names:
        schema = tools[name].input_schema
        settings = schema["properties"]["settings"]
        assert settings["additionalProperties"] is False
        assert set(settings["properties"]) == set(asdict(LparSecurityPolicy()))
        assert schema["required"] == [
            "system_name_or_uuid",
            name.split("_")[2] + "_name_or_uuid",
            "settings",
        ]
        assert "acknowledge_security_change" not in schema["properties"]
        assert "intended_linux_guest" not in schema["properties"]
        assert tools[name].annotations.destructive_hint is True


@pytest.mark.parametrize("kind", ["lpar", "vios"])
@pytest.mark.asyncio
async def test_confirmation_and_target_grants(monkeypatch, kind):
    from hmcpctl.server_tools.lpar import configuration
    from hmcpctl.server_tools.vios import core

    module = core if kind == "vios" else configuration
    update = AsyncMock(return_value=RESULT)
    profiles = []
    monkeypatch.setattr(module, "configure_lpar_security_policy", update)

    def run(operation, **kwargs):
        profiles.append(kwargs)
        return asyncio.run(operation("hmc"))

    monkeypatch.setattr(module, "with_client", run)
    setter = getattr(module, f"hmc_configure_{kind}_security_policy")
    # Direct adapter call proves existing connection and ownership selectors.
    result = await asyncio.to_thread(
        setter, "sys-R1", "target-R1", SETTINGS, True, "connection-R1"
    )
    assert result == RESULT
    assert profiles == [{"profile": "connection-R1"}]
    assert update.await_args.kwargs["ownership_override"] is True
    assert update.await_args.kwargs.get("vios", False) is (kind == "vios")
    security = TOOL_SECURITY[f"hmc_configure_{kind}_security_policy"]
    assert security.target_kind == kind
    assert security.effect == "destructive"
    assert any(
        t.kind == kind and t.argument == f"{kind}_name_or_uuid"
        for t in security.targets
    )
    wrong = "vios" if kind == "lpar" else "lpar"
    async with Client(policy_app(f"hmc_configure_{wrong}_security_policy")) as client:
        denied = await client.call_tool(
            f"hmc_configure_{kind}_security_policy", {}, raise_on_error=False
        )
    assert denied.is_error
    assert update.await_count == 1


@pytest.mark.parametrize("kind", ["lpar", "vios"])
@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.asyncio
async def test_oversized_sensitive_input_not_reflected(monkeypatch, kind, creation):
    from hmcpctl.server_tools.lpar import configuration, lifecycle_create
    from hmcpctl.server_tools.vios import core

    module = core if kind == "vios" else lifecycle_create if creation else configuration
    reached = []
    monkeypatch.setattr(module, "with_client", lambda *a, **kw: reached.append(True))
    name = f"hmc_create_{kind}" if creation else f"hmc_configure_{kind}_security_policy"
    argument = "security_policy" if creation else "settings"
    inputs = [
        {"secure_boot": MARKER},
        {MARKER: "value"},
        MARKER,
        [MARKER],
        {f"{MARKER}-{i}-" + "x" * 70000: MARKER for i in range(8)},
    ]
    async with Client(policy_app(name)) as client:
        for invalid in inputs:
            arguments = {"system_name_or_uuid": "sys-R1", argument: invalid}
            arguments["name" if creation else f"{kind}_name_or_uuid"] = "target-R1"
            result = await client.call_tool(name, arguments, raise_on_error=False)
            assert result.is_error
            text = str(result.content)
            assert MARKER not in text
            assert len(text) < 4000
    assert reached == []


@pytest.mark.parametrize("group", ["lpars", "vios"])
def test_cli_confirmation_and_outcomes(monkeypatch, group):
    from hmcpctl.cli_commands.lpar import config
    from hmcpctl.cli_commands.vios import core

    module = core if group == "vios" else config
    call = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(module, "configure_lpar_security_policy", call)
    monkeypatch.setattr(module, "with_client", lambda f: asyncio.run(f("hmc")))
    args = [
        group,
        "configure-security-policy",
        "target-R1",
        "sys-R1",
        "--security-policy",
        '{"secure_boot":0}',
    ]
    declined = CliRunner().invoke(app, args, input="n\n")
    assert declined.exit_code != 0
    call.assert_not_awaited()
    accepted = CliRunner().invoke(app, args + ["--yes", "--ownership-override"])
    assert accepted.exit_code == 0, accepted.output
    assert call.await_args.args[2] == SETTINGS
    assert call.await_args.kwargs["ownership_override"] is True
    assert call.await_args.kwargs.get("vios", False) is (group == "vios")
    for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
        call.return_value = replace(RESULT, status=status)
        assert CliRunner().invoke(app, args + ["--yes"]).exit_code == 1


@pytest.mark.parametrize("group", ["lpars", "vios"])
@pytest.mark.parametrize("creation", [False, True])
def test_cli_private_inline_input(monkeypatch, group, creation):
    from hmcpctl.cli_commands.lpar import config, create
    from hmcpctl.cli_commands.vios import core

    module = core if group == "vios" else create if creation else config
    reached = []
    monkeypatch.setattr(module, "with_client", lambda *a, **kw: reached.append(True))
    args = (
        [group, "create", "target-R1", "--system", "sys-R1"]
        if creation
        else [group, "configure-security-policy", "target-R1", "sys-R1"]
    )
    for invalid in (
        MARKER,
        "@" + MARKER,
        json.dumps({"secure_boot": MARKER}),
        json.dumps({MARKER: True}),
        json.dumps({MARKER: "x" * 65536}),
        '["' + MARKER + '"]',
    ):
        result = CliRunner().invoke(app, args + ["--security-policy", invalid, "--yes"])
        assert result.exit_code != 0
        assert MARKER not in result.output
        assert len(result.output) < 2000
    assert reached == []


@pytest.mark.parametrize(
    "case",
    [
        "happy",
        "ambiguous",
        "unsafe",
        "stale",
        "permission-privacy",
        "bounds",
        "regression",
    ],
)
@pytest.mark.asyncio
async def test_spec_evaluation_cases(policy_target, monkeypatch, case):  # noqa: F811 -- imported pytest fixture
    from hmcpctl.errors import HMCError
    from hmcpctl.server_tools.lpar import configuration

    hmc, entry, command = policy_target
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setattr(
        configuration, "with_client", lambda f, **kw: asyncio.run(f(hmc))
    )
    settings = {"secure_boot": 0}
    if case == "ambiguous":
        entry["Resource"].pop("KeyStoreSize")
        settings = {"keystore_signed_updates": True}
    elif case == "unsafe":
        entry["Resource"]["KeyStoreSignedUpdatesEnabled"] = "true"
        settings = {"keystore_signed_updates": False}
    elif case == "stale":
        hmc.get_managed_system.return_value["Resource"][
            "AssociatedSystemCapabilities"
        ].pop("PartitionSecureBootCapable")
    elif case == "permission-privacy":
        entry["Resource"]["PartitionType"] = "Virtual IO Server"
    elif case == "bounds":
        command.side_effect = HMCError(MARKER)
    elif case == "regression":
        entry["Resource"]["PendingSecureBoot"] = "1"
        settings = {"secure_boot": 1}
    name = "hmc_configure_lpar_security_policy"
    async with Client(policy_app(name)) as client:
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": settings,
            },
            raise_on_error=False,
        )
    assert MARKER not in str(result)
    if case in {"ambiguous", "unsafe", "stale", "permission-privacy"}:
        assert result.is_error
        command.assert_not_awaited()
    else:
        assert not result.is_error, result
        data = result.structured_content
        assert data["status"] == (
            "write-outcome-unknown" if case == "bounds" else "verified"
        )
        command.assert_awaited_once()
        if case == "regression":
            assert data["observed"]["current_secure_boot"] == 0
            assert data["observed"]["settings"]["secure_boot"] == 1
            assert any("restart" in warning.lower() for warning in data["warnings"])


def test_creation_presenters_forward_false_zero_policy(monkeypatch):
    from hmcpctl.cli_commands.lpar import create
    from hmcpctl.cli_commands.vios import core as vios_cli
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.server_tools.lpar import lifecycle_create
    from hmcpctl.server_tools.vios import core as vios_tools

    settings = LparSecurityPolicy(
        secure_boot=0, keystore_kbytes=0, keystore_signed_updates=False
    )
    workflow = LparPcieWorkflowResult(True, True, {"UUID": "created"}, True, (), ())
    lpar = AsyncMock(return_value=workflow)
    vios = AsyncMock(return_value={"UUID": "created-vios"})
    for module in (create, lifecycle_create):
        monkeypatch.setattr(module, "create_lpar", lpar)
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    for module in (vios_cli, vios_tools):
        monkeypatch.setattr(module, "create_vios", vios)
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    lifecycle_create.hmc_create_lpar("sys-R1", "p", security_policy=settings)
    assert lpar.await_args.args[2].security_policy == settings
    vios_tools.hmc_create_vios("sys-R1", "v", security_policy=settings)
    assert vios.await_args.kwargs["security_policy"] == settings
    for group in ("lpars", "vios"):
        result = CliRunner().invoke(
            app,
            [
                group,
                "create",
                "p",
                "--system",
                "sys-R1",
                "--security-policy",
                json.dumps(asdict(settings)),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
    assert lpar.await_args.args[2].security_policy == settings
    assert vios.await_args.kwargs["security_policy"] == settings


@pytest.mark.parametrize("kind", ["lpar", "vios"])
def test_read_presenters_preserve_pending_current(monkeypatch, kind):
    from hmcpctl.cli_commands.lpar import config
    from hmcpctl.cli_commands.vios import core as vios_cli
    from hmcpctl.server_tools.lpar import configuration
    from hmcpctl.server_tools.vios import core

    read = AsyncMock(return_value=STATE)
    modules = (core, vios_cli) if kind == "vios" else (configuration, config)
    for module in modules:
        monkeypatch.setattr(module, "read_lpar_security_policy", read)
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    assert (
        getattr(modules[0], f"hmc_get_{kind}_security_policy")("sys-R1", "p") == STATE
    )
    result = CliRunner().invoke(
        app,
        ["vios" if kind == "vios" else "lpars", "get-security-policy", "p", "sys-R1"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["settings"]["secure_boot"] == 0
    assert data["current_secure_boot"] == 2
    assert read.await_args.kwargs.get("vios", False) is (kind == "vios")


@pytest.mark.parametrize(
    "group,payload",
    [
        ("lpars", {"keystore_signed_updates_without_verification": False}),
        ("vios", {"keystore_signed_updates_without_verification": False}),
        ("vios", {"linux_dynamic_key_secure_boot": False}),
    ],
)
def test_creation_context_refuses_before_transport(monkeypatch, group, payload):
    from hmcpctl.cli_commands.lpar import create
    from hmcpctl.cli_commands.vios import core

    module = core if group == "vios" else create
    reached = []
    monkeypatch.setattr(module, "with_client", lambda *a, **kw: reached.append(True))
    result = CliRunner().invoke(
        app,
        [
            group,
            "create",
            "p",
            "--system",
            "sys-R1",
            "--security-policy",
            json.dumps(payload),
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--security-policy" in result.output
    assert reached == []


@pytest.mark.parametrize("group", ["lpars", "vios"])
def test_security_policy_discovery_keeps_narrow_help_bounded(group, monkeypatch):
    from rich.text import Text

    monkeypatch.setenv("COLUMNS", "40")
    runner = CliRunner()
    result = runner.invoke(app, [group, "--help"], terminal_width=40)
    assert result.exit_code == 0
    help_text = Text.from_ansi(result.output).plain
    assert len(help_text) < 20000
    assert "security-policy" in help_text
    detail = runner.invoke(app, [group, "configure-security-policy", "--help"])
    assert detail.exit_code == 0
    assert "irreversible" in Text.from_ansi(detail.output).plain
