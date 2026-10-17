"""Decommission output remains copyable and separate from confirmation text."""

import json
import shlex
from dataclasses import asdict

import click
import pytest
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands.lpar import decommission as command
from hmcpctl.operations.lpar.decommission import DecommissionResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep


@pytest.fixture
def evidence_result(monkeypatch):
    result = DecommissionResult(
        False,
        False,
        "22222222-2222-2222-2222-222222222222",
        False,
        (
            WorkflowStep(
                "detach_storage_mappings", "error", {"mappings": (), "error": "refused"}
            ),
        ),
        ("Inspect unresolved evidence before acting.",),
        {
            "lpar_uuid": "22222222-2222-2222-2222-222222222222",
            "lpar_name": "target",
            "partition_id": 7,
            "state": "running",
            "owner": None,
            "adapters": (),
            "storage_mappings": (),
            "unresolved_storage_mapping_count": 1,
            "unavailable_storage_source_count": 0,
            "mapping_evidence": (
                {
                    "vios_uuid": "33333333-3333-3333-3333-333333333333",
                    "type": "VirtualSCSIMapping",
                    "backing_device": "disk-雪[long]-" * 10,
                    "disposition": "unproven",
                    "missing_identity": "client UUID",
                },
            ),
        },
    )
    monkeypatch.setattr(command, "with_client", lambda callback: result)
    return result


@pytest.mark.parametrize("format", ["tsv", "json", "table"])
def test_decommission_formats_and_confirmation(evidence_result, format):
    run = CliRunner().invoke(
        cli.app,
        ["lpars", "decommission", "target", "--system", "system-a", "--format", format],
        input="y\n",
        env={"COLUMNS": "40", "FORCE_COLOR": "1"},
    )
    assert run.exit_code == 1, run.output
    assert "Decommission LPAR" in click.unstyle(run.stderr)
    assert "Decommission LPAR" not in run.stdout
    if format == "json":
        assert json.loads(run.stdout) == json.loads(json.dumps(asdict(evidence_result)))
    else:
        assert "unproven" in click.unstyle(run.stdout)
        assert "{'" not in run.stdout and "…" not in run.stdout
        if format == "tsv":
            assert run.stdout.startswith("Section\tItem\tField\tValue\n")
            assert "disk-雪[long]-" * 10 in run.stdout
            inspection = next(
                line.split("\t")[-1]
                for line in run.stdout.splitlines()
                if "inspection" in line
            )
            argv = shlex.split(inspection)
            assert argv[:3] == ["hmcpctl", "storage", "list-mappings"]
            assert (
                "--lpar" not in argv and argv[argv.index("--system") + 1] == "system-a"
            )


def test_format_conflict_and_decline_precede_io(monkeypatch):
    def unexpected(callback):
        pytest.fail("backend invoked")

    monkeypatch.setattr(command, "with_client", unexpected)
    for args, input_text, code in [
        (["--json", "--format", "tsv"], "", 2),
        (["--format", "json"], "n\n", 1),
    ]:
        run = CliRunner().invoke(
            cli.app,
            ["lpars", "decommission", "target", "--system", "system-a", *args],
            input=input_text,
        )
        assert run.exit_code == code
        assert not run.stdout


@pytest.mark.parametrize(
    "kind,vios",
    [
        ("VirtualSCSIMapping", "33333333-3333-3333-3333-333333333333"),
        ("VirtualFibreChannelMapping", "33333333-3333-3333-3333-333333333333"),
        ("unknown", "unknown"),
    ],
)
def test_inspection_commands_parse_without_lpar_filter(monkeypatch, kind, vios):
    from hmcpctl.cli_commands import raw
    from hmcpctl.cli_commands.storage import resources
    from hmcpctl.cli_commands.vios import core
    from hmcpctl.operations.systems.fleet import FleetListing

    monkeypatch.setattr(resources, "with_client", lambda callback: [])
    monkeypatch.setattr(core, "with_client", lambda callback: FleetListing((), ()))
    monkeypatch.setattr(raw, "with_client", lambda callback: ("<inventory/>", {}))
    argv = shlex.split(
        command._inspection({"type": kind, "vios_uuid": vios}, "system ' [雪]")
    )
    assert "--lpar" not in argv
    result = CliRunner().invoke(cli.app, argv[1:])
    assert result.exit_code == 0, result.output


def test_real_redirected_decommission_is_lossless(evidence_result):
    import os
    import subprocess
    import sys

    source = f"""
from hmcpctl import cli
from hmcpctl.cli_commands.lpar import decommission as command
from hmcpctl.operations.lpar.decommission import DecommissionResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
result = {evidence_result!r}
command.with_client = lambda callback: result
cli.app(args=['lpars', 'decommission', 'target', '--system', 'system-a', '--yes'])
"""
    run = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "40", "FORCE_COLOR": "1"},
        timeout=20,
        check=False,
    )
    assert run.returncode == 1
    assert run.stdout.startswith("Section\tItem\tField\tValue\n")
    assert "disk-雪[long]-" * 10 in run.stdout
    assert "…" not in run.stdout
