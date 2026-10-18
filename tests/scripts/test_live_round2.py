"""Contract test for the round2 arm wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_round2 as wrapper  # noqa: E402
from live_test import runner  # noqa: E402


def test_dispatches_its_own_group_through_the_argument_entry_point(monkeypatch):
    """Not `main`: that skips the credential bootstrap and the results naming."""
    seen = []
    monkeypatch.setattr(
        runner,
        "_run_from_arguments",
        lambda argv: seen.append(argv) or 0,
    )

    assert wrapper.main([]) == 0
    assert seen == [["--group", "round2"]]


def test_relays_the_runners_exit_status(monkeypatch):
    monkeypatch.setattr(runner, "_run_from_arguments", lambda _argv: 3)

    assert wrapper.main([]) == 3


def test_its_group_is_one_the_runner_knows():
    assert wrapper.GROUP in runner.SUBTASK_GROUPS


def test_help_explains_itself_instead_of_starting_a_run(monkeypatch, capsys):
    """A wrapper that ignored --help would mutate a managed system instead."""

    def forbidden(_argv):
        raise AssertionError("--help started a live run")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(["--help"])

    assert exit_info.value.code == 0
    assert "mutates a managed system" in capsys.readouterr().out


def test_an_unknown_option_is_rejected_before_any_run(monkeypatch):
    def forbidden(_argv):
        raise AssertionError("ran despite an unknown option")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(["--results-file", "x.json"])

    assert exit_info.value.code != 0
