"""Named product system-profile arm dispatcher, never a source-arm alias."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_system_profiles as wrapper
from live_test import runner


def test_system_profiles_wrapper(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: calls.append(args) or 0
    )
    destination = tmp_path / "results.json"
    assert wrapper.main(["--results-file", str(destination)]) == 0
    assert calls == [["--group", "system-profiles", "--results-file", str(destination)]]
    with pytest.raises(SystemExit):
        wrapper.main(["--help"])
    assert len(calls) == 1


def test_system_profiles_wrapper_refuses_nonprivate_or_existing(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: called.append(args) or 0
    )
    occupied = tmp_path / "occupied.json"
    occupied.write_text("original")
    with pytest.raises(SystemExit):
        wrapper.main(["--results-file", str(occupied)])
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(SystemExit):
        wrapper.main(["--results-file", str(public / "results.json")])
    assert occupied.read_text() == "original" and called == []
