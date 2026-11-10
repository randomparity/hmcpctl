"""Tests for LPAR processor compatibility mode tools (SSH CLI path)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import live_process_error, mock_uuid_resolution

from hmcpctl.server_tools.lpar.configuration import (
    hmc_get_lpar_proc_compat,
    hmc_set_lpar_proc_compat,
)
from hmcpctl.server_tools.systems.resources import (
    hmc_get_proc_compat_modes,
)
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN123456"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "test-lpar-01"


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a minimal asyncssh connection mock."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() resolves inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


# ---------------------------------------------------------------------- #
# hmc_get_proc_compat_modes
# ---------------------------------------------------------------------- #


def test_get_proc_compat_modes_runs_correct_command(monkeypatch, mock_hmc):
    """hmc_get_proc_compat_modes issues lssyscfg sys with correct arguments."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn_mock = _make_ssh_mock("default,POWER8,POWER9,POWER10\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_proc_compat_modes(SYSTEM_UUID)

    expected_cmd = f"lssyscfg -r sys -m {SYSTEM_NAME} -F lpar_proc_compat_modes"
    conn_mock.run.assert_awaited_with(expected_cmd, check=True, timeout=300.0)
    assert result == ["default", "POWER8", "POWER9", "POWER10"]


def test_get_proc_compat_modes_returns_empty_when_none(monkeypatch, mock_hmc):
    """hmc_get_proc_compat_modes handles empty outputs gracefully."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn_mock = _make_ssh_mock("\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_proc_compat_modes(SYSTEM_UUID)

    assert result == []


# ---------------------------------------------------------------------- #
# hmc_get_lpar_proc_compat
# ---------------------------------------------------------------------- #


def _make_scripted_ssh_mock(*outputs: str) -> MagicMock:
    """Return an asyncssh mock whose successive commands print *outputs*."""
    results = []
    for stdout in outputs:
        result = MagicMock()
        result.stdout = stdout
        results.append(result)
    conn = AsyncMock()
    conn.run = AsyncMock(side_effect=results)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _commands(conn_mock: MagicMock) -> list[str]:
    return [call.args[0] for call in conn_mock.run.await_args_list]


def test_get_lpar_proc_compat_reads_lpar_and_default_profile(monkeypatch, mock_hmc):
    """The read reports the partition modes and the default profile's mode."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_scripted_ssh_mock(
        "default,POWER9_base,default_profile\n", "POWER8\n"
    )

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID
        )

    assert _commands(conn_mock) == [
        (
            f"lssyscfg -r lpar -m {SYSTEM_NAME} --filter lpar_names={LPAR_NAME} "
            "-F desired_lpar_proc_compat_mode,curr_lpar_proc_compat_mode,default_profile"
        ),
        (
            f"lssyscfg -r prof -m {SYSTEM_NAME} "
            f"--filter lpar_names={LPAR_NAME},profile_names=default_profile "
            "-F lpar_proc_compat_mode"
        ),
    ]
    assert result == {
        "desired": "default",
        "curr": "POWER9_base",
        "profile": "default_profile",
        "profile_mode": "POWER8",
    }


def test_get_lpar_proc_compat_reads_the_named_profile(monkeypatch, mock_hmc):
    """profile_name selects the profile whose mode is reported."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_scripted_ssh_mock(
        "default,default,default_profile\n", "default\n"
    )

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID,
            lpar_name_or_uuid=LPAR_UUID,
            profile_name="test_profile",
        )

    assert "profile_names=test_profile" in _commands(conn_mock)[1]
    assert result["profile"] == "test_profile"
    assert result["profile_mode"] == "default"


def test_get_lpar_proc_compat_reports_unknown_partition(monkeypatch, mock_hmc):
    """An unknown partition exits 1 with HSCL8012 and no profile read follows."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock()
    conn_mock.run.side_effect = live_process_error("cli-lpar-unknown")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="HSCL8012"),
    ):
        hmc_get_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID
        )

    conn_mock.run.assert_awaited_once()


# ---------------------------------------------------------------------- #
# hmc_set_lpar_proc_compat
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["POWER9", "POWER9_base", "POWER9_Base"])
def test_set_lpar_proc_compat_writes_the_default_profile(monkeypatch, mock_hmc, mode):
    """With no profile_name the mode is written to the partition's default profile."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_scripted_ssh_mock("", "default_profile\n", "")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_set_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID, mode=mode
        )

    assert _commands(conn_mock)[1:] == [
        (
            f"lssyscfg -r lpar -m {SYSTEM_NAME} --filter lpar_names={LPAR_NAME} "
            "-F default_profile"
        ),
        (
            f"chsyscfg -r prof -m {SYSTEM_NAME} "
            f"-i name=default_profile,lpar_name={LPAR_NAME},lpar_proc_compat_mode={mode}"
        ),
    ]
    assert result == (
        f"Set lpar_proc_compat_mode={mode} on profile default_profile of {LPAR_NAME}"
    )


def test_set_lpar_proc_compat_writes_the_named_profile(monkeypatch, mock_hmc):
    """profile_name skips the default lookup and is the profile written."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_scripted_ssh_mock("", "")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_set_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID,
            lpar_name_or_uuid=LPAR_UUID,
            mode="POWER9",
            profile_name="test_profile",
        )

    assert _commands(conn_mock)[1:] == [
        (
            f"chsyscfg -r prof -m {SYSTEM_NAME} "
            f"-i name=test_profile,lpar_name={LPAR_NAME},lpar_proc_compat_mode=POWER9"
        ),
    ]
    assert "profile test_profile" in result


def test_set_lpar_proc_compat_refuses_a_partition_without_a_default_profile(
    monkeypatch, mock_hmc
):
    """An empty default_profile is reported, not written as an empty name."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_scripted_ssh_mock("", "\n")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="no default profile"),
    ):
        hmc_set_lpar_proc_compat(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID, mode="POWER9"
        )

    assert conn_mock.run.await_count == 2  # ownership read, default lookup; no write
