"""Storage presentation contracts using synthetic operation results (#1699)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from unittest.mock import Mock

import pytest
from click import unstyle
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands.storage import resources as storage
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.lpar.provision import AttachDiskResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.storage.resources import StorageMapResult

RUNNER = CliRunner()
RAW = {"Resource": {"MachineSerialNumber": "unrelated-hardware-marker"}}
LOCATION = ChangeLocation("Disabled", "current-configuration")
ATTACH = AttachDiskResult(
    True, "lp-test", False, (WorkflowStep("storage", "ok", RAW),), (), LOCATION
)
UPLOAD = {
    "status": "uploaded",
    "media_name": "media-test",
    "media_size_bytes": 12,
    "source_sha256": "abc",
    "media": RAW,
}
CASES = [
    (
        "create-vg",
        ["vi-test", "--name", "vg-test", "--pvs", "hdisk1", "--yes"],
        RAW,
        "vg-test",
    ),
    (
        "create-disk",
        [
            "vi-test",
            "--vg",
            "vg-test",
            "--name",
            "disk-test",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
        RAW,
        "disk-test",
    ),
    (
        "delete-disk",
        ["vi-test", "--vg", "vg-test", "--name", "disk-test", "--yes"],
        RAW,
        "disk-test",
    ),
    (
        "attach-disk",
        [
            "lp-test",
            "--vios",
            "vi-test",
            "--vg",
            "vg-test",
            "--name",
            "disk-test",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
        ATTACH,
        "disk-test",
    ),
    (
        "map",
        ["vi-test", "--lpar", "lp-test", "--name", "disk-test", "--yes"],
        StorageMapResult("lp-test", RAW, LOCATION),
        "disk-test",
    ),
    (
        "create-media-repo",
        ["vi-test", "--vg", "vg-test", "--size-mib", "1024", "--yes"],
        RAW,
        "vg-test",
    ),
    (
        "create-media",
        [
            "vi-test",
            "--vg",
            "vg-test",
            "--name",
            "media-test",
            "--size-mib",
            "1024",
            "--yes",
        ],
        RAW,
        "media-test",
    ),
    (
        "delete-media-repo",
        ["vi-test", "--vg", "vg-test", "--yes"],
        "vi-test",
        "vg-test",
    ),
    (
        "delete-media",
        ["vi-test", "--vg", "vg-test", "--name", "media-test", "--yes"],
        RAW,
        "media-test",
    ),
    (
        "mount-optical-media",
        ["vi-test", "--lpar", "lp-test", "--name", "media-test", "--yes"],
        StorageMapResult("lp-test", RAW, LOCATION),
        "media-test",
    ),
    (
        "unmount-optical-media",
        ["vi-test", "--lpar", "lp-test", "--name", "media-test", "--yes"],
        LOCATION,
        "media-test",
    ),
    (
        "detach-mapping",
        ["vi-test", "vhost0/vtscsi0", "--yes"],
        LOCATION,
        "vhost0/vtscsi0",
    ),
    (
        "upload-iso",
        [
            "vi-test",
            "--vg",
            "vg-test",
            "--name",
            "media-test",
            "https://iso.example.test/test.iso",
        ],
        UPLOAD,
        "media-test",
    ),
]


def _invoke(monkeypatch, command, args, data, flags=(), input=None):
    call = Mock(return_value=data)
    monkeypatch.setattr(storage, "with_client", call)
    return RUNNER.invoke(
        cli.app, ["storage", command, *args, *flags], input=input
    ), call


@pytest.mark.parametrize("command,args,data,target", CASES)
def test_default_names_request_and_target_without_raw_dump(
    monkeypatch, command, args, data, target
):
    result, call = _invoke(monkeypatch, command, args, data)
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Field\tValue\n")
    assert f"operation\t{command}\n" in result.stdout
    assert f"target\t{target}\n" in result.stdout
    assert "unrelated-hardware-marker" not in result.stdout
    assert (
        "outcome\tuploaded" if command == "upload-iso" else "outcome\trequest_succeeded"
    ) in result.stdout
    call.assert_called_once()


@pytest.mark.parametrize("command,args,data,target", CASES)
@pytest.mark.parametrize(
    "flags", [("--json",), ("-j",), ("--format", "json"), ("--details",)]
)
def test_json_is_one_document_and_preserves_explicit_envelopes(
    monkeypatch, command, args, data, target, flags
):
    result, _ = _invoke(monkeypatch, command, args, data, flags)
    assert result.exit_code == 0, result.output
    decoded = json.loads(result.stdout)
    if flags == ("--details",) or command in {"attach-disk", "upload-iso"}:
        expected = asdict(data) if is_dataclass(data) else data
        assert decoded == json.loads(json.dumps(expected))
    else:
        assert decoded["operation"] == command
        assert decoded["target"] == target
        assert decoded["outcome"] == "request_succeeded"
        assert decoded["verification"] == "final state not verified"
        assert "unrelated-hardware-marker" not in result.stdout


@pytest.mark.parametrize("command,args,data,target", CASES)
def test_output_conflicts_fail_before_io(monkeypatch, command, args, data, target):
    for flags in [("--json", "--format", "table"), ("--details", "--format", "tsv")]:
        result, call = _invoke(monkeypatch, command, args, data, flags)
        assert result.exit_code == 2
        assert "conflict" in unstyle(result.stderr).lower()
        assert result.stdout == ""
        call.assert_not_called()


@pytest.mark.parametrize("command,args,data,target", CASES[:-1])
@pytest.mark.parametrize("selected", ["json", "tsv"])
def test_confirmed_machine_output_keeps_prompt_on_stderr(
    command, args, data, target, selected
):
    # CliRunner echoes prompt input to stdout; use actual stdin/stdout for this contract.
    args = [arg for arg in args if arg != "--yes"]
    source = f"""
from hmcpctl import cli
from hmcpctl.cli_commands.storage import resources
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.lpar.provision import AttachDiskResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.storage.resources import StorageMapResult
resources.with_client = lambda op: {data!r}
cli.app(prog_name='hmcpctl')
"""
    import pty

    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "rb") as terminal:
            os.write(master, b"y\n")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    source,
                    "storage",
                    command,
                    *args,
                    "--format",
                    selected,
                ],
                stdin=terminal,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
    finally:
        os.close(master)
    assert result.returncode == 0, result.stderr
    if selected == "json":
        json.loads(result.stdout)
    else:
        assert result.stdout.startswith("Field\tValue\n")
    assert "[y/N]" in result.stderr


@pytest.mark.parametrize(
    "command", ["create-media-repo", "delete-media-repo", "delete-media", "delete-disk"]
)
def test_empty_or_idempotent_return_does_not_claim_completion(monkeypatch, command):
    _, args, _, _ = next(case for case in CASES if case[0] == command)
    result, _ = _invoke(monkeypatch, command, args, None, ("--json",))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["outcome"] == "request_succeeded"
    assert json.loads(result.stdout)["verification"] == "final state not verified"


@pytest.mark.parametrize(
    "status,expected,code",
    [
        ("RUNNING", "submitted", 0),
        ("COMPLETED_WITH_WARNINGS", "failed", 1),
        ("COMPLETED_WITH_ERROR", "failed", 1),
        ("COMPLETED_OK", "completed", 0),
    ],
)
@pytest.mark.parametrize("case_index", [2, 9])
def test_returned_job_is_not_opaque_success(
    monkeypatch, status, expected, code, case_index
):
    command, args, _, _ = CASES[case_index]
    resource = {"JobID": "job-test", "Status": status}
    data = (
        StorageMapResult("lp-test", resource, LOCATION)
        if case_index == 9
        else {"Resource": resource}
    )
    result, _ = _invoke(monkeypatch, command, args, data, ("--json",))
    assert result.exit_code == code, result.output
    decoded = json.loads(result.stdout)
    assert decoded["outcome"] == expected
    assert decoded["job_id"] == "job-test"
    assert decoded["job_status"] == status
    detailed, _ = _invoke(monkeypatch, command, args, data, ("--details",))
    assert detailed.exit_code == code, detailed.output
    assert json.loads(detailed.stdout) == (asdict(data) if is_dataclass(data) else data)


def test_attach_partial_preserves_error_and_location_without_successful_raw_step(
    monkeypatch,
):
    data = AttachDiskResult(
        False,
        "lp-test",
        False,
        (
            WorkflowStep("create_disk", "ok", RAW),
            WorkflowStep("storage", "error", "mapping refused"),
        ),
        ("location read uncertain",),
        LOCATION,
    )
    command, args, _, _ = CASES[3]
    result, _ = _invoke(monkeypatch, command, args, data)
    assert result.exit_code == 1
    for evidence in (
        "incomplete",
        "create_disk",
        "mapping refused",
        "location read uncertain",
        "CurrentProfileSync is Disabled",
    ):
        assert evidence in result.stdout
    assert "unrelated-hardware-marker" not in result.stdout


def test_attach_dry_run_names_target_and_validation(monkeypatch):
    data = AttachDiskResult(
        False, "lp-test", True, (WorkflowStep("storage", "dry_run"),), ()
    )
    command, args, _, _ = CASES[3]
    result, _ = _invoke(monkeypatch, command, [*args, "--dry-run"], data)
    assert result.exit_code == 0
    assert "outcome\tdry_run" in result.stdout
    assert "target\tdisk-test" in result.stdout


@pytest.mark.parametrize("index", [4, 9, 10, 11])
def test_mapping_location_and_retention_survive_projection(monkeypatch, index):
    command, args, data, _ = CASES[index]
    result, _ = _invoke(monkeypatch, command, args, data, ("--json",))
    decoded = json.loads(result.stdout)
    assert decoded["change_location"] == asdict(LOCATION)
    assert "CurrentProfileSync is Disabled" in decoded["change_location_summary"]
    if index in {10, 11}:
        assert decoded["backing_storage"] == "retained"


@pytest.mark.parametrize("width", [40, 80, 200])
def test_actual_redirected_json_round_trips_target(tmp_path, width):
    target = "disk-[test]-東京-" * 20 + "\\\t\n"
    code = "from hmcpctl import cli\nfrom hmcpctl.cli_commands.storage import resources\nresources.with_client=lambda op: None\ncli.app(prog_name='hmcpctl')\n"
    destination = tmp_path / "result.json"
    with destination.open("w") as output:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                "storage",
                "delete-disk",
                "vi-test",
                "--vg",
                "vg-test",
                "--name",
                target,
                "--yes",
                "--json",
            ],
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "COLUMNS": str(width)},
            timeout=20,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(destination.read_text())["target"] == target


def test_attach_pending_job_evidence_is_retained_without_claiming_completion(
    monkeypatch,
):
    job = {"Resource": {"JobID": "job-test", "Status": "RUNNING"}}
    data = AttachDiskResult(
        False, "lp-test", False, (WorkflowStep("storage", "ok", job),), ()
    )
    command, args, _, _ = CASES[3]
    result, _ = _invoke(monkeypatch, command, args, data)
    assert result.exit_code == 1
    assert "incomplete" in result.stdout
    assert "job-test" in result.stdout
    assert "RUNNING" in result.stdout
