"""Fresh private output must be checked before the named dispatcher starts."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))


def test_help_never_bootstraps(monkeypatch):
    import live_creation_memory as wrapper

    dispatch = Mock()
    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    with pytest.raises(SystemExit) as exit:
        wrapper.main(["--help"])
    assert exit.value.code == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["checkout", "parent", "owner", "public", "result", "sidecar"]
)
def test_wrapper_requires_fresh_private_result_and_sidecar(
    tmp_path, monkeypatch, fault
):
    import live_creation_memory as wrapper

    dispatch = Mock()
    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    path = tmp_path / "results.json"
    if fault == "checkout":
        path = Path(wrapper.__file__).resolve().parent / "never-created.json"
    if fault == "parent":
        path = tmp_path / "absent" / "results.json"
    if fault == "owner":
        from live_test.arms import memory_statistics

        monkeypatch.setattr(
            memory_statistics.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1
        )
    if fault == "public":
        tmp_path.chmod(0o755)
    if fault in {"result", "sidecar"}:
        occupied = (
            path if fault == "result" else path.with_name("results-observations.json")
        )
        occupied.write_text("preserve")
    with pytest.raises(SystemExit) as exit:
        wrapper.main(["--results-file", str(path)])
    assert exit.value.code == 2
    dispatch.assert_not_called()


def test_wrapper_dispatches_only_assigned_group(tmp_path, monkeypatch):
    import live_creation_memory as wrapper

    dispatch = Mock(return_value=0)
    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    path = tmp_path / "results.json"
    assert wrapper.main(["--results-file", str(path)]) == 0
    dispatch.assert_called_once_with(
        ["--group", "creation-memory", "--results-file", str(path)]
    )


@pytest.mark.parametrize(
    "fault", ["relative", "repository", "result-symlink", "sidecar-symlink"]
)
def test_wrapper_rejects_unsafe_paths_before_dispatch(tmp_path, monkeypatch, fault):
    import live_creation_memory as wrapper

    tmp_path.chmod(0o700)
    path = tmp_path / "results.json"
    if fault == "relative":
        monkeypatch.chdir(tmp_path)
        path = Path("results.json")
    elif fault == "repository":
        (tmp_path / ".git").write_text("gitdir: /unavailable")
    elif fault == "result-symlink":
        path.symlink_to(tmp_path / "missing.json")
    else:
        wrapper.runner.evidence.observations_path(str(path)).symlink_to(
            tmp_path / "missing.json"
        )
    dispatch = Mock()
    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    with pytest.raises(SystemExit) as exit:
        wrapper.main(["--results-file", str(path)])
    assert exit.value.code == 2
    dispatch.assert_not_called()


@pytest.mark.parametrize("raises", [False, True])
def test_wrapper_dispatches_with_private_umask_and_restores_it(
    tmp_path, monkeypatch, raises
):
    import live_creation_memory as wrapper

    tmp_path.chmod(0o700)
    observed = []

    def dispatch(args):
        mask = os.umask(0o077)
        os.umask(mask)
        observed.append(mask)
        if raises:
            raise RuntimeError("dispatch interrupted")
        return 3

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    original = os.umask(0o027)
    try:
        args = ["--results-file", str(tmp_path / "results.json")]
        if raises:
            with pytest.raises(RuntimeError, match="dispatch interrupted"):
                wrapper.main(args)
        else:
            assert wrapper.main(args) == 3
        restored = os.umask(0o027)
        assert restored == 0o027
        assert observed == [0o077]
    finally:
        os.umask(original)
