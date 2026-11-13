"""Persistent-memory volume MCP tools and CLI commands (#643, ADR 0373)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from hmcpctl import _app, cli
from hmcpctl.cli_commands.lpar import memory as cli_memory
from hmcpctl.documents.persistent_memory import PersistentMemoryVolume
from hmcpctl.operations.lpar.memory.persistent import PersistentMemoryVolumeResult
from hmcpctl.server_tools.catalog import TOOL_SECURITY
from hmcpctl.server_tools.lpar import pmem

HMC = object()
RUNNER = CliRunner()
VOLUME = PersistentMemoryVolume(
    uuid="00000004-ABCD-4EF0-8ABC-000000000004",
    name="example-vol",
    size_mib=256,
    current_size_mib=256,
    volume_id=1,
    affinity=False,
    partition_id=1,
    partition_name="example-lpar",
    partition_uuid="00000005-ABCD-4EF0-8ABC-000000000005",
)


def _result(status: str = "verified") -> PersistentMemoryVolumeResult:
    return PersistentMemoryVolumeResult(status, {"name": "example-vol"}, VOLUME, ())


def _run(fn, **_kwargs):
    return asyncio.run(fn(HMC))


@pytest.mark.parametrize(
    ("tool", "effect", "operation", "target_kind"),
    [
        (
            "hmc_list_persistent_memory_volumes",
            "read",
            "pmem.list_volumes",
            "managed_system",
        ),
        (
            "hmc_get_persistent_memory_volume",
            "read",
            "pmem.get_volume",
            "managed_system",
        ),
        ("hmc_create_persistent_memory_volume", "mutate", "pmem.create_volume", "lpar"),
        ("hmc_rename_persistent_memory_volume", "mutate", "pmem.rename_volume", "lpar"),
        (
            "hmc_delete_persistent_memory_volume",
            "destructive",
            "pmem.delete_volume",
            "lpar",
        ),
    ],
)
def test_tool_security(tool, effect, operation, target_kind):
    security = TOOL_SECURITY[tool]
    assert (security.effect, security.operation, security.target_kind) == (
        effect,
        operation,
        target_kind,
    )


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(pmem, "with_client", _run)
    monkeypatch.setattr(_app, "with_client", _run)
    calls = {
        name: AsyncMock(return_value=value)
        for name, value in (
            ("list_persistent_memory_volumes", [VOLUME, VOLUME]),
            ("get_persistent_memory_volume", VOLUME),
            ("create_persistent_memory_volume", _result()),
            ("rename_persistent_memory_volume", _result()),
            ("delete_persistent_memory_volume", _result()),
        )
    }
    for name, mock in calls.items():
        monkeypatch.setattr(pmem, name, mock)
    return calls


def test_list_tool_forwards_partition_and_applies_limit(tools):
    assert pmem.hmc_list_persistent_memory_volumes(
        "example-lpar", limit=1, system_name_or_uuid="sys-R1"
    ) == [VOLUME]
    tools["list_persistent_memory_volumes"].assert_awaited_once_with(
        HMC, "sys-R1", "example-lpar"
    )


def test_mutating_tools_forward_every_argument(tools):
    pmem.hmc_get_persistent_memory_volume("sys-R1", "example-vol")
    pmem.hmc_create_persistent_memory_volume(
        "example-lpar", "example-vol", 512, affinity=True, system_name_or_uuid="sys-R1"
    )
    pmem.hmc_rename_persistent_memory_volume(
        "example-lpar",
        "example-vol",
        "renamed",
        ownership_override=True,
        system_name_or_uuid="sys-R1",
    )
    pmem.hmc_delete_persistent_memory_volume(
        "example-lpar",
        "example-vol",
        acknowledge_data_loss=True,
        system_name_or_uuid="sys-R1",
    )
    tools["get_persistent_memory_volume"].assert_awaited_once_with(
        HMC, "sys-R1", "example-vol"
    )
    tools["create_persistent_memory_volume"].assert_awaited_once_with(
        HMC,
        "example-lpar",
        "example-vol",
        512,
        system_name_or_uuid="sys-R1",
        affinity=True,
        ownership_override=False,
    )
    tools["rename_persistent_memory_volume"].assert_awaited_once_with(
        HMC,
        "example-lpar",
        "example-vol",
        "renamed",
        system_name_or_uuid="sys-R1",
        ownership_override=True,
    )
    tools["delete_persistent_memory_volume"].assert_awaited_once_with(
        HMC,
        "example-lpar",
        "example-vol",
        system_name_or_uuid="sys-R1",
        acknowledge_data_loss=True,
        ownership_override=False,
    )


@pytest.fixture
def commands(monkeypatch):
    monkeypatch.setattr(cli_memory, "with_client", _run)
    calls = {
        "list": AsyncMock(return_value=[VOLUME]),
        "get": AsyncMock(return_value=VOLUME),
        "create": AsyncMock(return_value=_result()),
        "rename": AsyncMock(return_value=_result("mismatch")),
        "delete": AsyncMock(return_value=_result()),
    }
    for verb, mock in calls.items():
        noun = "volumes" if verb == "list" else "volume"
        monkeypatch.setattr(cli_memory, f"{verb}_persistent_memory_{noun}", mock)
    return calls


def test_cli_reads_print_volumes(commands):
    listed = RUNNER.invoke(cli.app, ["lpars", "pmem-list", "sys-R1", "--lpar", "lp"])
    shown = RUNNER.invoke(cli.app, ["lpars", "pmem-show", "sys-R1", "example-vol"])
    assert (listed.exit_code, shown.exit_code) == (0, 0), listed.output + shown.output
    assert "example-vol" in listed.stdout and "example-vol" in shown.stdout
    commands["list"].assert_awaited_once_with(HMC, "sys-R1", "lp")
    commands["get"].assert_awaited_once_with(HMC, "sys-R1", "example-vol")


def test_cli_create_forwards_and_succeeds_on_verified(commands):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "pmem-create", "lp", "example-vol", "-s", "sys-R1"]
        + ["--size-mib", "256", "--affinity", "--yes"],
    )
    assert result.exit_code == 0, result.output
    commands["create"].assert_awaited_once_with(
        HMC,
        "lp",
        "example-vol",
        256,
        system_name_or_uuid="sys-R1",
        affinity=True,
        ownership_override=False,
    )


def test_cli_rename_exits_1_when_not_verified(commands):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "pmem-rename", "lp", "example-vol", "renamed", "-s", "sys-R1", "-y"],
    )
    assert result.exit_code == 1
    assert "mismatch" in result.stdout


def test_cli_delete_requires_acknowledgement_before_anything(commands):
    result = RUNNER.invoke(
        cli.app, ["lpars", "pmem-delete", "lp", "example-vol", "-s", "sys-R1", "-y"]
    )
    assert result.exit_code == 2
    commands["delete"].assert_not_awaited()


def test_cli_delete_declined_confirmation_does_not_delete(commands):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "pmem-delete", "lp", "example-vol", "-s", "sys-R1"]
        + ["--acknowledge-data-loss"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    commands["delete"].assert_not_awaited()


def test_cli_delete_forwards_acknowledgement(commands):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "pmem-delete", "lp", "example-vol", "-s", "sys-R1"]
        + ["--acknowledge-data-loss", "--yes"],
    )
    assert result.exit_code == 0, result.output
    commands["delete"].assert_awaited_once_with(
        HMC,
        "lp",
        "example-vol",
        system_name_or_uuid="sys-R1",
        acknowledge_data_loss=True,
        ownership_override=False,
    )
