"""Tests for LPAR profile backup / restore / sync / I/O slot assignment tools (SSH CLI path)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_config, mock_uuid_resolution
from fastmcp import Client

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.operations.lpar.configuration import synchronize_lpar_profile
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar.profiles import (
    hmc_backup_lpar_profiles,
    hmc_restore_lpar_profiles,
    hmc_sync_lpar_profile,
)
from hmcpctl.ssh.profiles import restore_lpar_profiles, sync_lpar_profile

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "managed_sys1"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "lpar1"
PROFILE_NAME = "profile1"
DRC_INDEX = "10000000"


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a minimal asyncssh connection mock."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _hmc_env(monkeypatch):
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


# ---------------------------------------------------------------------- #
# hmc_backup_lpar_profiles
# ---------------------------------------------------------------------- #


def test_backup_lpar_profiles_runs_correct_command(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles issues bkprofdata with correct system and file path."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    BACKUP_OUTPUT = "Backup operation completed successfully.\n"
    conn_mock = _make_ssh_mock(BACKUP_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_backup_lpar_profiles(SYSTEM_UUID, "/tmp/lpar_profiles.bak")

    expected_cmd = f"bkprofdata -m {SYSTEM_NAME} -f /tmp/lpar_profiles.bak"
    conn_mock.run.assert_awaited_with(expected_cmd, check=True, timeout=300.0)
    assert "completed successfully" in result


def test_backup_lpar_profiles_returns_cli_output(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles returns the raw SSH stdout verbatim."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    RAW_OUTPUT = "Operation: backup\nStatus: OK\nFile: /tmp/profiles\n"
    conn_mock = _make_ssh_mock(RAW_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_backup_lpar_profiles(SYSTEM_UUID, "/tmp/profiles")

    assert result == RAW_OUTPUT


def test_backup_lpar_profiles_force_flag_appended(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles with force=True appends --force to bkprofdata."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    BACKUP_OUTPUT = "Backup operation completed successfully.\n"
    conn_mock = _make_ssh_mock(BACKUP_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_backup_lpar_profiles(
            SYSTEM_UUID, "/tmp/lpar_profiles.bak", force=True
        )

    expected_cmd = f"bkprofdata -m {SYSTEM_NAME} -f /tmp/lpar_profiles.bak --force"
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert "completed successfully" in result


def test_backup_lpar_profiles_no_force_by_default(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles default force=False does not add --force to bkprofdata."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn_mock = _make_ssh_mock("OK\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_backup_lpar_profiles(SYSTEM_UUID, "/tmp/profiles")

    called_cmd = conn_mock.run.call_args[0][0]
    assert "--force" not in called_cmd


def test_backup_lpar_profiles_empty_file_path_raises(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles raises ValueError for empty file_path."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)

    with pytest.raises(ValueError, match="file_path must not be empty"):
        hmc_backup_lpar_profiles(SYSTEM_UUID, "")


def test_backup_lpar_profiles_whitespace_file_path_raises(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles raises ValueError for whitespace-only file_path."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)

    with pytest.raises(ValueError, match="file_path must not be empty"):
        hmc_backup_lpar_profiles(SYSTEM_UUID, "   ")


# ---------------------------------------------------------------------- #
# hmc_restore_lpar_profiles
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("restore_type", [1, 2, 3])
def test_restore_lpar_profiles_runs_correct_command(
    monkeypatch, mock_hmc, restore_type
):
    """rstprofdata carries the mandatory -l restore type (rstprofdata.md:17,31)."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    RESTORE_OUTPUT = "Restore operation completed successfully.\n"
    conn_mock = _make_ssh_mock(RESTORE_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_restore_lpar_profiles(
            SYSTEM_UUID,
            "/tmp/lpar_profiles.bak",
            system_wide_restore_approved=True,
            ownership_override=True,
            restore_type=restore_type,
        )

    expected_cmd = (
        f"rstprofdata -m {SYSTEM_NAME} -l {restore_type} -f /tmp/lpar_profiles.bak"
    )
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert "completed successfully" in result


@pytest.mark.parametrize("restore_type", [0, 4, 5, True, "1", None])
def test_restore_lpar_profiles_refuses_other_restore_types(restore_type):
    """Only types 1-3 are sent; 4 initializes (deletes) every partition."""
    run = AsyncMock()
    with (
        patch("hmcpctl.ssh.profiles.run_hmc_command", run),
        pytest.raises(ValueError, match="restore_type must be 1, 2 or 3"),
    ):
        asyncio.run(
            restore_lpar_profiles(make_config(), "sys", "/tmp/p.bak", restore_type)
        )
    run.assert_not_awaited()


def test_restore_lpar_profiles_returns_cli_output(monkeypatch, mock_hmc):
    """hmc_restore_lpar_profiles returns the raw SSH stdout verbatim."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    RAW_OUTPUT = "Operation: restore\nStatus: OK\nFile: /tmp/profiles.bak\n"
    conn_mock = _make_ssh_mock(RAW_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_restore_lpar_profiles(
            SYSTEM_UUID,
            "/tmp/profiles.bak",
            system_wide_restore_approved=True,
            ownership_override=True,
            restore_type=1,
        )

    assert result == RAW_OUTPUT


def test_restore_lpar_profiles_requires_system_wide_approval(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)

    with pytest.raises(PermissionError, match="overwrites every profile"):
        hmc_restore_lpar_profiles(SYSTEM_UUID, "/tmp/profiles.bak", restore_type=1)

    assert not mock_hmc.calls


# ---------------------------------------------------------------------- #
# hmc_sync_lpar_profile
# ---------------------------------------------------------------------- #


def test_sync_lpar_profile_runs_correct_command(monkeypatch, mock_hmc):
    """hmc_sync_lpar_profile issues chsyscfg with correct sync parameters."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    SYNC_OUTPUT = "Profile sync completed successfully.\n"
    conn_mock = _make_ssh_mock(SYNC_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_sync_lpar_profile(SYSTEM_UUID, LPAR_UUID)

    expected_cmd = (
        f"chsyscfg -r lpar -m {SYSTEM_NAME} -i name={LPAR_NAME},sync_curr_profile=1"
    )
    conn_mock.run.assert_awaited_with(expected_cmd, check=True, timeout=300.0)
    assert "successfully" in result


def test_sync_lpar_profile_returns_cli_output(monkeypatch, mock_hmc):
    """hmc_sync_lpar_profile returns the raw SSH stdout verbatim."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    RAW_OUTPUT = "Operation: sync\nStatus: OK\nLPAR: lpar1\n"
    conn_mock = _make_ssh_mock(RAW_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_sync_lpar_profile(SYSTEM_UUID, LPAR_UUID)

    assert result == RAW_OUTPUT


@pytest.mark.parametrize(
    ("mode", "value"), [("enable", 1), ("disable", 0), ("suspend", 2)]
)
def test_sync_mode_renders_setting_value(monkeypatch, mock_hmc, mode, value):
    """Each mode writes the matching sync_curr_profile setting value (ADR 0201)."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_sync_lpar_profile(SYSTEM_UUID, LPAR_UUID, mode=mode)

    expected_cmd = (
        f"chsyscfg -r lpar -m {SYSTEM_NAME} "
        f"-i name={LPAR_NAME},sync_curr_profile={value}"
    )
    conn_mock.run.assert_awaited_with(expected_cmd, check=True, timeout=300.0)


# Deliberately outside ProfileSyncMode; typed Any so the call type-checks.
_BAD_MODE: Any = "on"


def test_sync_rejects_unknown_mode():
    """An unknown mode is refused before any SSH connection is opened."""
    with (
        patch("hmcpctl.ssh.profiles.run_hmc_command", new=AsyncMock()) as run,
        pytest.raises(ValueError, match="enable, disable or suspend"),
    ):
        asyncio.run(sync_lpar_profile(make_config(), SYSTEM_NAME, LPAR_NAME, _BAD_MODE))

    run.assert_not_awaited()


def test_synchronize_refuses_an_unknown_mode_before_any_hmc_call():
    """The operation validates the mode before resolving or authorizing anything."""
    hmc = MagicMock(side_effect=AssertionError("the HMC was contacted"))

    with (
        patch(
            "hmcpctl.operations.lpar.configuration.resolve_and_authorize_lpar_names",
            new=AsyncMock(side_effect=AssertionError("names were resolved")),
        ),
        pytest.raises(ValueError, match="enable, disable or suspend"),
    ):
        asyncio.run(
            synchronize_lpar_profile(
                hmc, LPAR_NAME, mode=_BAD_MODE, system_name_or_uuid=SYSTEM_NAME
            )
        )


def test_sync_tool_schema_offers_only_the_three_modes():
    """MCP callers see the three documented modes with enable as the default."""
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def schema():
        async with Client(create_mcp(policy)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return tools["hmc_sync_lpar_profile"].input_schema

    mode = asyncio.run(schema())["properties"]["mode"]
    assert mode["enum"] == ["enable", "disable", "suspend"]
    assert mode["default"] == "enable"


def test_restore_tool_schema_requires_documented_restore_type():
    """MCP callers must choose a restore type; the schema offers only 1-3."""
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def schema():
        async with Client(create_mcp(policy)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return tools["hmc_restore_lpar_profiles"].input_schema

    parameters = asyncio.run(schema())
    assert "restore_type" in parameters["required"]
    assert parameters["properties"]["restore_type"]["enum"] == [1, 2, 3]


def test_restore_tool_description_warns_merge_unconfigures_partitions():
    """MCP callers learn the type-3 resource_config reset and the re-apply command (#1322)."""
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def description():
        async with Client(create_mcp(policy)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return tools["hmc_restore_lpar_profiles"].description

    text = asyncio.run(description())
    assert "resource_config" in text
    assert "chsyscfg -r lpar -m <system> -o apply -p <lpar> -n <profile>" in text


def test_restore_type_4_refusal_names_what_it_would_do():
    """A library caller passing 4 is told it initializes and deletes every partition."""
    run = AsyncMock()
    with (
        patch("hmcpctl.ssh.profiles.run_hmc_command", run),
        pytest.raises(ValueError, match=r"Type 4 \(initialize, which deletes every"),
    ):
        asyncio.run(restore_lpar_profiles(make_config(), "sys", "/tmp/p.bak", 4))
    run.assert_not_awaited()
