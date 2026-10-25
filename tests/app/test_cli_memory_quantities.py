"""Typed native memory presentation preserves intent and verified outcomes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_memory import LparMemoryChange
from hmcpctl.server_tools.lpar import lifecycle


def test_native_memory_command_is_registered_and_validates_before_client(monkeypatch):
    from hmcpctl.cli_commands.lpar import memory

    client = MagicMock()
    monkeypatch.setattr(memory, "with_client", client)
    runner = CliRunner()
    for args in [
        ["--operation", "move", "--quantity", "512"],
        ["--operation", "add"],
        ["--operation", "add", "--entitled", "1.5"],
        ["--operation", "set", "--quantity", "-1"],
    ]:
        result = runner.invoke(
            app, ["lpars", "memory", "lpar-R1", "--system", "sys-R1", "--yes", *args]
        )
        assert result.exit_code != 0
    client.assert_not_called()


@pytest.mark.parametrize(
    "outcome,code",
    [("applied", 0), ("noop", 0), ("rejected", 1), ("partial", 1), ("unverified", 1)],
)
def test_cli_memory_delegates_typed_request_and_reports_outcome(
    monkeypatch, outcome, code
):
    from hmcpctl.cli_commands.lpar import memory

    hmc = MagicMock()
    operation = AsyncMock(
        return_value={
            "outcome": outcome,
            "workflow_completed": code == 0,
            "warnings": ["current configuration only"],
        }
    )
    monkeypatch.setattr(memory, "change_lpar_memory", operation)
    monkeypatch.setattr(memory, "with_client", lambda fn: asyncio.run(fn(hmc)))
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory",
            "lpar-R1",
            "--system",
            "sys-R1",
            "--operation",
            "add",
            "--quantity",
            "512",
            "--entitled",
            "auto",
            "--force",
            "--yes",
        ],
    )
    assert result.exit_code == code, result.output
    assert outcome in result.output
    assert "current configuration only" in result.output
    operation.assert_awaited_once_with(
        hmc,
        "lpar-R1",
        LparMemoryChange(
            operation="add", quantity_mib=512, entitled_mib="auto", force=True
        ),
        system_name_or_uuid="sys-R1",
        ownership_override=False,
    )


def test_cli_confirmation_discloses_shutdown_cpu_reset_and_cancel_has_no_effect(
    monkeypatch,
):
    from hmcpctl.cli_commands.lpar import memory

    client = MagicMock()
    monkeypatch.setattr(memory, "with_client", client)
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory",
            "lpar-R1",
            "--system",
            "sys-R1",
            "--operation",
            "remove",
            "--quantity",
            "512",
        ],
        input="n\n",
    )
    assert "processor" in result.output
    assert result.exit_code == 0
    client.assert_not_called()


def test_mcp_typed_change_delegates_without_changing_legacy_resources(monkeypatch):
    hmc = MagicMock()
    native = AsyncMock(return_value={"outcome": "applied"})
    absolute = AsyncMock(return_value={"legacy": True})
    monkeypatch.setattr(lifecycle, "change_lpar_memory", native)
    monkeypatch.setattr(lifecycle, "set_lpar_memory", absolute)
    monkeypatch.setattr(
        lifecycle, "with_client", lambda fn, **kwargs: asyncio.run(fn(hmc))
    )
    request = LparMemoryChange(operation="set", quantity_mib=1024)
    assert lifecycle.hmc_dlpar_mem(
        "lpar-R1", system_name_or_uuid="sys-R1", change=request
    ) == {"outcome": "applied"}
    native.assert_awaited_once_with(
        hmc, "lpar-R1", request, system_name_or_uuid="sys-R1", ownership_override=False
    )
    resources = LparResources(desired_memory=1024)
    assert lifecycle.hmc_dlpar_mem("lpar-R1", resources) == {"legacy": True}
    absolute.assert_awaited_once_with(
        hmc, "lpar-R1", resources, system_name_or_uuid=None, ownership_override=False
    )


def test_mcp_conflicting_resources_or_missing_system_refuse_before_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(lifecycle, "with_client", client)
    request = LparMemoryChange(operation="add", quantity_mib=512)
    for kwargs in [
        {"change": request},
        {
            "change": request,
            "system_name_or_uuid": "sys-R1",
            "resources": LparResources(desired_memory=1024),
        },
    ]:
        with pytest.raises(ValueError):
            lifecycle.hmc_dlpar_mem("lpar-R1", **kwargs)
    client.assert_not_called()


def test_native_mcp_schema_is_closed_and_typed_without_changing_security():
    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))
    tools = asyncio.run(mcp.list_tools())
    tool = next(tool for tool in tools if tool.name == "hmc_dlpar_mem")
    schema = tool.parameters
    assert "change" in schema["properties"]
    model = next(
        item
        for item in schema["properties"]["change"]["anyOf"]
        if item.get("type") == "object"
    )
    assert model["additionalProperties"] is False
    assert model["properties"]["operation"]["enum"] == ["add", "remove", "set"]
    assert model["required"] == ["operation"]
    assert set(model["properties"]) == {
        "operation",
        "quantity_mib",
        "entitled_mib",
        "force",
    }
    security = TOOL_SECURITY["hmc_dlpar_mem"]
    assert security.operation == "lpar.dlpar_mem"
    assert security.effect == "mutate"
