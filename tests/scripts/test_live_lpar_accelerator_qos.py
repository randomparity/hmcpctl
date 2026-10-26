"""The only entry point accepts a private, new, external result destination."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_accelerator_qos as wrapper


@pytest.mark.parametrize("fails", [False, True])
def test_live_config_wrapper_private_umask(monkeypatch, tmp_path, fails):
    tmp_path.chmod(0o700)
    seen = []

    def dispatch(args):
        seen.append(args)
        probe = tmp_path / "probe"
        probe.write_text("private")
        assert probe.stat().st_mode & 0o777 == 0o600
        if fails:
            raise RuntimeError("dispatch failed")
        return 3

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    previous = os.umask(0o027)
    try:
        args = ["--results-file", str(tmp_path / "results.json")]
        if fails:
            with pytest.raises(RuntimeError, match="dispatch failed"):
                wrapper.main(args)
        else:
            assert wrapper.main(args) == 3
        assert os.umask(0o027) == 0o027
    finally:
        os.umask(previous)
    assert seen == [["--group", "lpar-accelerator-qos", *args]]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "exposed",
        "existing",
        "observations",
        "repository",
        "ancestor",
        "absent-parent",
        "unknown",
        "symlink",
        "observation-symlink",
    ],
)
def test_live_config_wrapper_denial(monkeypatch, tmp_path, case):
    tmp_path.chmod(0o755 if case == "exposed" else 0o700)
    path = tmp_path / "results.json"
    if case == "existing":
        path.write_text("old")
    elif case == "observations":
        wrapper.runner.evidence.observations_path(str(path)).write_text("old")
    elif case == "symlink":
        path.symlink_to(tmp_path / "absent")
    elif case == "observation-symlink":
        wrapper.runner.evidence.observations_path(str(path)).symlink_to(
            tmp_path / "absent"
        )
    elif case in ("repository", "ancestor"):
        (tmp_path / ".git").write_text("gitdir: /absent")
        if case == "ancestor":
            path = tmp_path / "nested" / "results.json"
            path.parent.mkdir(mode=0o700)
    elif case == "absent-parent":
        path = tmp_path / "absent" / "results.json"

    def forbidden(args):
        raise AssertionError("dispatch on denial")

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", forbidden)
    args = ["--results-file", str(path)]
    with pytest.raises(SystemExit) as error:
        wrapper.main(
            []
            if case == "missing"
            else [*args, "--group", "all"]
            if case == "unknown"
            else args
        )
    assert error.value.code == 2
