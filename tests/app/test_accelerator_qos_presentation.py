"""Accelerator QoS presentations preserve the owner and dispatch contracts."""

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from lpar.test_accelerator_qos import accelerator_boundary  # noqa: F401 -- shared external boundary
from test_connection_authorization import (  # noqa: F401 -- shared config isolation
    _policy,
    _seal_every_outbound_path,
    lab_profile,
)
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import config
from hmcpctl.documents.lpar_accelerator import LparAcceleratorQos
from hmcpctl.operations.lpar.accelerator import set_lpar_accelerator_qos
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import configuration

TOOL = "hmc_set_lpar_accelerator_qos"
ARGUMENTS = {
    "lpar_name_or_uuid": "part-test",
    "system_name_or_uuid": "sys-test",
    "change": {"qos": 3},
    "profile": "lab",
}
GRANT = {
    "tools": [TOOL],
    "connections": ["lab"],
    "targets": {"managed_system": ["sys-test"], "lpar": ["part-test"]},
}
CLI = ["lpars", "accelerator-qos", "part-test", "-s", "sys-test", "--qos"]


@pytest.fixture
def presentation_boundary(request, monkeypatch):
    boundary = request.getfixturevalue("accelerator_boundary")
    boundary.profiles = []

    @asynccontextmanager
    async def connect(profile=None):
        boundary.profiles.append(profile)
        yield boundary.hmc

    monkeypatch.setattr("hmcpctl._app.client_from_env", connect)
    monkeypatch.setattr("hmcpctl.cli_commands.runtime.client", connect)
    boundary.owner = AsyncMock(wraps=set_lpar_accelerator_qos)
    for module in (config, configuration):
        monkeypatch.setattr(module, "set_lpar_accelerator_qos", boundary.owner)
    return boundary


@pytest.mark.parametrize(
    "qos,options,extra,completed",
    [
        (0, {}, [], True),
        (
            3,
            {"wait_minutes": 0, "detail_level": 4, "force": True},
            [
                "--wait-minutes",
                "0",
                "--detail-level",
                "4",
                "--force",
                "--ownership-override",
            ],
            True,
        ),
        (3, {}, [], False),
    ],
)
def test_cli_mapping(presentation_boundary, qos, options, extra, completed):
    b = presentation_boundary
    observed = qos if completed else qos + 1
    b.after_rows[0].update(curr_qos=str(observed), pend_qos=str(observed))
    result = CliRunner().invoke(app, [*CLI, str(qos), *extra, "-y"])
    assert result.exit_code == (0 if completed else 1), result.output
    payload = json.loads(result.output)
    assert payload["workflow_completed"] is completed
    assert payload["requested_qos"] == qos
    assert payload["completion_scope"] == "native-current-and-pending"
    assert set(payload) == {
        "UUID",
        "requested_qos",
        "submission",
        "submission_error",
        "before",
        "after",
        "observation_error",
        "outcome",
        "workflow_completed",
        "completion_scope",
        "effective_runtime",
        "profile_persistence",
        "restart_required",
        "warnings",
    }
    assert payload["before"]["native"] == {"curr_qos": 0, "pend_qos": 0}
    assert payload["after"]["native"] == {"curr_qos": observed, "pend_qos": observed}
    b.owner.assert_awaited_once_with(
        b.hmc,
        "part-test",
        LparAcceleratorQos(qos=qos, **options),
        system_name_or_uuid="sys-test",
        ownership_override="--ownership-override" in extra,
    )
    assert len(b.sent) == 1 and f"-q {qos}" in b.sent[0]
    assert ("-w 0" in b.sent[0]) is (options.get("wait_minutes") == 0)


@pytest.mark.parametrize(
    "args",
    [
        [*CLI, "3"],
        [*CLI, "-1"],
        [*CLI, "3", "--wait-minutes", "-1"],
        [*CLI, "3", "--detail-level", "5"],
        ["lpars", "accelerator-qos", " ", "--system", "sys-test", "--qos", "3"],
        ["lpars", "accelerator-qos", "part-test", "--system", " ", "--qos", "3"],
        ["lpars", "accelerator-qos", "part-test", "--qos", "3"],
        ["lpars", "accelerator-qos", "part-test", "--system", "sys-test"],
    ],
)
def test_cli_cancel_invalid(monkeypatch, args):
    client = MagicMock(side_effect=AssertionError("must not construct client"))
    confirm = MagicMock(return_value=False)
    monkeypatch.setattr("hmcpctl.cli_commands.runtime.client", client)
    monkeypatch.setattr(config.typer, "confirm", confirm)
    result = CliRunner().invoke(app, args)
    if args == [*CLI, "3"]:
        assert result.exit_code == 0, result.output
        prompt = confirm.call_args.args[0]
        assert all(
            word in prompt
            for word in ("part-test", "sys-test", "3", "force", "restart")
        )
    else:
        assert result.exit_code == 2, result.output
        confirm.assert_not_called()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_schema_and_delegate(presentation_boundary, caplog):
    b = presentation_boundary
    b.partition["Resource"]["ResourceMonitoringControlState"] = "inactive"
    application = create_mcp(_policy([GRANT]))
    async with Client(application) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert TOOL in tools
        tool = tools[TOOL]
        schema = tool.input_schema
        assert set(schema["required"]) == {
            "lpar_name_or_uuid",
            "system_name_or_uuid",
            "change",
        }
        request = schema["properties"]["change"]
        assert request["additionalProperties"] is False
        assert request["required"] == ["qos"]
        fields = request["properties"]
        assert set(fields) == {"qos", "wait_minutes", "detail_level", "force"}
        assert fields["qos"]["type"] == "integer" and fields["qos"]["minimum"] == 0
        assert (
            fields["force"]["type"] == "boolean" and fields["force"]["default"] is False
        )
        assert fields["wait_minutes"]["anyOf"][0]["minimum"] == 0
        assert fields["detail_level"]["anyOf"][0]["minimum"] == 1
        assert fields["detail_level"]["anyOf"][0]["maximum"] == 4
        assert all(field["description"] for field in fields.values())
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is None
        with caplog.at_level(logging.INFO, logger="hmcpctl.audit"):
            result = await client.call_tool(
                TOOL,
                {
                    **ARGUMENTS,
                    "change": {
                        "qos": 3,
                        "wait_minutes": 0,
                        "detail_level": 4,
                        "force": True,
                    },
                    "ownership_override": True,
                },
            )
    assert result.data["workflow_completed"] is True
    assert result.data["restart_required"] is True
    assert b.profiles == ["lab"]
    b.owner.assert_awaited_once_with(
        b.hmc,
        "part-test",
        LparAcceleratorQos(qos=3, wait_minutes=0, detail_level=4, force=True),
        system_name_or_uuid="sys-test",
        ownership_override=True,
    )
    assert len(b.sent) == 1 and "-w 0 -d 4 --force" in b.sent[0]
    assert (
        sum(
            '"event": "ownership-override"' in record.message
            for record in caplog.records
        )
        == 2
    )
    security = TOOL_SECURITY[TOOL]
    assert (security.effect, security.operation, security.target_kind) == (
        "mutate",
        "lpar.set_accelerator_qos",
        "lpar",
    )
    assert security.connection_argument == "profile" and security.exhaustive_targets
    assert {(t.kind, t.argument, t.required) for t in security.targets} == {
        ("lpar", "lpar_name_or_uuid", True),
        ("managed_system", "system_name_or_uuid", True),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"qos": "3"},
        {"qos": True},
        {"qos": 3, "force": 1},
        {"qos": 3, "wait_minutes": "0"},
        {"qos": 3, "extra": 1},
    ],
)
async def test_mcp_schema_and_delegate_strict(monkeypatch, change):
    opened = []
    _seal_every_outbound_path(monkeypatch, opened)
    async with Client(create_mcp(_policy([GRANT]))) as client:
        with pytest.raises(ToolError, match="validation error"):
            await client.call_tool(TOOL, {**ARGUMENTS, "change": change})
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grants,arguments,reason",
    [
        ([GRANT], {**ARGUMENTS, "profile": "prod"}, "connection 'prod'"),
        ([GRANT], {**ARGUMENTS, "system_name_or_uuid": "other"}, "target"),
        ([GRANT], {**ARGUMENTS, "lpar_name_or_uuid": "other"}, "target"),
        (
            [
                {
                    **GRANT,
                    "targets": {"managed_system": ["sys-test"], "lpar": ["other"]},
                },
                {
                    **GRANT,
                    "targets": {"managed_system": ["other"], "lpar": ["part-test"]},
                },
            ],
            ARGUMENTS,
            "target",
        ),
        ([{**GRANT, "tools": ["hmc_get_lpar_description"]}], ARGUMENTS, "Unknown tool"),
    ],
)
async def test_mcp_target_denial(monkeypatch, grants, arguments, reason):
    opened = []
    _seal_every_outbound_path(monkeypatch, opened)
    async with Client(create_mcp(_policy(grants))) as client:
        with pytest.raises(ToolError, match=reason):
            await client.call_tool(TOOL, arguments)
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "lab"])
async def test_mcp_target_denial_complete_grant_succeeds(
    presentation_boundary, profile
):
    b = presentation_boundary
    grant = {**GRANT, "connections": ["lab", "<default>"]}
    async with Client(create_mcp(_policy([grant]))) as client:
        arguments = {**ARGUMENTS, "profile": profile}
        if profile is None:
            arguments.pop("profile")
        result = await client.call_tool(TOOL, arguments)
    assert result.data["workflow_completed"] is True
    assert b.profiles == [profile] and b.counts["ownership"] == 2 and len(b.sent) == 1
    b.owner.assert_awaited_once_with(
        b.hmc,
        "part-test",
        LparAcceleratorQos(qos=3),
        system_name_or_uuid="sys-test",
        ownership_override=False,
    )


def test_cli_mapping_confirmed(presentation_boundary):
    b = presentation_boundary
    result = CliRunner().invoke(app, [*CLI, "3", "--force"], input="y\n")
    assert result.exit_code == 0, result.output
    assert all(
        word in result.output
        for word in ("part-test", "sys-test", "QoS=3", "force=True", "restart")
    )
    assert '"workflow_completed": true' in result.output
    b.owner.assert_awaited_once_with(
        b.hmc,
        "part-test",
        LparAcceleratorQos(qos=3, force=True),
        system_name_or_uuid="sys-test",
        ownership_override=False,
    )


@pytest.mark.asyncio
async def test_mcp_target_denial_ownership(presentation_boundary, caplog):
    b = presentation_boundary
    b.description = "[hmcpctl owner:foreign created:2026-10-26]"
    async with Client(create_mcp(_policy([GRANT]))) as client:
        with (
            caplog.at_level(logging.INFO, logger="hmcpctl.audit"),
            pytest.raises(ToolError, match="owned by"),
        ):
            await client.call_tool(TOOL, ARGUMENTS)
    assert not b.sent
    assert any(
        '"event": "ownership-denied"' in record.message for record in caplog.records
    )
