"""CLI/MCP artifact discovery and outcome forwarding."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer

from hmcpctl.cli_commands.lpar import profiles as cli
from hmcpctl.jobs import job_outcome
from hmcpctl.operations.lpar.profile_artifacts import (
    BackupProfileDeletion,
    BackupProfileInventory,
)
from hmcpctl.server_tools.lpar import profiles as tools

SYSTEM = "22222222-2222-4222-8222-222222222222"


def test_tool_contract_and_connection_forwarding(monkeypatch):
    hmc = MagicMock()
    listing = BackupProfileInventory(SYSTEM, job_outcome("job-1", None), ())
    operation = AsyncMock(return_value=listing)
    seen = []
    monkeypatch.setattr(tools, "list_backup_profile_artifacts", operation)
    monkeypatch.setattr(
        tools,
        "with_client",
        lambda fn, **kwargs: seen.append(kwargs) or asyncio.run(fn(hmc)),
    )
    assert tools.hmc_list_backup_lpar_profiles(SYSTEM, profile="test") == listing
    operation.assert_awaited_once_with(hmc, SYSTEM)
    assert seen == [{"profile": "test"}]
    assert tools.tool_security()["hmc_list_backup_lpar_profiles"].effect == "read"
    delete = tools.tool_security()["hmc_delete_backup_lpar_profile"]
    assert delete.effect == "destructive"
    assert delete.target_kind == "managed_system"
    assert delete.exhaustive_targets is False


def test_cli_inventory_unavailable_exits_nonzero_with_honest_json(monkeypatch):
    result = BackupProfileInventory(SYSTEM, job_outcome("job-1", None), None)
    printed = []
    monkeypatch.setattr(cli, "with_client", lambda fn: result)
    monkeypatch.setattr(cli, "print_json", printed.append)
    with pytest.raises(typer.Exit) as error:
        cli.lpars_list_profile_backups(SYSTEM)
    assert error.value.exit_code == 1
    assert printed == [asdict(result)]


@pytest.mark.parametrize(
    "postcondition,exit_code",
    [("absent", 0), ("present", 1), ("unknown", 1), ("not-checked", 1)],
)
def test_cli_delete_success_requires_verified_absence(
    monkeypatch, postcondition, exit_code
):
    result = BackupProfileDeletion(
        SYSTEM, "backup-1", job_outcome("job-1", None), postcondition
    )
    monkeypatch.setattr(cli, "with_client", lambda fn: result)
    printed = []
    monkeypatch.setattr(cli, "print_json", printed.append)
    if exit_code:
        with pytest.raises(typer.Exit) as error:
            cli.lpars_delete_profile_backup(SYSTEM, "backup-1")
        assert error.value.exit_code == exit_code
    else:
        cli.lpars_delete_profile_backup(SYSTEM, "backup-1")
    assert printed == [asdict(result)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["hmc_list_backup_lpar_profiles", "hmc_delete_backup_lpar_profile"]
)
async def test_mcp_serializes_bounded_artifact_result(tool_name, monkeypatch):
    from dataclasses import replace

    from fastmcp import Client

    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    job = replace(
        job_outcome(
            "job-1", {"Resource": {"JobID": "job-1", "Status": "COMPLETED_OK"}}
        ),
        job=None,
    )
    result = (
        BackupProfileInventory(SYSTEM, job, ())
        if "list" in tool_name
        else BackupProfileDeletion(SYSTEM, "backup-1", job, "absent")
    )
    monkeypatch.setattr(tools, "with_client", lambda operation, **kwargs: result)
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    arguments = {"system_name_or_uuid": SYSTEM}
    if "delete" in tool_name:
        arguments["file_name"] = "backup-1"
    async with Client(create_mcp(policy)) as client:
        response = await client.call_tool(tool_name, arguments)
    assert response.structured_content["system_uuid"] == SYSTEM
    assert response.structured_content["job"]["job"] is None
