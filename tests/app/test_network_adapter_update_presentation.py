"""Actual CLI/MCP boundaries for one contained running Ethernet update."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.virtualization import adapters as cli
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.virtualization.adapters import (
    NetworkUpdatePreservation,
    NetworkUpdateResult,
    NetworkUpdateVerification,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.virtualization import adapters as tools

SYSTEM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LPAR = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ADAPTER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NAME = "hmc_update_network_adapter"
ARGS = {
    "lpar_name_or_uuid": LPAR,
    "adapter_uuid": ADAPTER,
    "system_name_or_uuid": SYSTEM,
    "exclusive_writer_window": True,
    "qos_priority": 0,
}
COMMAND = [
    "adapters",
    "update-network",
    LPAR,
    "--uuid",
    ADAPTER,
    "--system",
    SYSTEM,
    "--qos-priority",
    "0",
]


@pytest.fixture
def boundary(monkeypatch):
    result = NetworkUpdateResult(
        SYSTEM,
        LPAR,
        ADAPTER,
        4,
        True,
        "completed",
        None,
        None,
        NetworkUpdateVerification(
            "verified", {"qos_priority": 0}, {"qos_priority": 0}, (), ()
        ),
        NetworkUpdatePreservation("unchanged", "parsed adapter configuration", ()),
        ChangeLocation("On", "unknown"),
        (),
    )
    operation = AsyncMock(return_value=result)
    connections, printed = [], []

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(object()))

    for module in (cli, tools):
        monkeypatch.setattr(module, "update_network_adapter", operation, raising=False)
        monkeypatch.setattr(module, "with_client", run)
    monkeypatch.setattr(cli, "print_json", printed.append)
    return SimpleNamespace(
        result=result, operation=operation, connections=connections, printed=printed
    )


def policy(targets="all-targets", connections=None, grant=True):
    return compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [NAME] if grant else ["hmc_list_adapters"],
                            "connections": connections or ["<default>"],
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


def test_cli_requires_window_even_with_yes(boundary):
    result = CliRunner().invoke(app, COMMAND + ["--yes"])
    assert result.exit_code == 2
    assert not boundary.connections


@pytest.mark.parametrize(
    "tagged,expected", [([], None), (["--tagged"], True), (["--untagged"], False)]
)
def test_cli_preserves_tri_state_zero_and_result(boundary, tagged, expected):
    result = CliRunner().invoke(
        app, COMMAND + ["--exclusive-writer-window", "--yes", *tagged]
    )
    assert result.exit_code == 0, result.output
    assert boundary.operation.await_args.kwargs["tagged"] is expected
    assert boundary.operation.await_args.kwargs["qos_priority"] == 0
    assert boundary.printed == [asdict(boundary.result)]
    boundary.operation.assert_awaited_once()


@pytest.mark.parametrize(
    "options",
    [
        ["--addl-vlan-ids", "{"],
        ["--addl-vlan-ids", "{}"],
        ["--addl-vlan-ids", "[true]"],
        ["--addl-vlan-ids", "[4095]"],
        ["--addl-vlan-ids", "[]", "--vlan-action", "add"],
        ["--qos-priority", "8"],
        ["--qos-priority", "invalid"],
        ["--vsi-type-id", "2147483648"],
        ["--remove-vsi-profile", "--vsi-type-id", "0"],
    ],
)
def test_cli_bad_inputs_before_confirmation_and_client(boundary, options):
    result = CliRunner().invoke(app, COMMAND + ["--exclusive-writer-window", *options])
    assert result.exit_code == 2, result.output
    assert not boundary.connections
    assert "Update network adapter" not in result.output


@pytest.mark.parametrize(
    "submission,verification,preservation,dispatched,reason,code",
    [
        ("unknown", "verified", "unchanged", True, None, 1),
        ("rejected", "verified", "unchanged", True, None, 1),
        ("completed", "mismatched", "unchanged", True, None, 1),
        ("completed", "unavailable", "unchanged", True, None, 1),
        ("completed", "verified", "drifted", True, None, 1),
        ("completed", "verified", "unavailable", True, None, 1),
        ("not_dispatched", "verified", "unchanged", False, "already_satisfied", 0),
    ],
)
def test_cli_exit_requires_complete_success(
    boundary, submission, verification, preservation, dispatched, reason, code
):
    boundary.operation.return_value = replace(
        boundary.result,
        submission=submission,
        mutation_dispatched=dispatched,
        reason=reason,
        verification=replace(boundary.result.verification, status=verification),
        preservation=replace(boundary.result.preservation, status=preservation),
    )
    result = CliRunner().invoke(app, COMMAND + ["--exclusive-writer-window", "--yes"])
    assert result.exit_code == code, result.output
    assert boundary.printed == [asdict(boundary.operation.return_value)]
    boundary.operation.assert_awaited_once()


def test_cli_empty_remove_and_disabled_values_forwarded(boundary):
    result = CliRunner().invoke(
        app,
        COMMAND
        + [
            "--exclusive-writer-window",
            "--yes",
            "--addl-vlan-ids",
            "[]",
            "--qos-priority",
            "none",
            "--untagged",
            "--remove-vsi-profile",
        ],
    )
    assert result.exit_code == 0, result.output
    kwargs = boundary.operation.await_args.kwargs
    assert kwargs["addl_vlan_ids"] == [] and kwargs["qos_priority"] == "none"
    assert kwargs["tagged"] is False and kwargs["remove_vsi_profile"] is True


@pytest.mark.asyncio
async def test_mcp_result_and_contained_target_inventory(boundary):
    assert NAME in TOOL_SECURITY
    security = TOOL_SECURITY[NAME]
    assert security.effect == "mutate" and security.exhaustive_targets
    assert {(t.kind, t.argument, t.required) for t in security.targets} == {
        ("lpar", "lpar_name_or_uuid", True),
        ("managed_system", "system_name_or_uuid", True),
    }
    async with Client(create_mcp(policy())) as client:
        result = await client.call_tool(NAME, ARGS, raise_on_error=False)
    assert not result.is_error
    assert result.structured_content == json.loads(json.dumps(asdict(boundary.result)))
    assert boundary.operation.await_args.kwargs["qos_priority"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("tagged", 1),
        ("tagged", "false"),
        ("exclusive_writer_window", "true"),
        ("exclusive_writer_window", 1),
        ("exclusive_writer_window", False),
        ("qos_priority", True),
        ("qos_priority", "0"),
        ("addl_vlan_ids", [True]),
        ("vsi_manager_id", False),
        ("vsi_type_id", "1"),
        ("vsi_type_version", True),
        ("remove_vsi_profile", 1),
        ("ownership_override", "true"),
    ],
)
async def test_real_mcp_refuses_coercion_before_client(boundary, field, value):
    async with Client(create_mcp(policy())) as client:
        result = await client.call_tool(
            NAME, {**ARGS, field: value}, raise_on_error=False
        )
    assert result.is_error
    assert not boundary.connections


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["system", "lpar", "connection", "grant"])
async def test_real_mcp_policy_denial_precedes_client(boundary, monkeypatch, denial):
    monkeypatch.delenv("HMC_HOST", raising=False)
    targets = {"managed_system": [SYSTEM], "lpar": [LPAR]}
    if denial in ("system", "lpar"):
        targets["managed_system" if denial == "system" else "lpar"] = [ADAPTER]
    config = policy(
        targets, ["other"] if denial == "connection" else None, denial != "grant"
    )
    async with Client(create_mcp(config)) as client:
        result = await client.call_tool(NAME, ARGS, raise_on_error=False)
    assert result.is_error
    assert not boundary.connections


def test_cli_confirmation_refusal_and_terminal_encoding(boundary, monkeypatch):
    prompts = []
    original = cli.terminal_field

    def terminal(value):
        with monkeypatch.context() as patch:
            patch.setattr(sys.stdout, "isatty", lambda: True)
            return original(value)

    monkeypatch.setattr(cli, "terminal_field", terminal)
    monkeypatch.setattr(
        cli.typer, "confirm", lambda text: prompts.append(text) or False
    )
    command = [
        "adapters",
        "update-network",
        "name\x1b[31m",
        "--uuid",
        ADAPTER,
        "--system",
        SYSTEM,
        "--qos-priority",
        "0",
        "--exclusive-writer-window",
    ]
    result = CliRunner().invoke(app, command)
    assert result.exit_code != 0
    assert len(prompts) == 1 and "\x1b" not in prompts[0]
    assert not boundary.connections
