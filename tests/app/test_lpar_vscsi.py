"""CLI and real MCP transport expose strict vSCSI client input (ADR 0376)."""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.documents.lpar_vscsi import VirtualScsiClientAdapter
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create

RECORD = {
    "local_slot": 2,
    "remote_slot": 20,
    "is_required": False,
    "remote_lpar_id": 7,
    "remote_lpar_name": None,
}


@pytest.fixture
def operation(monkeypatch):
    mock = AsyncMock(return_value=LparPcieWorkflowResult(True, True, {}, True, (), ()))
    connections = []

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(object()))

    for module in (lifecycle_create, create):
        monkeypatch.setattr(module, "with_client", run)
        monkeypatch.setattr(module, "create_lpar", mock)
    mock.connections = connections
    return mock


def forwarded(operation):
    creation = operation.await_args.args[2]
    return creation.virtual_scsi_adapters, creation.max_virtual_slots


def cli(*extra, yes=True):
    args = ["lpars", "create", "new", "--system", "system", *extra]
    return CliRunner().invoke(app, [*args, "--yes"] if yes else args, input="y\n")


def test_cli_forwards_typed_records(operation):
    result = cli(
        "--max-virtual-slots", "8", "--virtual-scsi-adapters", json.dumps([RECORD])
    )
    assert result.exit_code == 0, result.output
    adapters, maximum = forwarded(operation)
    assert maximum == 8 and [asdict(a) for a in adapters] == [RECORD]


@pytest.mark.parametrize(
    "payload",
    [
        "null",
        "{}",
        "[",
        json.dumps([{**RECORD, "role": "server"}]),
        json.dumps([{**RECORD, "extra": "private-value"}]),
        json.dumps([{**RECORD, "remote_lpar_name": "private/value"}]),
        json.dumps([{**RECORD, "remote_lpar_name": "vios-R1"}]),
        json.dumps([{**RECORD, "is_required": 0}]),
        " " * 65537,
    ],
)
def test_cli_invalid_input_refused_before_prompt_or_client(operation, payload):
    result = cli(
        "--max-virtual-slots", "8", "--virtual-scsi-adapters", payload, yes=False
    )
    assert result.exit_code == 2
    assert "--virtual-scsi-adapters" in result.output
    assert "Create LPAR" not in result.output
    assert "private-value" not in result.output and "private/value" not in result.output
    assert operation.connections == []
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        (),
        ("--max-virtual-slots", "2"),
        (
            "--max-virtual-slots",
            "8",
            "--virtual-serial-adapters",
            json.dumps([{"local_slot": 2, "role": "server", "is_required": False}]),
        ),
    ],
    ids=["no-maximum", "at-maximum", "serial-collision"],
)
def test_cli_slot_rules_checked_before_prompt(operation, extra):
    result = cli(*extra, "--virtual-scsi-adapters", json.dumps([RECORD]), yes=False)
    assert result.exit_code == 2
    assert "Create LPAR" not in result.output
    assert operation.connections == []


def test_direct_handler_forwards_typed_tuple(operation):
    value = (VirtualScsiClientAdapter(**RECORD),)
    lifecycle_create.hmc_create_lpar(
        "system", "new", virtual_scsi_adapters=value, max_virtual_slots=8
    )
    assert forwarded(operation) == (value, 8)


@pytest.mark.asyncio
async def test_real_mcp_schema_and_round_trip(operation):
    tool = "hmc_create_lpar"
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
    server = create_mcp(policy, catalog="registered")
    arguments = {"system_name_or_uuid": "system", "name": "new", "max_virtual_slots": 8}
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        prop = tools[tool].input_schema["properties"]["virtual_scsi_adapters"]
        fields = prop["items"]["properties"]
        assert set(fields) == set(RECORD)
        assert all(field.get("description") for field in fields.values())
        result = await client.call_tool(
            tool, {**arguments, "virtual_scsi_adapters": [RECORD]}
        )
        assert not result.is_error
        assert [asdict(a) for a in forwarded(operation)[0]] == [RECORD]
        operation.reset_mock()
        for bad in (
            {**RECORD, "role": "server"},
            {**RECORD, "is_required": 0},
            {**RECORD, "remote_slot": "20"},
            {**RECORD, "remote_lpar_name": "vios-R1"},
        ):
            with pytest.raises(Exception, match="validation|Validation|Error"):
                await client.call_tool(
                    tool, {**arguments, "virtual_scsi_adapters": [bad]}
                )
        operation.assert_not_awaited()
