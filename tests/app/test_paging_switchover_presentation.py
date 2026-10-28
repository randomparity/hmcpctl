"""CLI/MCP preserve paging intent, policy and the library's independent facts."""

import asyncio
import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from lpar.test_paging_observation import (
    PRIMARY,
    SECONDARY,
    SYSTEM,
    TARGET,
    observe,
    paging_boundary,  # noqa: F401 - pytest fixture registration
)
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import (
    DEFAULT_CONNECTION_TOKEN,
    AccessPolicyError,
    compile_access_policy,
)
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import memory as cli
from hmcpctl.operations.lpar.paging import reduce_paging_attempt
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle as tools

NAME = "hmc_switch_lpar_paging_vios"
ARGS = {
    "lpar_name_or_uuid": TARGET,
    "system_name_or_uuid": SYSTEM,
    "expected_current_paging_vios_name_or_uuid": SECONDARY,
    "exclusive_writer_window": True,
}
COMMAND = [
    "lpars",
    "paging-switchover",
    TARGET,
    "--system",
    SYSTEM,
    "--expected-current-paging-vios",
    SECONDARY,
]


@pytest.fixture
def presentation(request, monkeypatch):
    boundary = request.getfixturevalue("paging_boundary")
    before = asyncio.run(observe(boundary))
    other = before.configured.primary
    after = replace(before, current_uom=other, current_native=other)
    result = reduce_paging_attempt(
        SYSTEM, before, after, before.current_uom, other, "unknown", None, ()
    )
    operation = AsyncMock(return_value=result)
    monkeypatch.setattr(cli, "switch_lpar_paging_vios", operation, raising=False)
    client = Mock(side_effect=lambda fn: asyncio.run(fn(boundary.hmc)))
    monkeypatch.setattr(cli, "with_client", client)
    printed = []
    monkeypatch.setattr(cli, "print_json", printed.append)
    return boundary, result, operation, client, printed


def test_cli_yes_not_window(presentation):
    assert callable(getattr(cli, "lpars_paging_switchover", None))
    outcome = CliRunner().invoke(app, COMMAND + ["--yes"])
    assert outcome.exit_code == 2
    presentation[3].assert_not_called()


@pytest.mark.parametrize(
    "outcome,exit_code",
    [("completed", 0), ("partial", 1), ("rejected", 1), ("unverified", 1)],
)
def test_cli_structured_result_and_exit(presentation, outcome, exit_code):
    assert callable(getattr(cli, "lpars_paging_switchover", None))
    boundary, result, operation, _client, printed = presentation
    result = replace(result, outcome=outcome, workflow_completed=outcome == "completed")
    operation.return_value = result
    reply = CliRunner().invoke(app, COMMAND + ["--exclusive-writer-window", "--yes"])
    assert reply.exit_code == exit_code, reply.output
    assert printed == [asdict(result)]
    operation.assert_awaited_once_with(
        boundary.hmc,
        TARGET,
        system_name_or_uuid=SYSTEM,
        expected_current_paging_vios_name_or_uuid=SECONDARY,
        exclusive_writer_window=True,
    )


def test_cli_confirmation_and_excluded_flags(presentation, monkeypatch):
    assert callable(getattr(cli, "lpars_paging_switchover", None))
    monkeypatch.setattr(cli.typer, "confirm", lambda _: False)
    assert (
        CliRunner().invoke(app, COMMAND + ["--exclusive-writer-window"]).exit_code == 0
    )
    for flag in [
        "--ownership-override",
        "--force",
        "--wait-minutes",
        "--detail-level",
        "--destination",
    ]:
        assert (
            CliRunner()
            .invoke(app, COMMAND + ["--exclusive-writer-window", "--yes", flag])
            .exit_code
            == 2
        )
    presentation[3].assert_not_called()


def policy(targets, effect=False):
    assert NAME in TOOL_SECURITY, "paging tool is not registered"
    return compile_access_policy(
        {
            "policies": {
                "paging": {
                    "grants": [
                        {
                            **(
                                {"effects": ["mutate"]} if effect else {"tools": [NAME]}
                            ),
                            "connections": [DEFAULT_CONNECTION_TOKEN],
                            "targets": targets,
                        }
                    ]
                }
            }
        },
        "paging",
        TOOL_SECURITY,
        "test-paging-policy.toml",
    )


def call(arguments, targets="all-targets", effect=False):
    application = create_mcp(policy(targets, effect))

    async def go():
        async with Client(application) as client:
            return await client.call_tool(NAME, arguments)

    return asyncio.run(go())


def test_mcp_security_covers_expected_provider():
    assert NAME in TOOL_SECURITY
    security = TOOL_SECURITY[NAME]
    assert (security.effect, security.operation, security.target_kind) == (
        "mutate",
        "lpar.switch_paging_vios",
        "lpar",
    )
    assert security.exhaustive_targets is False
    assert {(t.kind, t.argument, t.required) for t in security.targets} == {
        ("lpar", "lpar_name_or_uuid", True),
        ("managed_system", "system_name_or_uuid", True),
        ("vios", "expected_current_paging_vios_name_or_uuid", True),
    }


@pytest.mark.parametrize("window", [False, 1, "true", None])
def test_mcp_literal_window_before_client(monkeypatch, window):
    assert NAME in TOOL_SECURITY
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    client = Mock(side_effect=AssertionError("must not construct client"))
    monkeypatch.setattr(tools, "with_client", client)
    with pytest.raises(ToolError):
        call({**ARGS, "exclusive_writer_window": window})
    client.assert_not_called()


def test_scoped_table_cannot_grant_discovered_provider(monkeypatch):
    assert NAME in TOOL_SECURITY
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    client = Mock(side_effect=AssertionError("must not construct client"))
    monkeypatch.setattr(tools, "with_client", client)
    targets = {
        "managed_system": [SYSTEM],
        "lpar": [TARGET],
        "vios": [PRIMARY, SECONDARY],
    }
    with pytest.raises(AccessPolicyError, match="all-targets"):
        policy(targets)
    with pytest.raises(ToolError, match="all-targets"):
        call(ARGS, targets, effect=True)
    client.assert_not_called()


@pytest.mark.parametrize("outcome", ["completed", "partial", "unverified"])
def test_mcp_structured_axes_match_library(presentation, monkeypatch, outcome):
    assert NAME in TOOL_SECURITY
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    result = replace(
        presentation[1], outcome=outcome, workflow_completed=outcome == "completed"
    )
    client = Mock(return_value=result)
    monkeypatch.setattr(tools, "with_client", client)
    reply = call(ARGS)
    assert reply.structured_content == json.loads(json.dumps(asdict(result)))
    assert reply.structured_content["submission"] == "unknown"
    assert reply.structured_content["outcome"] == outcome
    client.assert_called_once()


def test_mcp_forwards_the_same_library_contract(presentation, monkeypatch):
    boundary, result, operation, _, _ = presentation
    monkeypatch.setattr(tools, "switch_lpar_paging_vios", operation)
    client = Mock(side_effect=lambda fn, **kwargs: asyncio.run(fn(boundary.hmc)))
    monkeypatch.setattr(tools, "with_client", client)
    assert tools.hmc_switch_lpar_paging_vios(**ARGS) == result
    operation.assert_awaited_once_with(
        boundary.hmc,
        TARGET,
        system_name_or_uuid=SYSTEM,
        expected_current_paging_vios_name_or_uuid=SECONDARY,
        exclusive_writer_window=True,
    )
    assert client.call_args.kwargs == {"profile": None}
