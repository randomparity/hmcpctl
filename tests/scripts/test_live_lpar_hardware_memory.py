"""The ST55 wrapper parses every argument before entering a live client."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_hardware_memory as wrapper
from live_test import runner


@pytest.mark.parametrize("status", [0, 3])
def test_named_startup_and_status(monkeypatch, status):
    dispatch = Mock(return_value=status)
    monkeypatch.setattr(runner, "_run_from_arguments", dispatch)
    assert wrapper.main([]) == status
    dispatch.assert_called_once_with(["--group", "lpar-hardware-memory"])


@pytest.mark.parametrize(
    "args,code", [(["--help"], 0), (["--results-file", "x"], 2), (["x"], 2)]
)
def test_arguments_cannot_enter_client(monkeypatch, capsys, args, code):
    dispatch = Mock(side_effect=AssertionError("unexpected live dispatch"))
    monkeypatch.setattr(runner, "_run_from_arguments", dispatch)
    with pytest.raises(SystemExit) as error:
        wrapper.main(args)
    assert error.value.code == code
    dispatch.assert_not_called()
    if code == 0:
        assert "mutates a managed system" in capsys.readouterr().out
