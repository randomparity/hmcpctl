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


@pytest.mark.asyncio
async def test_mixed_layout_affinity_forwarding_and_target_authorization(adapter):
    arguments = {
        "system_name_or_uuid": "sys-R1",
        "name": "target-R1",
        "memory_layout": {"hpt_ratio": "1:128"},
        "affinity_group_id": 1,
    }
    async with Client(application(adapter.name)) as client:
        result = await client.call_tool(adapter.name, arguments)
        assert not result.is_error
        assert forwarded(adapter) == 1
        call = adapter.operation.await_args
        layout = (
            call.args[2].memory_layout
            if adapter.kind == "lpar"
            else call.kwargs["memory_layout"]
        )
        assert layout.hpt_ratio == "1:128"
    adapter.operation.reset_mock()
    adapter.connections.clear()
    from hmcpctl.authorization.access_policy import AccessPolicyError

    with pytest.raises(AccessPolicyError, match="has no target selector"):
        application(adapter.name, targets={"managed_system": ["allowed-R1"]})
    other = f"hmc_create_{'vios' if adapter.kind == 'lpar' else 'lpar'}"
    async with Client(application(other)) as client:
        result = await client.call_tool(adapter.name, arguments, raise_on_error=False)
        assert result.is_error
    adapter.operation.assert_not_awaited()
    assert adapter.connections == []


def test_mixed_cli_forwarding(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter.cli_module, "with_client", lambda fn: asyncio.run(fn(adapter.client))
    )
    result = CliRunner().invoke(
        app,
        [
            adapter.group,
            "create",
            "target-R1",
            "--system",
            "sys-R1",
            "--yes",
            "--hpt-ratio",
            "1:128",
            "--affinity-group-id",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert forwarded(adapter) == 1
    call = adapter.operation.await_args
    layout = (
        call.args[2].memory_layout
        if adapter.kind == "lpar"
        else call.kwargs["memory_layout"]
    )
    assert layout.hpt_ratio == "1:128"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter,available",
    [("lpar", True), ("lpar", False), ("vios", True)],
    indirect=["adapter"],
)
async def test_saved_affinity_presentations(adapter, monkeypatch, available):
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    proof = {
        "status": "saved-configuration-verified"
        if available
        else "readback-unavailable",
        "requested": 1,
        "observed": {
            "native-partition": "none",
            "native-profile": 1 if available else None,
        },
        "observation_status": {
            "native-partition": "mismatch",
            "native-profile": "matches-request"
            if available
            else "readback-unavailable",
        },
    }
    expected = (
        LparPcieWorkflowResult(
            True,
            available,
            {"UUID": "target-R1"},
            True,
            (WorkflowStep("affinity_group", "ok" if available else "error", proof),),
            (),
        )
        if adapter.kind == "lpar"
        else {"UUID": "target-R1", "affinity_verification": proof}
    )
    adapter.operation.return_value = expected
    assert (
        await asyncio.to_thread(
            adapter.handler, "sys-R1", "target-R1", affinity_group_id=1
        )
        == expected
    )
    monkeypatch.setattr(
        adapter.cli_module, "with_client", lambda fn: asyncio.run(fn(adapter.client))
    )
    cli = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        [
            adapter.group,
            "create",
            "target-R1",
            "--system",
            "sys-R1",
            "--affinity-group-id",
            "1",
            "--yes",
        ],
    )
    assert cli.exit_code == (1 if adapter.kind == "lpar" and not available else 0)
    assert (
        proof["status"] in cli.output
        and "native-partition" in cli.output
        and "mismatch" in cli.output
    )
    async with Client(application(adapter.name)) as client:
        result = await client.call_tool(
            adapter.name,
            {
                "system_name_or_uuid": "sys-R1",
                "name": "target-R1",
                "affinity_group_id": 1,
            },
        )
    assert not result.is_error
    assert proof["status"] in str(result) and "native-partition" in str(result)
