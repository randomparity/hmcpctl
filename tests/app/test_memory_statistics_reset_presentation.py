"""Statistics reset command intent, typed settings and real MCP authorization."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_connection_authorization import _policy, lab_profile  # noqa: F401 — named profile fixture
from test_target_authorization import _authorize
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import AccessPolicyError
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import memory
from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle

TOOL = "hmc_reset_lpar_memory_statistics"


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_cli_reset(monkeypatch, complete, override):
    hmc = object()
    operation = AsyncMock(
        return_value={
            "workflow_completed": complete,
            "reset_verification": "unavailable",
        }
    )
    monkeypatch.setattr(
        memory, "reset_lpar_memory_statistics", operation, raising=False
    )
    monkeypatch.setattr(memory, "with_client", lambda fn: asyncio.run(fn(hmc)))
    args = [
        "lpars",
        "memory-statistics-reset",
        "guest-R1",
        "--system",
        "sys-R1",
        "--wait-minutes",
        "0",
        "--detail-level",
        "4",
        "--yes",
    ]
    if override:
        args.append("--ownership-override")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == (0 if complete else 1), result.output
    assert json.loads(result.output)["reset_verification"] == "unavailable"
    operation.assert_awaited_once_with(
        hmc,
        "guest-R1",
        LparMemoryStatisticsReset(wait_minutes=0, detail_level=4),
        system_name_or_uuid="sys-R1",
        ownership_override=override,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--wait-minutes", "-1"],
        ["--detail-level", "0"],
        ["--detail-level", "5"],
        ["--detail-level", "1.5"],
    ],
)
def test_cli_rejects_invalid_settings_before_client(monkeypatch, args):
    client = Mock()
    monkeypatch.setattr(memory, "with_client", client)
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory-statistics-reset",
            "guest-R1",
            "--system",
            "sys-R1",
            "--yes",
            *args,
        ],
    )
    assert result.exit_code != 0
    client.assert_not_called()


def test_cli_cancel_and_blank_selector(monkeypatch):
    client = Mock()
    monkeypatch.setattr(memory, "with_client", client)
    args = ["lpars", "memory-statistics-reset", "guest-R1", "--system", "sys-R1"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0 and "history" in result.output
    assert (
        CliRunner()
        .invoke(
            app,
            ["lpars", "memory-statistics-reset", " ", "--system", "sys-R1", "--yes"],
        )
        .exit_code
        != 0
    )
    client.assert_not_called()


@pytest.mark.parametrize("override", [False, True])
def test_mcp_schema_default_and_forwarding(monkeypatch, override):
    operation = AsyncMock(return_value={"workflow_completed": True})
    monkeypatch.setattr(
        lifecycle, "reset_lpar_memory_statistics", operation, raising=False
    )
    hmc = object()
    client = Mock(side_effect=lambda fn, **kw: asyncio.run(fn(hmc)))
    monkeypatch.setattr(lifecycle, "with_client", client)
    result = lifecycle.hmc_reset_lpar_memory_statistics(
        "guest-R1", "sys-R1", profile="lab", ownership_override=override
    )
    assert result["workflow_completed"]
    operation.assert_awaited_once_with(
        hmc,
        "guest-R1",
        LparMemoryStatisticsReset(),
        system_name_or_uuid="sys-R1",
        ownership_override=override,
    )
    assert client.call_args.kwargs == {"profile": "lab"}
    security = TOOL_SECURITY[TOOL]
    assert not security.exhaustive_targets
    assert security.operation == "lpar.reset_memory_statistics"


@pytest.mark.parametrize("kind", ["lpar", "vios"])
@pytest.mark.parametrize("override", [False, True])
def test_override_cannot_bypass_target_grants(kind, override):
    args = {
        "lpar_name_or_uuid": "guest-R1",
        "system_name_or_uuid": "sys-R1",
        "profile": "lab",
        "ownership_override": override,
    }
    grant = {
        "tools": [TOOL],
        "connections": ["lab"],
        "targets": {"managed_system": ["sys-R1"], kind: ["guest-R1"]},
    }
    with pytest.raises(AccessPolicyError):
        _authorize([grant], TOOL, args)
    grant["targets"] = "all-targets"
    _authorize([grant], TOOL, args)


@pytest.mark.asyncio
async def test_actual_mcp_wrong_connection_denies_before_client(monkeypatch):
    client = Mock(side_effect=AssertionError("unauthorized client opened"))
    monkeypatch.setattr(lifecycle, "with_client", client)
    application = create_mcp(
        _policy([{"tools": [TOOL], "connections": ["lab"], "targets": "all-targets"}])
    )
    async with Client(application) as connection:
        with pytest.raises(ToolError):
            await connection.call_tool(
                TOOL,
                {
                    "lpar_name_or_uuid": "guest-R1",
                    "system_name_or_uuid": "sys-R1",
                    "profile": "foreign",
                    "ownership_override": True,
                },
            )
    client.assert_not_called()
