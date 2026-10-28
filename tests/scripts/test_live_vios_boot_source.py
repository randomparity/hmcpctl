"""The opt-in wrapper cannot select a read/write mode from caller input."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_vios_boot_source as wrapper


def test_wrapper_capture(monkeypatch):
    route = Mock(return_value=2)
    monkeypatch.setattr(wrapper.vios_boot_source, "main", route)
    assert wrapper.main(["--packet", "p"]) == 2
    route.assert_called_once_with(["capture", "--packet", "p"])


def test_help_no_connection(monkeypatch):
    connection = Mock(side_effect=AssertionError("connection from help"))
    monkeypatch.setattr(wrapper.vios_boot_source, "load_profile", connection)
    with pytest.raises(SystemExit) as caught:
        wrapper.main(["--help"])
    assert caught.value.code == 0
    connection.assert_not_called()
