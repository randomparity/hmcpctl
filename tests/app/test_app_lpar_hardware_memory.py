"""C10 creation adapters preserve strict private flags and existing boundaries."""

import asyncio
import inspect
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.models import ParameterInfo
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import AccessPolicyError, compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.cli_commands.runtime import current_options
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create
from hmcpctl.server_tools.vios import core as vios_tools

FIELDS = ("hardware_mem_encryption", "hardware_mem_expansion")
COMBINATIONS = tuple(product((None, False, True), repeat=2))
MARKER = "PRIVATE-HARDWARE-MARKER"
POLICY = LparSecurityPolicy(secure_boot=0)


@pytest.fixture(params=("lpar", "vios"))
def adapter(request, monkeypatch):
    kind = request.param
    mcp_module = lifecycle_create if kind == "lpar" else vios_tools
    cli_module = create if kind == "lpar" else vios_cli
    result = (
        LparPcieWorkflowResult(True, True, {"UUID": "target-R1"}, True, (), ())
        if kind == "lpar"
        else {"UUID": "target-R1"}
    )
    operation = AsyncMock(return_value=result)
    connections = []
    client = object()

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(client))

    for module in (mcp_module, cli_module):
        monkeypatch.setattr(module, f"create_{kind}", operation)
        monkeypatch.setattr(module, "with_client", run)
    return SimpleNamespace(
        kind=kind,
        group="lpars" if kind == "lpar" else "vios",
        name=f"hmc_create_{kind}",
        handler=getattr(mcp_module, f"hmc_create_{kind}"),
        cli=getattr(cli_module, "lpars_create" if kind == "lpar" else "vios_create"),
        cli_module=cli_module,
        operation=operation,
        connections=connections,
        client=client,
        result=result,
    )


def application(name, targets="all-targets"):
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [name],
                            "connections": ["<default>"],
                            "targets": targets,
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


def assert_forwarded(adapter, encryption, expansion):
    call = adapter.operation.await_args
    assert call.args[:2] == (adapter.client, "sys-R1")
    values = call.args[2] if adapter.kind == "lpar" else SimpleNamespace(**call.kwargs)
    assert values.hardware_mem_encryption is encryption
    assert values.hardware_mem_expansion is expansion
    assert (
        call.args[2].name if adapter.kind == "lpar" else call.args[2]
    ) == "target-R1"


@pytest.mark.asyncio
async def test_actual_mcp_strict_private_schema_and_grants(adapter):
    async with Client(application(adapter.name)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = tools[adapter.name].input_schema
        for field in FIELDS:
            definition = schema["properties"][field]
            assert {item["type"] for item in definition["anyOf"]} == {"boolean", "null"}
            assert definition["default"] is None
            assert field not in schema["required"]
            assert definition["description"]
        assert TOOL_SECURITY[adapter.name].operation == f"{adapter.kind}.create"
        assert TOOL_SECURITY[adapter.name].effect == "mutate"
        assert TOOL_SECURITY[adapter.name].target_kind == "managed_system"
        for field in FIELDS:
            for invalid in (
                0,
                1,
                "true",
                "false",
                MARKER,
                [MARKER],
                {MARKER: True},
                MARKER * 5000,
            ):
                result = await client.call_tool(
                    adapter.name,
                    {
                        "system_name_or_uuid": "sys-R1",
                        "name": "target-R1",
                        field: invalid,
                    },
                    raise_on_error=False,
                )
                assert result.is_error
                assert MARKER not in str(result)
                assert len(str(result)) < 4000
                adapter.operation.assert_not_awaited()
                assert adapter.connections == []
    # Both creation tools can select nested peers that scalar grants cannot bound.
    with pytest.raises(AccessPolicyError, match="no target selector"):
        application(adapter.name, {"managed_system": ["sys-R2"]})
    other_tool = f"hmc_create_{'vios' if adapter.kind == 'lpar' else 'lpar'}"
    async with Client(application(other_tool)) as client:
        denied = await client.call_tool(
            adapter.name,
            {
                "system_name_or_uuid": "sys-R1",
                "name": "target-R1",
                "hardware_mem_encryption": True,
                "hardware_mem_expansion": False,
            },
            raise_on_error=False,
        )
        assert denied.is_error
    adapter.operation.assert_not_awaited()
    assert adapter.connections == []


@pytest.mark.parametrize("encryption,expansion", COMBINATIONS)
@pytest.mark.parametrize("omit_none", (False, True))
@pytest.mark.asyncio
async def test_actual_mcp_all_combinations(adapter, encryption, expansion, omit_none):
    arguments = {"system_name_or_uuid": "sys-R1", "name": "target-R1"}
    arguments.update(
        {
            key: value
            for key, value in zip(FIELDS, (encryption, expansion))
            if value is not None or not omit_none
        }
    )
    async with Client(application(adapter.name)) as client:
        result = await client.call_tool(adapter.name, arguments)
    assert not result.is_error
    adapter.operation.assert_awaited_once()
    assert_forwarded(adapter, encryption, expansion)
    assert adapter.connections == [{"profile": None}]


@pytest.mark.parametrize("encryption,expansion", COMBINATIONS)
def test_direct_mcp_adapter_preserves_connection_and_flags(
    adapter, encryption, expansion
):
    result = adapter.handler(
        "sys-R1",
        "target-R1",
        profile="connection-R1",
        security_policy=POLICY,
        hardware_mem_encryption=encryption,
        hardware_mem_expansion=expansion,
    )
    assert result == adapter.result
    assert_forwarded(adapter, encryption, expansion)
    assert adapter.connections == [{"profile": "connection-R1"}]
    call = adapter.operation.await_args
    assert (
        call.args[2].security_policy
        if adapter.kind == "lpar"
        else call.kwargs["security_policy"]
    ) == POLICY


@pytest.mark.parametrize("encryption,expansion", COMBINATIONS)
def test_cli_confirmation_and_exact_forwarding(
    adapter, monkeypatch, encryption, expansion
):
    def run(fn):
        adapter.connections.append(current_options().profile)
        return asyncio.run(fn(adapter.client))

    monkeypatch.setattr(adapter.cli_module, "with_client", run)
    args = [
        "--profile",
        "connection-R1",
        adapter.group,
        "create",
        "target-R1",
        "--system",
        "sys-R1",
    ]
    for field, value in zip(FIELDS, (encryption, expansion)):
        if value is not None:
            args.append("--" + ("" if value else "no-") + field.replace("_", "-"))
    runner = CliRunner()
    declined = runner.invoke(app, args, input="n\n")
    assert declined.exit_code == 1, declined.output
    assert "Create" in declined.output
    adapter.operation.assert_not_awaited()
    assert adapter.connections == []
    confirmed = runner.invoke(app, args, input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert_forwarded(adapter, encryption, expansion)
    skipped = runner.invoke(app, args + ["--yes"])
    assert skipped.exit_code == 0, skipped.output
    assert "[y/N]" not in skipped.output
    assert_forwarded(adapter, encryption, expansion)
    assert adapter.operation.await_count == 2
    assert adapter.connections == ["connection-R1", "connection-R1"]


@pytest.mark.parametrize("surface", ("mcp", "cli"))
def test_old_positional_adapters_preserve_security_and_yes(
    adapter, surface, monkeypatch
):
    fn = adapter.handler if surface == "mcp" else adapter.cli
    parameters = inspect.signature(fn).parameters
    names = list(parameters)
    hardware_position = names.index("hardware_mem_encryption")
    assert (
        hardware_position
        == {
            ("lpar", "mcp"): 16,
            ("vios", "mcp"): 6,
            ("lpar", "cli"): 35,
            ("vios", "cli"): 6,
        }[(adapter.kind, surface)]
    )
    assert names[hardware_position : hardware_position + 2] == list(FIELDS)
    expected_tail = (
        ["memory_layout"]
        if surface == "mcp"
        else (
            [
                "min_num_huge_pages",
                "desired_num_huge_pages",
                "max_num_huge_pages",
                "hpt_ratio",
                "ppt_ratio",
                "kvm_reserved_mem_percent",
            ]
            if adapter.kind == "lpar"
            else ["hpt_ratio", "ppt_ratio"]
        )
    )
    expected_tail += (
        ["virtual_serial_adapters"]
        if surface == "mcp" and adapter.kind == "lpar"
        else ["virtual_serial_adapters", "max_virtual_slots"]
        if surface == "mcp"
        else ["max_virtual_slots", "virtual_serial_adapters"]
    )
    assert names[hardware_position + 2 :] == [*expected_tail, "affinity_group_id"]
    assert all(parameters[name].default is None for name in FIELDS)
    if surface == "cli":
        assert parameters["max_virtual_slots"].default is None
        assert parameters["virtual_serial_adapters"].default is None
    last_old = "security_policy" if surface == "mcp" else "yes"
    old_names = [
        name for name in names[: names.index(last_old) + 1] if name not in FIELDS
    ]
    values = {name: parameters[name].default for name in old_names}
    if surface == "cli":
        values = {
            name: value.default if isinstance(value, ParameterInfo) else value
            for name, value in values.items()
        }
    values.update(
        name="target-R1",
        security_policy=POLICY if surface == "mcp" else '{"secure_boot":0}',
    )
    values["system_name_or_uuid" if surface == "mcp" else "system"] = "sys-R1"
    if surface == "mcp":
        values["profile"] = "connection-R1"
    else:
        values["yes"] = True
        monkeypatch.setattr(
            "typer.confirm",
            lambda *args, **kwargs: pytest.fail("Old yes argument shifted"),
        )
    fn(*(values[name] for name in old_names))
    assert_forwarded(adapter, None, None)
    call = adapter.operation.await_args
    request = call.args[2] if adapter.kind == "lpar" else SimpleNamespace(**call.kwargs)
    assert request.virtual_serial_adapters == ()
    assert request.max_virtual_slots is None
    assert (
        call.args[2].security_policy
        if adapter.kind == "lpar"
        else call.kwargs["security_policy"]
    ) == POLICY
    assert adapter.connections == (
        [{"profile": "connection-R1"}] if surface == "mcp" else [{}]
    )
