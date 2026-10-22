"""Saved-profile CLI exit codes preserve execution/readback facts."""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from typer.testing import CliRunner

from hmcpctl.cli_commands.lpar import profiles as cli
from hmcpctl.operations.lpar.profiles import (
    PartitionProfileInventory,
    PartitionProfileOutcome,
)


@pytest.mark.parametrize(
    "execution,requested,preservation,code",
    [
        ("completed", "verified", "verified", 0),
        ("completed", "verified", "drifted", 2),
        ("completed", "unverified", "unverified", 2),
        ("unknown", "verified", "drifted", 2),
        ("rejected", "unverified", "unverified", 1),
    ],
)
def test_cli_update_exit_codes_and_json_keep_three_facts(
    monkeypatch, execution, requested, preservation, code
):
    handler = getattr(cli, "lpars_update_partition_profile", None)
    assert callable(handler)
    result = PartitionProfileOutcome(
        "update",
        "system",
        "partition",
        "LogicalPartition",
        "saved",
        "profile-id",
        execution=execution,
        requested_change=requested,
        preservation=preservation,
        comparison_complete=True,
    )
    printed = []
    monkeypatch.setattr(cli, "with_client", lambda fn: result)
    monkeypatch.setattr(cli, "print_json", printed.append)
    if code:
        with pytest.raises(typer.Exit) as exc:
            handler(
                "system",
                "partition",
                "saved",
                "etag",
                '{"desired_memory":1024}',
                exclusive_writer_window=True,
            )
        assert exc.value.exit_code == code
    else:
        handler(
            "system",
            "partition",
            "saved",
            "etag",
            '{"desired_memory":1024}',
            exclusive_writer_window=True,
        )
    assert printed == [asdict(result)]


@pytest.mark.parametrize("vios", [False, True])
def test_cli_list_selects_correct_parent_kind(monkeypatch, vios):
    handler = getattr(cli, "lpars_list_partition_profiles", None)
    assert callable(handler)
    operation = AsyncMock(
        return_value=PartitionProfileInventory(
            "system", "partition", "LogicalPartition", ()
        )
    )
    hmc = MagicMock()
    monkeypatch.setattr(cli, "list_partition_profiles", operation)
    monkeypatch.setattr(cli, "with_client", lambda fn: asyncio.run(fn(hmc)))
    monkeypatch.setattr(cli, "print_json", lambda value: None)
    handler("system", "partition", vios=vios)
    assert operation.call_args.kwargs["partition_type"] == (
        "VirtualIOServer" if vios else "LogicalPartition"
    )


def test_all_six_profile_commands_are_registered_and_typed_patch_rejects_unknown_input(
    monkeypatch,
):
    from hmcpctl.cli import app

    runner = CliRunner()
    for name in [
        "list-profiles",
        "read-profile",
        "update-profile",
        "save-current-profile",
        "set-default-profile",
        "delete-profile",
    ]:
        result = runner.invoke(app, ["lpars", name, "--help"])
        assert result.exit_code == 0, result.output
        assert "--vios" in result.output
    monkeypatch.setattr(
        cli,
        "with_client",
        lambda fn: pytest.fail("invalid patch must not reach an HMC"),
    )
    result = runner.invoke(
        app,
        [
            "lpars",
            "update-profile",
            "system",
            "partition",
            "saved",
            "--expected-etag",
            "etag",
            "--patch",
            '{"unknown":1}',
        ],
    )
    assert result.exit_code != 0
