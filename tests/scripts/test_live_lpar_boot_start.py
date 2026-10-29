"""Named ST68 wrapper forwards only to the finite standalone procedure."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_boot_start as wrapper


def test_exact_run_dispatch(monkeypatch):
    dispatch = Mock(return_value=2)
    monkeypatch.setattr(wrapper.lpar_boot_start, "main", dispatch)
    args = [
        "--packet",
        "p",
        "--packet-sha256",
        "s",
        "--preflight",
        "f",
        "--results-file",
        "r",
        "--grant-packet-sha256",
        "s",
        "--exclusive-writer-window",
    ]
    assert wrapper.main(args) == 2
    dispatch.assert_called_once_with(["run", *args])


def test_help_cannot_authenticate(monkeypatch):
    client = Mock()
    monkeypatch.setattr(wrapper.lpar_boot_start, "HMCClient", client)
    with pytest.raises(SystemExit) as result:
        wrapper.main(["--help"])
    assert result.value.code == 0
    client.assert_not_called()


def test_no_default_runner_dispatch():
    from live_test import runner

    assert "lpar-boot-start" not in runner.SUBTASK_GROUPS


def test_recovery_accepts_no_grant_flags(monkeypatch):
    client = Mock()
    monkeypatch.setattr(wrapper.lpar_boot_start, "HMCClient", client)
    assert (
        wrapper.lpar_boot_start.main(
            [
                "recover",
                "--results",
                "not-read",
                "--grant-packet-sha256",
                "s",
            ]
        )
        == 2
    )
    client.assert_not_called()
