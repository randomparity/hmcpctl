"""CLI and real MCP transport expose strict serial input through existing owners."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create
from hmcpctl.server_tools.vios import core as vios_tools

RECORD = {
    "local_slot": 2,
    "role": "server",
    "is_required": False,
    "supports_hmc": 0,
    "remote_lpar_id": 7,
    "remote_lpar_name": "peer",
    "remote_slot": 3,
}


@pytest.fixture(params=("lpar", "vios"))
def adapter(request, monkeypatch):
    kind = request.param
    tools = lifecycle_create if kind == "lpar" else vios_tools
    cli = create if kind == "lpar" else vios_cli
    result = (
        LparPcieWorkflowResult(True, True, {}, True, (), ()) if kind == "lpar" else {}
    )
    operation = AsyncMock(return_value=result)
    connections = []

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(object()))

    for module in (tools, cli):
        monkeypatch.setattr(module, "with_client", run)
        monkeypatch.setattr(module, f"create_{kind}", operation)
    return SimpleNamespace(
        kind=kind,
        operation=operation,
        connections=connections,
        handler=getattr(tools, f"hmc_create_{kind}"),
        group="lpars" if kind == "lpar" else "vios",
    )


def forwarded(adapter):
    call = adapter.operation.await_args
    if adapter.kind == "lpar":
        return call.args[2].virtual_serial_adapters, call.args[2].max_virtual_slots
    return call.kwargs["virtual_serial_adapters"], call.kwargs["max_virtual_slots"]


def test_cli_forwards_all_seven_fields_and_maximum(adapter):
    result = CliRunner().invoke(
        app,
        [
            adapter.group,
            "create",
            "new",
            "--system",
            "system",
            "--yes",
            "--max-virtual-slots",
            "8",
            "--virtual-serial-adapters",
            json.dumps([RECORD]),
        ],
    )
    assert result.exit_code == 0, result.output
    serial, maximum = forwarded(adapter)
    assert maximum == 8 and len(serial) == 1 and asdict(serial[0]) == RECORD


@pytest.mark.parametrize(
    "payload",
    [
        "null",
        "{}",
        "[{}]",
        "@private-file",
        "[",
        '"private-value"',
        json.dumps([{**RECORD, "extra": "private-value"}]),
        json.dumps([{**RECORD, "local_slot": "2"}]),
        json.dumps([{**RECORD, "is_required": 0}]),
        json.dumps([{**RECORD, "supports_hmc": 1}]),
        json.dumps([{**RECORD, "remote_lpar_name": "private/value"}]),
        " " * 65537,
        '"' + "é" * 32768 + '"',
    ],
)
def test_cli_invalid_inline_input_refuses_before_prompt_or_client(adapter, payload):
    result = CliRunner().invoke(
        app,
        [
            adapter.group,
            "create",
            "new",
            "--system",
            "system",
            "--max-virtual-slots",
            "8",
            "--virtual-serial-adapters",
            payload,
        ],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "Create LPAR" not in result.output and "Create VIOS" not in result.output
    assert "private-value" not in result.output and "private/value" not in result.output
    assert adapter.connections == []
    adapter.operation.assert_not_awaited()


@pytest.mark.parametrize("maximum", [None, "2"])
def test_cli_local_slot_maximum_checked_before_prompt(adapter, maximum):
    args = [
        adapter.group,
        "create",
        "new",
        "--system",
        "system",
        "--virtual-serial-adapters",
        json.dumps([RECORD]),
    ]
    if maximum is not None:
        args += ["--max-virtual-slots", maximum]
    result = CliRunner().invoke(app, args, input="y\n")
    assert result.exit_code != 0
    assert "Create LPAR" not in result.output and "Create VIOS" not in result.output
    assert adapter.connections == []


def test_direct_handler_forwards_typed_tuple(adapter):
    value = (VirtualSerialAdapter(**RECORD),)
    adapter.handler("system", "new", virtual_serial_adapters=value, max_virtual_slots=8)
    assert forwarded(adapter) == (value, 8)


@pytest.mark.asyncio
async def test_real_mcp_schema_and_round_trip(adapter):
    tool = f"hmc_create_{adapter.kind}"
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [tool],
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
    server = create_mcp(policy)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        schema = tools[tool].input_schema
        serial = schema["properties"]["virtual_serial_adapters"]
        assert serial.get("description")
        fields = serial["items"]["properties"]
        assert set(fields) == set(RECORD)
        assert all(field.get("description") for field in fields.values())
        result = await client.call_tool(
            tool,
            {
                "system_name_or_uuid": "system",
                "name": "new",
                "max_virtual_slots": 8,
                "virtual_serial_adapters": [RECORD],
            },
        )
        assert not result.is_error
        assert asdict(forwarded(adapter)[0][0]) == RECORD
        adapter.operation.reset_mock()
        for bad in (
            {**RECORD, "extra": 1},
            {**RECORD, "is_required": 0},
            {**RECORD, "remote_slot": "3"},
        ):
            with pytest.raises(Exception, match="validation|Validation|Error"):
                await client.call_tool(
                    tool,
                    {
                        "system_name_or_uuid": "system",
                        "name": "new",
                        "max_virtual_slots": 8,
                        "virtual_serial_adapters": [bad],
                    },
                )
        adapter.operation.assert_not_awaited()


def test_cli_accepts_exact_64kib_boundary(adapter):
    payload = json.dumps([RECORD])
    payload += " " * (65536 - len(payload.encode("utf-8")))
    assert len(payload.encode("utf-8")) == 65536
    result = CliRunner().invoke(
        app,
        [
            adapter.group,
            "create",
            "new",
            "--system",
            "system",
            "--yes",
            "--max-virtual-slots",
            "8",
            "--virtual-serial-adapters",
            payload,
        ],
    )
    assert result.exit_code == 0, result.output
    assert asdict(forwarded(adapter)[0][0]) == RECORD
