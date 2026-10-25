import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_memory_move as wrapper


def test_private_wrapper_forwards_only_named_group(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    seen = []
    monkeypatch.setattr(
        wrapper.runner, "_run_from_arguments", lambda args: seen.append(args) or 3
    )
    path = tmp_path / "result.json"
    assert wrapper.main(["--results-file", str(path)]) == 3
    assert seen == [["--group", "lpar-memory-move", "--results-file", str(path)]]


@pytest.mark.parametrize("case", ["missing", "exposed", "existing"])
def test_wrapper_refuses_bad_custody(monkeypatch, tmp_path, case):
    def forbidden(args):
        raise AssertionError("unexpected dispatch")

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", forbidden)
    tmp_path.chmod(0o755 if case == "exposed" else 0o700)
    path = tmp_path / "result.json"
    if case == "existing":
        path.write_text("old")
    with pytest.raises(SystemExit) as error:
        wrapper.main([] if case == "missing" else ["--results-file", str(path)])
    assert error.value.code == 2


@pytest.mark.parametrize("case", ["observations", "repository", "absent-parent"])
def test_wrapper_refuses_unsafe_destination(monkeypatch, tmp_path, case):
    tmp_path.chmod(0o700)
    path = tmp_path / "result.json"
    if case == "observations":
        wrapper.runner.evidence.observations_path(str(path)).write_text("old")
    elif case == "repository":
        (tmp_path / ".git").write_text("gitdir: /unavailable")
    else:
        path = tmp_path / "absent" / "result.json"

    def forbidden(args):
        raise AssertionError("unexpected dispatch")

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", forbidden)
    with pytest.raises(SystemExit) as error:
        wrapper.main(["--results-file", str(path)])
    assert error.value.code == 2


def test_wrapper_restores_umask_after_failure(monkeypatch, tmp_path):
    import os

    tmp_path.chmod(0o700)
    masks = []
    original = os.umask

    def capture(mask):
        masks.append(mask)
        return original(mask)

    monkeypatch.setattr(os, "umask", capture)

    def fail(args):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", fail)
    before = original(0o027)
    try:
        with pytest.raises(RuntimeError, match="dispatch failed"):
            wrapper.main(["--results-file", str(tmp_path / "result.json")])
        assert masks == [0o077, 0o027]
    finally:
        original(before)
