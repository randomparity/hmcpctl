"""Inventory format contracts with constructed output fixtures, not HMC captures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from unittest.mock import Mock

import pytest
from click import unstyle
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands.lpar import inventory as lpars
from hmcpctl.cli_commands.storage import resources as storage
from hmcpctl.cli_commands.systems import core as systems
from hmcpctl.cli_commands.vios import core as vios
from hmcpctl.operations.inventory.composite import _lpar_summary
from hmcpctl.operations.storage.resources import (
    OpticalMedia,
    StorageMapping,
    VolumeGroup,
)
from hmcpctl.operations.systems.fleet import FleetListing
from hmcpctl.operations.systems.health import FleetHealthResult

RUNNER = CliRunner()
NAME = "long-[inventory]-東京-" * 5 + "\\tail\tline\nnext\r"
UUID = "12345678-1234-4234-8234-123456789abc"
ENTRY = {"UUID": UUID, "Resource": {"PartitionName": NAME, "SystemName": NAME}}
CASES = [
    (systems, ["systems", "list"], [ENTRY]),
    (systems, ["systems", "show", "target"], ENTRY),
    (systems, ["systems", "summary", "target"], None),
    (systems, ["systems", "health"], FleetHealthResult((), (), (), ())),
    (systems, ["systems", "capacity"], []),
    (systems, ["systems", "find-placement", "4096"], []),
    (lpars, ["lpars", "list"], FleetListing([ENTRY])),
    (lpars, ["lpars", "show", "target"], ENTRY),
    (lpars, ["lpars", "summary", "target"], _lpar_summary(ENTRY, [])),
    (lpars, ["lpars", "refcodes", "system", "target"], []),
    (vios, ["vios", "list"], FleetListing([ENTRY])),
    (storage, ["storage", "list-vgs", "vios"], [VolumeGroup(UUID, NAME, 0, None, "")]),
    (storage, ["storage", "get-media-repo", "vios", "--vg", "vg"], ENTRY),
    (
        storage,
        ["storage", "list-optical-media", "vios", "--vg", "vg"],
        [OpticalMedia(NAME, 0)],
    ),
    (
        storage,
        ["storage", "list-mappings", "vios"],
        [StorageMapping(UUID, None, "", NAME)],
    ),
]


def _stub(monkeypatch, module, data):
    call = Mock(return_value=data)
    monkeypatch.setattr(module, "with_client", call)
    if module is lpars:
        monkeypatch.setattr(module, "run_cli_coroutine", call)
    return call


@pytest.mark.parametrize("module,args,data", CASES)
@pytest.mark.parametrize(
    "flags", [["--json", "--format", "tsv"], ["-j", "--format", "table"]]
)
def test_conflicts_fail_before_operations(monkeypatch, module, args, data, flags):
    call = _stub(monkeypatch, module, data)
    result = RUNNER.invoke(cli.app, [*args, *flags])
    assert result.exit_code == 2
    assert "conflict" in unstyle(result.stderr).lower()
    assert result.stdout == ""
    call.assert_not_called()


@pytest.mark.parametrize("module,args,data", CASES)
@pytest.mark.parametrize("flag", ["--json", "-j", "--format"])
def test_json_aliases_keep_original_values(monkeypatch, module, args, data, flag):
    if data is None:
        from hmcpctl.operations.inventory.composite import SystemSummary

        data = SystemSummary(UUID, NAME, None, None, None, 0, 0, 0, 0, None, {}, None)
    _stub(monkeypatch, module, data)
    flags = [flag, "json"] if flag == "--format" else [flag]
    result = RUNNER.invoke(cli.app, [*args, *flags])
    assert result.exit_code == 0, result.output
    expected = data
    if hasattr(data, "__dataclass_fields__"):
        expected = asdict(data)
    elif isinstance(data, list):
        expected = [
            asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in data
        ]
    assert json.loads(result.stdout) == json.loads(json.dumps(expected))


def _decode(cell):
    if cell == "\\N":
        return None
    output = []
    chars = iter(cell)
    for char in chars:
        output.append(
            {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}[next(chars)]
            if char == "\\"
            else char
        )
    return "".join(output)


def test_mapping_tsv_preserves_absent_empty_and_literal_null(monkeypatch):
    data = [StorageMapping("\\N", None, "", NAME)]
    _stub(monkeypatch, storage, data)
    result = RUNNER.invoke(cli.app, ["storage", "list-mappings", "vios"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "Mapping ID\tClient LPAR\tBacking Storage\tType"
    assert [_decode(cell) for cell in lines[1].split("\t")] == ["\\N", None, NAME, ""]


@pytest.mark.parametrize("width", [40, 80, 200])
@pytest.mark.parametrize("kind", ["full", "empty", "partial"])
def test_real_stdout_file_redirection_is_lossless(tmp_path, width, kind):
    entry = {**ENTRY, "ManagedSystem": {"UUID": UUID, "SystemName": NAME}}
    script = f"""
from hmcpctl import cli
from hmcpctl.cli_commands.lpar import inventory
from hmcpctl.operations.systems.fleet import FleetListing, UnreadableSystem
entries = {[entry] if kind != "empty" else []!r}
warnings = {[("offline", UUID, "No Connection", None)] if kind == "partial" else []!r}
inventory.with_client = lambda op: FleetListing(entries, [UnreadableSystem(*w) for w in warnings])
cli.app(prog_name="hmcpctl")
"""
    destination = tmp_path / "inventory.tsv"
    with destination.open("wb") as stdout:
        result = subprocess.run(
            [sys.executable, "-c", script, "lpars", "list"],
            stdout=stdout,
            stderr=subprocess.PIPE,
            env={**os.environ, "COLUMNS": str(width)},
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    lines = destination.read_text().splitlines()
    assert lines[0] == "System\tSystem UUID\tName\tID\tUUID\tState\tType\tOS\tRMC"
    if kind != "empty":
        cells = [_decode(cell) for cell in lines[1].split("\t")]
        assert cells[:5] == [NAME, UUID, NAME, None, UUID]
        assert len(lines) == 2
    else:
        assert len(lines) == 1
        assert b"No logical partitions found" in result.stderr
    assert (b"Skipped managed system" in result.stderr) == (kind == "partial")


@pytest.mark.parametrize(
    "isatty,flags,table",
    [
        (True, [], True),
        (False, [], False),
        (True, ["--format", "tsv"], False),
        (False, ["--format", "table"], True),
    ],
)
def test_stdout_routes_and_explicit_overrides(monkeypatch, isatty, flags, table):
    _stub(monkeypatch, systems, [{"UUID": UUID, "Resource": {"SystemName": "[東京]"}}])
    with RUNNER.isolation() as streams:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)
        from typer.main import get_command

        get_command(cli.app).main(["systems", "list", *flags], standalone_mode=False)
        value = streams[0].getvalue().decode()
    assert ("┏" in value or "┌" in value) == table
    assert "[東京]" in value


def test_object_tsv_preserves_nested_json(monkeypatch):
    _stub(monkeypatch, lpars, ENTRY)
    result = RUNNER.invoke(cli.app, ["lpars", "show", "target", "--format", "tsv"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "Field\tValue"
    rows = dict(tuple(_decode(c) for c in line.split("\t")) for line in lines[1:])
    assert rows["UUID"] == UUID
    assert json.loads(rows["Resource"]) == ENTRY["Resource"]


def test_no_json_removed_and_bad_format_make_no_calls(monkeypatch):
    call = _stub(monkeypatch, lpars, ENTRY)
    for flags in (["--no-json"], ["--format", "csv"]):
        result = RUNNER.invoke(cli.app, ["lpars", "show", "target", *flags])
        assert result.exit_code == 2
    call.assert_not_called()


@pytest.mark.parametrize("group", ["lpars", "vios"])
@pytest.mark.parametrize("format", ["json", "tsv", "table"])
def test_partial_empty_fleet_keeps_diagnostics(monkeypatch, group, format):
    from hmcpctl.operations.systems.fleet import UnreadableSystem

    listing = FleetListing(
        [], [UnreadableSystem("offline", UUID, "No Connection", None)]
    )
    _stub(monkeypatch, lpars if group == "lpars" else vios, listing)
    result = RUNNER.invoke(cli.app, [group, "list", "--format", format])
    assert result.exit_code == 0, result.output
    assert "Skipped managed system offline" in result.stderr
    assert "Skipped" not in result.stdout
    if format == "json":
        assert json.loads(result.stdout) == asdict(listing)
    elif format == "tsv":
        assert result.stdout.count("\n") == 1
        assert result.stdout.startswith("System\tSystem UUID\t")


def test_summary_warning_and_agreeing_json_flags(monkeypatch):
    from hmcpctl.operations.inventory.composite import SystemSummary

    summary = SystemSummary(
        UUID, NAME, None, None, None, 0, 0, 0, 0, None, {}, None, ("unavailable",)
    )
    _stub(monkeypatch, systems, summary)
    result = RUNNER.invoke(
        cli.app, ["systems", "summary", "target", "-j", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["warnings"] == ["unavailable"]
    assert result.stderr.strip() == "unavailable"


def test_resource_tsv_keeps_zero_empty_and_attributed_fields(monkeypatch):
    entry = {
        "UUID": UUID,
        "Resource": {
            "SystemName": {"text": NAME},
            "State": "",  # live-vocabulary: allow synthetic empty-cell serialization edge
            "IPAddress": 0,
        },
    }
    _stub(monkeypatch, systems, [entry])
    result = RUNNER.invoke(cli.app, ["systems", "list", "--format", "tsv"])
    assert result.exit_code == 0, result.output
    assert [_decode(c) for c in result.stdout.splitlines()[1].split("\t")] == [
        NAME,
        UUID,
        "",
        None,
        "0",
    ]
