"""The focused wrapper cannot dispatch broad profile or activation arms."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_partition_profile_contract as wrapper
from live_test import partition_profile_contract as arm
from live_test import runner


def test_focused_wrapper_and_dispatch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: calls.append(args) or 0
    )
    destination = tmp_path / "results.json"
    assert wrapper.main(["--results-file", str(destination)]) == 0
    assert calls == [["--group", arm.GROUP, "--results-file", str(destination)]]
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [arm.SUBTASK]
    assert (
        arm.SUBTASK
        not in runner.SUBTASK_GROUPS["all"] + runner.SUBTASK_GROUPS["round2"]
    )
    with pytest.raises(SystemExit):
        wrapper.main(["--help"])
    assert len(calls) == 1


def test_refuses_existing_or_public_destination(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_run_from_arguments", lambda args: calls.append(args))
    existing = tmp_path / "results.json"
    existing.write_text("original")
    with pytest.raises(SystemExit):
        wrapper.main(["--results-file", str(existing)])
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(SystemExit):
        wrapper.main(["--results-file", str(directory / "results.json")])
    assert existing.read_text() == "original" and calls == []
