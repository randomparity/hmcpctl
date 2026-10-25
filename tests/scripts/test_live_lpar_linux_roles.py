"""The C05 wrapper parses before dispatching its finite hardware group."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_lpar_linux_roles as wrapper
from live_test import runner


def test_dispatch_and_exit_status(monkeypatch):
    dispatch = Mock(return_value=3)
    monkeypatch.setattr(runner, "_run_from_arguments", dispatch)
    assert wrapper.main([]) == 3
    dispatch.assert_called_once_with(["--group", "lpar-linux-roles"])


@pytest.mark.parametrize("argv,code", [(["--help"], 0), (["--invalid"], 2)])
def test_parse_precedes_dispatch(monkeypatch, argv, code):
    dispatch = Mock(side_effect=AssertionError("unexpected hardware dispatch"))
    monkeypatch.setattr(runner, "_run_from_arguments", dispatch)
    with pytest.raises(SystemExit) as result:
        wrapper.main(argv)
    assert result.value.code == code
    dispatch.assert_not_called()
