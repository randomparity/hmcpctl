"""The ST59 named entry point cannot dispatch through help or invalid options."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from live_test import runner


def wrapper():
    assert (
        Path(__file__).parents[2] / "scripts/live_lpar_sharing_weight.py"
    ).is_file(), "ST59 wrapper must be installed"
    return importlib.import_module("live_lpar_sharing_weight")


def test_sharing_wrapper(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: seen.append(args) or 3
    )
    path = str(tmp_path / "results.json")
    assert wrapper().main(["--results-file", path]) == 3
    assert seen == [["--group", "processor-sharing-weight", "--results-file", path]]


@pytest.mark.parametrize(
    "args,code",
    [(["--help"], 0), (["--group", "all"], 2), (["--results-file"], 2), (["59"], 2)],
)
def test_sharing_wrapper_no_dispatch(monkeypatch, args, code):
    def forbidden(args):
        raise AssertionError("unexpected hardware dispatch")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)
    with pytest.raises(SystemExit) as error:
        wrapper().main(args)
    assert error.value.code == code
