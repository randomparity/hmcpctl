"""ST69 is a named, explicit preflight/run/recovery procedure."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_test_preflight
import live_test_recovery


@pytest.fixture(autouse=True)
def require_arm():
    global live_ethernet_update, ethernet_update
    assert (Path(__file__).parents[2] / "scripts/live_ethernet_update.py").exists(), (
        "ST69 wrapper absent"
    )
    live_ethernet_update = importlib.import_module("live_ethernet_update")
    ethernet_update = importlib.import_module("live_test.ethernet_update")


def test_named_entry_has_only_run_route(monkeypatch):
    calls = []
    monkeypatch.setattr(ethernet_update, "main", lambda args: calls.append(args) or 7)
    assert live_ethernet_update.main(["--packet", "private"]) == 7
    assert calls == [["run", "--packet", "private"]]


def test_preflight_and_recovery_routes(monkeypatch):
    calls = []
    monkeypatch.setenv("HMC_PROFILE", "st69-profile")

    def dispatch(args):
        import os

        assert os.environ["HMC_PROFILE"] == "st69-profile"
        calls.append(args)
        return 7

    monkeypatch.setattr(ethernet_update, "main", dispatch)
    assert (
        live_test_preflight.main(
            [
                "--group",
                "client-ethernet-update",
                "--packet",
                "/private/p",
                "--packet-sha256",
                "a" * 64,
                "--output",
                "/private/o",
            ]
        )
        == 7
    )
    assert calls[-1][0] == "preflight"
    assert (
        live_test_recovery.main(
            [
                "--ethernet-update-procedure",
                "--results",
                "/private/run",
                "--output",
                "/private/recovery",
            ]
        )
        == 7
    )
    assert calls[-1] == [
        "recover",
        "--results",
        "/private/run",
        "--output",
        "/private/recovery",
    ]


def test_missing_separate_grant_cannot_parse():
    with pytest.raises(SystemExit):
        ethernet_update.main(
            [
                "run",
                "--packet",
                "/p",
                "--packet-sha256",
                "a" * 64,
                "--preflight",
                "/f",
                "--out",
                "/o",
            ]
        )


def test_generic_recovery_refuses_st69_before_credentials(tmp_path, monkeypatch):
    import json
    import sys
    import types

    source = tmp_path / "run.json"
    source.write_text(json.dumps({"group": "client-ethernet-update"}))
    monkeypatch.setitem(
        sys.modules,
        "hmcpctl.config",
        types.SimpleNamespace(
            load_profile=lambda *_: pytest.fail("credentials reached")
        ),
    )
    with pytest.raises(SystemExit):
        live_test_recovery.main(["--results", str(source)])
