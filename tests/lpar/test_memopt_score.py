"""Tests for LPAR memory-optimization score access (lsmemopt over SSH)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from conftest import live_fixture, mock_uuid_resolution

from hmcpctl.config import HMCConfig
from hmcpctl.server_tools.lpar.configuration import (
    hmc_get_lpar_memopt_score,
    hmc_list_lpar_memopt_scores,
)
from hmcpctl.ssh.affinity import (
    HMCCLIError,
    get_lpar_memopt_score,
    list_lpar_memopt_scores,
)

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"

SYSTEM_NAME = "p9da10"
LPAR_NAME = "p9da10v1t"
SCORE_ROW = "lpar_name=p9da10v1t,lpar_id=1,curr_lpar_score=100"
NONE_ROW = "lpar_name=dalpar2rrd1t,lpar_id=17,curr_lpar_score=none"
SECOND_ROW = "lpar_name=dapurea1t,lpar_id=19,curr_lpar_score=100"
MULTI_ROW = f"{SCORE_ROW}\n{SECOND_ROW}\n"


def _config() -> HMCConfig:
    return HMCConfig(
        host="hmc.test",
        user="hscroot",
        password="abc123",  # pragma: allowlist secret
    )


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a minimal asyncssh connection mock."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _not_found_error(stderr: str) -> asyncssh.ProcessError:
    """Build the non-zero-exit outcome the HMC CLI produces for a bad name."""
    return asyncssh.ProcessError(
        env={},
        command="lsmemopt",
        subsystem=None,
        exit_status=1,
        exit_signal=None,
        returncode=1,
        stdout="",
        stderr=stderr,
    )


# ---------------------------------------------------------------------- #
# get_lpar_memopt_score (ssh layer)
# ---------------------------------------------------------------------- #


def test_get_lpar_memopt_score_runs_correct_command():
    """get_lpar_memopt_score issues lsmemopt with the single-LPAR filter."""
    cfg = _config()
    conn = _make_ssh_mock(SCORE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, LPAR_NAME))

    expected_cmd = (
        f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore "
        f"--filter lpar_names={LPAR_NAME}"
    )
    conn.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert result == {
        "lpar_name": "p9da10v1t",
        "lpar_id": "1",
        "curr_lpar_score": "100",
    }


def test_get_lpar_memopt_score_preserves_none_score():
    """The literal HMC score 'none' is passed through unchanged."""
    cfg = _config()
    conn = _make_ssh_mock(NONE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, "dalpar2rrd1t"))

    assert result["curr_lpar_score"] == "none"


def test_get_lpar_memopt_score_quotes_selectors():
    """Names needing shell quoting are shlex-quoted in the command."""
    cfg = _config()
    conn = _make_ssh_mock("lpar_name=my lpar,lpar_id=1,curr_lpar_score=100")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        asyncio.run(get_lpar_memopt_score(cfg, "sys one", "my lpar"))

    expected_cmd = (
        "lsmemopt -m 'sys one' -r lpar -o currscore --filter 'lpar_names=my lpar'"
    )
    conn.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_get_lpar_memopt_score_rejects_empty_lpar_name(bad_name):
    """An empty LPAR selector fails before any SSH round-trip."""
    cfg = _config()
    conn = _make_ssh_mock("")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(ValueError, match="lpar_name"),
    ):
        asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, bad_name))

    conn.run.assert_not_called()


def test_get_lpar_memopt_score_raises_when_no_row_reported():
    """Exit-0 output without a score row is anomalous and raises HMCCLIError."""
    cfg = _config()
    conn = _make_ssh_mock("")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="returned 0 rows; expected exactly 1"),
    ):
        asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, LPAR_NAME))


def test_get_lpar_memopt_score_rejects_multiple_rows():
    """A single-LPAR query fails rather than selecting an arbitrary row."""
    cfg = _config()
    conn = _make_ssh_mock(MULTI_ROW)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="returned 2 rows; expected at most 1"),
    ):
        asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, LPAR_NAME))


def test_get_lpar_memopt_score_rejects_mismatched_row():
    """A single-LPAR query fails when the HMC reports a different partition."""
    cfg = _config()
    conn = _make_ssh_mock(SECOND_ROW)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(
            HMCCLIError, match="reported LPAR 'dapurea1t'; expected 'p9da10v1t'"
        ),
    ):
        asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, LPAR_NAME))


def test_get_lpar_memopt_score_unknown_lpar_raises_hmcclierror():
    """A non-zero HMC CLI exit (unknown LPAR) surfaces as HMCCLIError."""
    cfg = _config()
    conn = _make_ssh_mock("")
    conn.run = AsyncMock(
        side_effect=_not_found_error("The partition named doesnotexist was not found.")
    )
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="doesnotexist"),
    ):
        asyncio.run(get_lpar_memopt_score(cfg, SYSTEM_NAME, "doesnotexist"))


# ---------------------------------------------------------------------- #
# list_lpar_memopt_scores (ssh layer)
# ---------------------------------------------------------------------- #


def test_list_lpar_memopt_scores_runs_correct_command_without_filter():
    """Without an LPAR selector the command carries no --filter option."""
    cfg = _config()
    conn = _make_ssh_mock(MULTI_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, None))

    expected_cmd = f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore"
    conn.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert result == [
        {"lpar_name": "p9da10v1t", "lpar_id": "1", "curr_lpar_score": "100"},
        {"lpar_name": "dapurea1t", "lpar_id": "19", "curr_lpar_score": "100"},
    ]


def test_list_lpar_memopt_scores_with_lpar_filter():
    """An LPAR selector appends the single-value --filter form."""
    cfg = _config()
    conn = _make_ssh_mock(SCORE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, LPAR_NAME))

    expected_cmd = (
        f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore "
        f"--filter lpar_names={LPAR_NAME}"
    )
    conn.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert len(result) == 1
    assert result[0]["lpar_name"] == "p9da10v1t"


def test_list_lpar_memopt_scores_filter_rejects_multiple_rows():
    """A filtered list fails when the HMC reports more than one row."""
    cfg = _config()
    conn = _make_ssh_mock(MULTI_ROW)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="returned 2 rows; expected at most 1"),
    ):
        asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, LPAR_NAME))


def test_list_lpar_memopt_scores_filter_rejects_mismatched_row():
    """A filtered list fails when the HMC reports a different partition."""
    cfg = _config()
    conn = _make_ssh_mock(SECOND_ROW)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(
            HMCCLIError, match="reported LPAR 'dapurea1t'; expected 'p9da10v1t'"
        ),
    ):
        asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, LPAR_NAME))


def test_list_lpar_memopt_scores_reads_captured_rows():
    """The captured V10R3 answer: an inactive partition scores the literal none."""
    capture = live_fixture("cli-memopt-lpar-curr")
    conn = _make_ssh_mock(capture["stdout"])

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(list_lpar_memopt_scores(_config(), "sys-R1", None))

    assert conn.run.call_args[0][0] == capture["command"]
    assert result == [
        {"lpar_name": "sys-R1-lp3", "lpar_id": "1", "curr_lpar_score": "none"},
        {"lpar_name": "sys-R1-vios1", "lpar_id": "100", "curr_lpar_score": "100"},
    ]


def test_list_lpar_memopt_scores_empty_output_returns_empty_list():
    """A system reporting no scores prints the empty-result sentinel: no rows."""
    cfg = _config()
    conn = _make_ssh_mock(live_fixture("cli-mempool-empty")["stdout"])

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        result = asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, None))

    assert result == []


def test_list_lpar_memopt_scores_rejects_malformed_rows():
    """A row missing required HMC fields fails with an actionable error."""
    cfg = _config()
    conn = _make_ssh_mock("lpar_name=p9da10v1t,curr_lpar_score=100")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="missing required fields: lpar_id"),
    ):
        asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME))


def test_list_lpar_memopt_scores_rejects_empty_filter_name():
    """An empty (but supplied) LPAR selector fails before any SSH round-trip."""
    cfg = _config()
    conn = _make_ssh_mock("")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(ValueError, match="lpar_name"),
    ):
        asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, "  "))

    conn.run.assert_not_called()


@pytest.mark.parametrize("bad_name", ["lpar,other=1", 'lpar"', "lpar=bad"])
def test_list_lpar_memopt_scores_rejects_filter_grammar(bad_name):
    """HMC filter delimiters are rejected before any SSH round-trip."""
    cfg = _config()
    conn = _make_ssh_mock("")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="lpar_names"),
    ):
        asyncio.run(list_lpar_memopt_scores(cfg, SYSTEM_NAME, bad_name))

    conn.run.assert_not_called()


# ---------------------------------------------------------------------- #
# hmc_get_lpar_memopt_score / hmc_list_lpar_memopt_scores (public tools)
# ---------------------------------------------------------------------- #


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() resolves inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")  # pragma: allowlist secret


def test_hmc_get_lpar_memopt_score_resolves_uuids(monkeypatch, mock_hmc):
    """hmc_get_lpar_memopt_score resolves UUIDs to CLI names over REST."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock(SCORE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_memopt_score(SYSTEM_UUID, LPAR_UUID)

    expected_cmd = (
        f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore "
        f"--filter lpar_names={LPAR_NAME}"
    )
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert result == {
        "lpar_name": "p9da10v1t",
        "lpar_id": "1",
        "curr_lpar_score": "100",
    }


def test_hmc_get_lpar_memopt_score_preserves_none_score(monkeypatch, mock_hmc):
    """The literal 'none' score from the HMC is returned unchanged."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock(NONE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_get_lpar_memopt_score("p9da10", "dalpar2rrd1t")

    assert result["curr_lpar_score"] == "none"


def test_hmc_get_lpar_memopt_score_unknown_lpar(monkeypatch, mock_hmc):
    """An unknown LPAR (non-zero HMC CLI exit) raises HMCCLIError."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock("")
    conn_mock.run = AsyncMock(
        side_effect=_not_found_error("The partition named doesnotexist was not found.")
    )
    conn_mock.__aenter__ = AsyncMock(return_value=conn_mock)
    conn_mock.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(HMCCLIError, match="doesnotexist"),
    ):
        hmc_get_lpar_memopt_score("p9da10", "doesnotexist")


def test_hmc_list_lpar_memopt_scores_all_lpars(monkeypatch, mock_hmc):
    """Without an LPAR selector every system LPAR score row is returned."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn_mock = _make_ssh_mock(MULTI_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_lpar_memopt_scores(SYSTEM_UUID)

    expected_cmd = f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore"
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert [row["lpar_name"] for row in result] == ["p9da10v1t", "dapurea1t"]


def test_hmc_list_lpar_memopt_scores_filtered_by_uuid(monkeypatch, mock_hmc):
    """An LPAR UUID is resolved before the --filter option is built."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn_mock = _make_ssh_mock(SCORE_ROW)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_lpar_memopt_scores(SYSTEM_UUID, LPAR_UUID)

    expected_cmd = (
        f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore "
        f"--filter lpar_names={LPAR_NAME}"
    )
    conn_mock.run.assert_called_once_with(expected_cmd, check=True, timeout=300.0)
    assert len(result) == 1


def test_hmc_list_lpar_memopt_scores_empty(monkeypatch, mock_hmc):
    """A system reporting no scores yields an empty list."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn_mock = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_lpar_memopt_scores(SYSTEM_UUID)

    assert result == []


@pytest.mark.asyncio
async def test_operation_uuid_selectors_reuse_borrowed_client():
    from hmcpctl.client.core import HMCClient
    from hmcpctl.operations.affinity.ssh import get_lpar_memopt_score as operation

    hmc = MagicMock(spec=HMCClient)
    hmc.config = _config()
    hmc.list_system_summaries = AsyncMock(
        return_value=[{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}]
    )
    hmc.get_logical_partition = AsyncMock(
        return_value={"Resource": {"PartitionName": LPAR_NAME}}
    )
    conn = _make_ssh_mock(SCORE_ROW)
    with (
        patch(
            "hmcpctl.ssh.selectors.HMCClient",
            side_effect=AssertionError("unexpected extra REST session"),
        ) as factory,
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
    ):
        result = await operation(hmc, SYSTEM_UUID, LPAR_UUID)
    assert result["lpar_name"] == LPAR_NAME
    factory.assert_not_called()
    hmc.__aenter__.assert_not_awaited()
    hmc.__aexit__.assert_not_awaited()
    hmc.list_system_summaries.assert_awaited_once()
    hmc.get_logical_partition.assert_awaited_once_with(LPAR_UUID)
    assert conn.run.call_args.args[0] == (
        f"lsmemopt -m {SYSTEM_NAME} -r lpar -o currscore --filter lpar_names={LPAR_NAME}"
    )
