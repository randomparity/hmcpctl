"""Contract test for the native boot-started arm wrapper (#1850)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_native_boot_started as wrapper  # noqa: E402
from live_test import runner  # noqa: E402
from live_test.arms import provision_boot  # noqa: E402


@pytest.mark.parametrize(
    ("profile", "group"),
    [("ubuntu", "native-ubuntu-boot-started"), ("rocky", "native-rocky-boot-started")],
)
def test_dispatches_the_profiles_group_through_the_argument_entry_point(
    monkeypatch, profile, group
):
    seen = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda argv: seen.append(argv) or 0
    )

    assert wrapper.main([profile]) == 0
    assert seen == [["--group", group]]


def test_relays_the_runners_exit_status(monkeypatch):
    monkeypatch.setattr(runner, "_run_from_arguments", lambda _argv: 3)

    assert wrapper.main(["rocky"]) == 3


def test_every_group_is_a_native_group_the_runner_knows():
    assert set(wrapper.GROUPS.values()) == set(provision_boot.NATIVE_GROUPS)
    assert all(runner.SUBTASK_GROUPS[g] == [72] for g in wrapper.GROUPS.values())


@pytest.mark.parametrize("argv", [[], ["fedora"], ["ubuntu", "--results-file", "x"]])
def test_a_missing_or_unknown_argument_is_rejected_before_any_run(monkeypatch, argv):
    def forbidden(_argv):
        raise AssertionError("ran despite a bad argument")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(argv)

    assert exit_info.value.code != 0


def test_help_explains_itself_instead_of_starting_a_run(monkeypatch, capsys):
    def forbidden(_argv):
        raise AssertionError("--help started a live run")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(["--help"])

    assert exit_info.value.code == 0
    assert "mutates a managed system" in capsys.readouterr().out
