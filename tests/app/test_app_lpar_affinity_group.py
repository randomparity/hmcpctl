"""C08 creation adapters retain strict values and existing policy boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create
from hmcpctl.server_tools.vios import core as vios_tools


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


def forwarded(adapter):
    call = adapter.operation.await_args
    return (
        call.args[2].affinity_group_id
        if adapter.kind == "lpar"
        else call.kwargs["affinity_group_id"]
    )


@pytest.mark.asyncio
async def test_presentations(adapter):
    async with Client(application(adapter.name)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        definition = tools[adapter.name].input_schema["properties"]["affinity_group_id"]
        assert definition["description"] and definition["default"] is None
        assert TOOL_SECURITY[adapter.name].operation == f"{adapter.kind}.create"
        for value in (None, "none", 1, 255):
            arguments = {"system_name_or_uuid": "sys-R1", "name": "target-R1"}
            if value is not None:
                arguments["affinity_group_id"] = value
            result = await client.call_tool(adapter.name, arguments)
            assert not result.is_error and forwarded(adapter) == value
        adapter.operation.reset_mock()
        adapter.connections.clear()
        for value in (
            True,
            False,
            0,
            256,
            1.0,
            "1",
            "NONE",
            "PRIVATE" * 5000,
            ["PRIVATE"],
            {"PRIVATE": 1},
        ):
            result = await client.call_tool(
                adapter.name,
                {
                    "system_name_or_uuid": "sys-R1",
                    "name": "target-R1",
                    "affinity_group_id": value,
                },
                raise_on_error=False,
            )
            assert (
                result.is_error
                and "PRIVATE" not in str(result)
                and len(str(result)) < 4000
            )
        adapter.operation.assert_not_awaited()
        assert adapter.connections == []
    other = f"hmc_create_{'vios' if adapter.kind == 'lpar' else 'lpar'}"
    async with Client(application(other)) as client:
        denied = await client.call_tool(
            adapter.name,
            {
                "system_name_or_uuid": "sys-R1",
                "name": "target-R1",
                "affinity_group_id": 1,
            },
            raise_on_error=False,
        )
        assert denied.is_error
    adapter.operation.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "none", 1, 255])
def test_direct_and_cli(adapter, monkeypatch, value):
    assert (
        adapter.handler("sys-R1", "target-R1", affinity_group_id=value)
        == adapter.result
    )
    assert forwarded(adapter) == value

    def run(fn):
        return asyncio.run(fn(adapter.client))

    monkeypatch.setattr(adapter.cli_module, "with_client", run)
    args = [adapter.group, "create", "target-R1", "--system", "sys-R1"]
    if value is not None:
        args.extend(["--affinity-group-id", str(value)])
    declined = CliRunner().invoke(app, args, input="n\n")
    assert declined.exit_code == 1
    assert adapter.operation.await_count == 1
    result = CliRunner().invoke(app, args + ["--yes"])
    assert result.exit_code == 0, result.output
    assert forwarded(adapter) == value


@pytest.mark.parametrize(
    "value", ["0", "256", "-1", "1.0", "NONE", " none", "1,other=1"]
)
def test_invalid_cli_before_client(adapter, value):
    result = CliRunner().invoke(
        app,
        [
            adapter.group,
            "create",
            "target-R1",
            "--system",
            "sys-R1",
            "--affinity-group-id",
            value,
            "--yes",
        ],
    )
    assert result.exit_code != 0
    adapter.operation.assert_not_awaited()
    assert adapter.connections == []
