"""Tests for LPAR MSP flag get/set tools (SSH CLI path)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from conftest import (
    SYSTEM_ENTRY,
    live_process_error,
    mock_system_summary,
    mock_uuid_resolution,
)

from hmcpctl.config import HMCConfig
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.server_tools.lpar.configuration import (
    hmc_get_lpar_msp,
    hmc_set_lpar_msp,
)
from hmcpctl.ssh.profiles import HMCCLIError, set_lpar_msp

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN123456"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "test-lpar-01"
VIOS_UUID = "33333333-3333-4333-8333-333333333333"
VIOS_NAME = "vios-1"


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
# hmc_get_lpar_msp
# ---------------------------------------------------------------------- #


def test_get_lpar_msp_runs_correct_command(monkeypatch, mock_hmc):
    """hmc_get_lpar_msp issues lssyscfg with the correct arguments."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock("1\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID
        )

    expected_cmd = (
        f"lssyscfg -r lpar -m {SYSTEM_NAME} --filter lpar_names={LPAR_NAME} -F msp"
    )
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert result is True


def test_get_lpar_msp_returns_true_when_enabled(monkeypatch, mock_hmc):
    """hmc_get_lpar_msp returns True when the HMC reports msp=1."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock("1\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID
        )

    assert result is True


def test_get_lpar_msp_returns_false_when_disabled(monkeypatch, mock_hmc):
    """hmc_get_lpar_msp returns False when the HMC reports msp=0."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock("0\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID
        )

    assert result is False


@pytest.mark.parametrize("raw", ["", "2\n", "enabled\n"])
def test_get_lpar_msp_rejects_unexpected_output(monkeypatch, mock_hmc, raw):
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock(raw)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="expected '0' or '1'"),
    ):
        hmc_get_lpar_msp(system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=LPAR_UUID)


# ---------------------------------------------------------------------- #
# hmc_set_lpar_msp
# ---------------------------------------------------------------------- #


def _make_ssh_mock_seq(*stdouts: str) -> MagicMock:
    """Return an asyncssh connection mock that returns *stdouts* in sequence.

    Each positional argument is the stdout of one successive conn.run() call.
    Use when set_lpar_msp makes two SSH round-trips (lpar_env check + chsyscfg).
    """
    results = []
    for stdout in stdouts:
        r = MagicMock()
        r.stdout = stdout
        results.append(r)

    conn = AsyncMock()
    conn.run = AsyncMock(side_effect=results)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _mock_vios_resolution(router, vios_uuid: str = VIOS_UUID) -> None:
    """Route the system GET and its VirtualIOServer feed holding one VIOS."""
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name=SYSTEM_NAME)
        )
    )
    mock_system_summary(router, SYSTEM_UUID, SYSTEM_NAME)
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/VirtualIOServer").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                f"<id>urn:uuid:{vios_uuid}</id>"
                '<content type="application/vnd.ibm.powervm.uom+xml">'
                '<VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
                f"<PartitionName>{VIOS_NAME}</PartitionName>"
                "</VirtualIOServer></content></entry></feed>"
            ),
        )
    )


def test_set_lpar_msp_by_vios_name_runs_correct_command(monkeypatch, mock_hmc):
    """A VIOS named on the selected system resolves through its VIOS feed (#1318)."""
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    conn_mock = _make_ssh_mock_seq("", "vioserver\n", "")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=VIOS_NAME, enabled=True
        )

    assert conn_mock.run.call_count == 3
    description_cmd, env_cmd, chsyscfg_cmd = (
        call[0][0] for call in conn_mock.run.call_args_list
    )
    assert description_cmd == (
        f"lssyscfg -r lpar -m {SYSTEM_NAME} --filter lpar_names={VIOS_NAME} "
        "-F description"
    )
    assert "lpar_env" in env_cmd
    assert (
        chsyscfg_cmd == f"chsyscfg -r lpar -m {SYSTEM_NAME} -i name={VIOS_NAME},msp=1"
    )
    assert result == ""


def test_set_lpar_msp_by_vios_uuid_matches_case_insensitively(monkeypatch, mock_hmc):
    """A VIOS UUID hosted by the selected system resolves to its partition name."""
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    conn_mock = _make_ssh_mock_seq("", "vioserver\n", "")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID,
            lpar_name_or_uuid=VIOS_UUID.upper(),
            enabled=False,
        )

    chsyscfg_cmd = conn_mock.run.call_args_list[2][0][0]
    assert (
        chsyscfg_cmd == f"chsyscfg -r lpar -m {SYSTEM_NAME} -i name={VIOS_NAME},msp=0"
    )


def test_set_lpar_msp_returns_cli_output(monkeypatch, mock_hmc):
    """hmc_set_lpar_msp returns the raw SSH stdout verbatim."""
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    RAW_OUTPUT = "0 objects successfully changed.\n"
    conn_mock = _make_ssh_mock_seq("", "vioserver\n", RAW_OUTPUT)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=VIOS_UUID, enabled=True
        )

    assert result == RAW_OUTPUT


@pytest.mark.parametrize(
    "selector",
    [
        LPAR_NAME,  # an AIX/Linux partition: absent from the VIOS feed
        LPAR_UUID,  # a partition UUID the selected system's VIOS feed does not host
    ],
)
def test_set_lpar_msp_refuses_non_vios_before_ssh(monkeypatch, mock_hmc, selector):
    """A selector outside the system's VIOS feed is refused before any SSH command.

    The live non-VIOS refusal check matches ``vioserver`` in this error.
    """
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    connect = MagicMock()

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", new=connect),
        pytest.raises(ResourceNotFoundError, match="lpar_env=vioserver"),
    ):
        hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=selector, enabled=True
        )

    connect.assert_not_called()


def test_set_lpar_msp_keeps_ssh_lpar_env_guard(monkeypatch, mock_hmc):
    """The SSH ``lpar_env`` probe still refuses a non-VIOS answer before chsyscfg.

    The HMC CLI returns 'aixlinux' for both AIX and Linux partitions.
    """
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    conn_mock = _make_ssh_mock_seq("", "aixlinux\n")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="only valid for a VIOS"),
    ):
        hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=VIOS_NAME, enabled=True
        )

    assert conn_mock.run.call_count == 2


# ---------------------------------------------------------------------- #
# set_lpar_msp (ssh-layer) — inner lpar_env guard
# ---------------------------------------------------------------------- #


def test_set_lpar_msp_rejects_partition_not_found(monkeypatch, mock_hmc):
    """An unknown partition exits 1 with HSCL8012, and no chsyscfg follows."""
    _hmc_env(monkeypatch)
    _mock_vios_resolution(mock_hmc)
    conn_mock = _make_ssh_mock()
    conn_mock.run.side_effect = [
        conn_mock.run.return_value,
        live_process_error("cli-lpar-unknown"),
    ]

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="HSCL8012 The partition named .* not found"),
    ):
        hmc_set_lpar_msp(
            system_name_or_uuid=SYSTEM_UUID, lpar_name_or_uuid=VIOS_UUID, enabled=True
        )

    assert conn_mock.run.call_count == 2


def test_set_lpar_msp_ssh_layer_rejects_non_vios():
    """set_lpar_msp (ssh) raises HMCCLIError for non-VIOS — inner guard."""
    cfg = HMCConfig(host="hmc.test", user="hscroot", password="abc123")
    env_result = MagicMock()
    env_result.stdout = "aixlinux\n"
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=env_result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="only valid for a VIOS"),
    ):
        asyncio.run(set_lpar_msp(cfg, "sys", "lpar", True))
