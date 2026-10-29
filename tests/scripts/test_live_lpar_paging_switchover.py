import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_paging_switchover as wrapper


def test_named_private_wrapper(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    calls = []
    monkeypatch.setattr(
        wrapper.runner, "_run_from_arguments", lambda args: calls.append(args) or 3
    )
    path = tmp_path / "result.json"
    assert wrapper.main(["--results-file", str(path)]) == 3
    assert calls == [["--group", "lpar-paging-switchover", "--results-file", str(path)]]


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "existing",
        "observations",
        "symlink",
        "exposed",
        "repository",
        "missing-parent",
    ],
)
def test_wrapper_refuses_unsafe_output(monkeypatch, tmp_path, case):
    tmp_path.chmod(0o700)
    path = tmp_path / "result.json"
    if case == "relative":
        path = Path("relative-result.json")
    elif case == "existing":
        path.write_text("retained")
    elif case == "observations":
        wrapper.runner.evidence.observations_path(str(path)).write_text("retained")
    elif case == "symlink":
        path.symlink_to(tmp_path / "missing.json")
    elif case == "exposed":
        tmp_path.chmod(0o755)
    elif case == "repository":
        (tmp_path / ".git").write_text("gitdir: /unavailable")
    else:
        path = tmp_path / "missing-parent" / "result.json"

    def forbidden(_args):
        pytest.fail("unsafe destination reached dispatch")

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", forbidden)
    with pytest.raises(SystemExit) as caught:
        wrapper.main(["--results-file", str(path)])
    assert caught.value.code == 2


@pytest.mark.parametrize("raises", [False, True])
def test_private_wrapper_restores_umask(monkeypatch, tmp_path, raises):
    import os

    tmp_path.chmod(0o700)
    observed = []

    def dispatch(args):
        mask = os.umask(0o077)
        os.umask(mask)
        observed.append(mask)
        if raises:
            raise ValueError("interrupted")
        return 0

    monkeypatch.setattr(wrapper.runner, "_run_from_arguments", dispatch)
    original = os.umask(0o027)
    try:
        if raises:
            with pytest.raises(ValueError):
                wrapper.main(["--results-file", str(tmp_path / "result.json")])
        else:
            assert wrapper.main(["--results-file", str(tmp_path / "result.json")]) == 0
        restored = os.umask(0o027)
        assert restored == 0o027
        assert observed == [0o077]
    finally:
        os.umask(original)
