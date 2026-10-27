"""Routing contracts for the separately registered VIOS AME source capture."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_vios_ame_source as wrapper


def test_wrapper_routes_only_capture(monkeypatch):
    dispatch = Mock(return_value=2)
    monkeypatch.setattr(wrapper.vios_ame_source, "main", dispatch)
    assert wrapper.main(["--packet", "private.json"]) == 2
    dispatch.assert_called_once_with(["capture", "--packet", "private.json"])


def test_help_never_loads_connection(monkeypatch):
    connection = Mock(side_effect=AssertionError("connection from help"))
    monkeypatch.setattr(wrapper.vios_ame_source, "load_profile", connection)
    with pytest.raises(SystemExit) as caught:
        wrapper.main(["--help"])
    assert caught.value.code == 0
    connection.assert_not_called()
