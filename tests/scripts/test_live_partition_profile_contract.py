"""The focused wrapper cannot dispatch broad profile or activation arms."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_partition_profile_contract as wrapper
import live_system_profiles as system_profiles_wrapper
from live_test import runner
from live_test.arms import partition_profile_contract as arm
from live_test.arms import profile_contract_run as flow


@pytest.mark.parametrize("selected", [wrapper, system_profiles_wrapper])
@pytest.mark.parametrize("raises", [False, True])
def test_private_profile_wrapper_restores_a_private_umask(
    tmp_path, monkeypatch, selected, raises
):
    tmp_path.chmod(0o700)
    observed = []

    def dispatch(_args):
        mask = os.umask(0o077)
        os.umask(mask)
        observed.append(mask)
        if raises:
            raise RuntimeError("dispatch interrupted")
        return 7

    monkeypatch.setattr(runner, "_run_from_arguments", dispatch)
    original = os.umask(0o027)
    try:
        args = ["--results-file", str(tmp_path / "results.json")]
        if raises:
            with pytest.raises(RuntimeError, match="dispatch interrupted"):
                selected.main(args)
        else:
            assert selected.main(args) == 7
        restored = os.umask(0o027)
        assert restored == 0o027
        assert observed == [0o077]
    finally:
        os.umask(original)


@pytest.mark.parametrize("selected", [wrapper, system_profiles_wrapper])
@pytest.mark.parametrize("fault", ["relative", "result-symlink", "sidecar"])
def test_private_profile_wrapper_refuses_unsafe_results(
    tmp_path, monkeypatch, selected, fault
):
    tmp_path.chmod(0o700)
    path = tmp_path / "results.json"
    if fault == "relative":
        monkeypatch.chdir(tmp_path)
        path = Path("results.json")
    elif fault == "result-symlink":
        path.symlink_to(tmp_path / "missing.json")
    else:
        runner.evidence.observations_path(str(path)).write_text("preserve")
    calls = []
    monkeypatch.setattr(runner, "_run_from_arguments", lambda args: calls.append(args))
    with pytest.raises(SystemExit) as raised:
        selected.main(["--results-file", str(path)])
    assert raised.value.code == 2
    assert calls == []


def test_focused_wrapper_and_dispatch(tmp_path, monkeypatch):
    assert runner.exercise_profile_contract is flow.exercise
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
