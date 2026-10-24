"""The named memory arm preserves output custody and never runs from help."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_memory_quantities as wrapper
from live_test import runner


def test_forwards_private_results_path_and_runner_status(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: seen.append(args) or 3
    )
    path = str(tmp_path / "results.json")
    assert wrapper.main(["--results-file", path]) == 3
    assert seen == [["--group", "lpar-memory-quantities", "--results-file", path]]


@pytest.mark.parametrize("args,code", [(["--help"], 0), (["--group", "all"], 2)])
def test_help_and_invalid_args_cannot_dispatch(monkeypatch, args, code):
    def forbidden(args):
        raise AssertionError("unexpected hardware dispatch")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)
    with pytest.raises(SystemExit) as error:
        wrapper.main(args)
    assert error.value.code == code
