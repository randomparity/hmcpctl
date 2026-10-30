"""Named vNIC policy procedure wrapper is installed and help is effect-free."""

import importlib.util
from pathlib import Path

import pytest


def test_help_exits_without_connecting(capsys):
    path = Path(__file__).parents[2] / "scripts/live_vnic_creation_policy.py"
    spec = importlib.util.spec_from_file_location("vnic_creation_wrapper", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert "--packet-sha256" in capsys.readouterr().out
