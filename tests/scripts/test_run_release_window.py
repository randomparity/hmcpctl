"""Offline release-window CLI composition; hardware entry points are replaced."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_test_runner as runner

ROOT = Path(__file__).parents[2]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def window(tmp_path):
    root = tmp_path / "candidate"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(ROOT), str(root)], check=True
    )
    (root / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(root, "switch", "-c", "fixture-window")
    shutil.copyfile(
        ROOT / "scripts/live_test/cli.py", root / "scripts/live_test/cli.py"
    )
    script = ROOT / "scripts/run_release_window.py"
    if script.exists():
        shutil.copyfile(script, root / "scripts/run_release_window.py")
    for name in (
        "live_test_baseline.py",
        "live_test_preflight.py",
        "live_round2.py",
        "live_test_recovery.py",
    ):
        target = root / "scripts" / name
        source = target.read_text()
        target.write_text(
            source.split('if __name__ == "__main__":')[0]
            + 'if __name__ == "__main__":\n    raise SystemExit("offline hardware boundary")\n'
        )
    claims = root / "docs/capabilities/releases/v0.0.0.json"
    claims.write_text(
        json.dumps(
            {
                "format_version": 1,
                "operations": [
                    {
                        "operation": "system.get",
                        "arm": "round2",
                        "scenario": "st1-system-inventory",
                    }
                ],
            }
        )
    )
    git(root, "add", "scripts", str(claims))
    git(root, "commit", "-qm", "Prepare offline window fixture")
    defaults = runner.LiveTestConfig()
    values = {
        key: ",".join(value)
        if isinstance(value := getattr(defaults, field), tuple)
        else str(value)
        for key, field in runner.LiveTestConfig._CONFIG_FIELDS.items()
    }
    (root / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items())
    )
    output = tmp_path / "private"
    output.mkdir(mode=0o700)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env.update(
        HMC_HOST="hmc.example.test",
        HMC_USER="fixture",
        HMC_PASSWORD="fixture",  # pragma: allowlist secret — offline boundary fixture
        HMC_SCHEMA_VERSION="V1_0",
        PYTHONPATH=str(root / "src"),
        UV_NO_SYNC="1",
    )
    return root, claims, output, env


def invoke(window, *extra):
    root, claims, output, env = window
    return subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_release_window.py"),
            "--claims",
            str(claims),
            "--branch",
            "promotion/window",
            "--output-dir",
            str(output),
            *extra,
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_dry_run_derives_arms_without_hardware_or_writes(window):
    root, _, output, _ = window
    before = git(root, "rev-parse", "HEAD")
    result = invoke(window, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["cli_coverage"]["system.get"].startswith("gap:")
    assert plan["authorization"]["arms"] == ["round2"]
    assert plan["authorization"]["authorized"] is False
    assert plan["authorization"]["vios_access"] is False
    assert "hp14" in str(plan["authorization"]["mutation_boundary"])
    assert not list(output.iterdir())
    assert git(root, "rev-parse", "HEAD") == before
    assert not list(root.glob("test-results-*.json"))


def test_missing_authorization_refuses_before_hardware(window):
    result = invoke(window)
    assert result.returncode == 2
    assert "authorization" in result.stderr.lower()
    assert "offline hardware boundary" not in result.stderr
    assert not list(window[2].iterdir())


BOUNDARY = r"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
import check_capability_inventory as inventory
import live_test_baseline as baseline
import live_test_runner as runner

def run(phase):
    root = Path.cwd()
    args = sys.argv[1:]
    if phase == "baseline":
        phase = "baseline-compare" if args[0] == "compare" else "baseline-" + Path(args[args.index("--output") + 1]).stem.rsplit("-", 1)[1]
    with Path(os.environ["STUB_EVENTS"]).open("a") as stream:
        stream.write(phase + "\n")
    print("PRIVATE-CHILD-MARKER")
    if os.environ.get("STUB_FAIL") == phase:
        return 1
    if phase == "arm" and os.environ.get("STUB_INTERRUPT"):
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(60)
    if phase == "recovery" and os.environ.get("STUB_INTERRUPT"):
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(0.01)
        os.kill(os.getppid(), signal.SIGINT)
    if phase.startswith("baseline-"):
        if phase == "baseline-compare":
            return baseline.main(args)
        system = args[args.index("--system") + 1]
        ids = [int(args[i+1]) for i, arg in enumerate(args) if arg == "--vios-id"]
        tables = {name: [] for name in baseline.TABLES}
        tables["system"] = [{"name": system, "type_model": "9009-42A", "serial_num": "anonymous"}]
        tables["partitions"] = [{"lpar_id": "7", "name": "vios-R1", "lpar_env": "vioserver", "state": "Running"}]
        if ids:
            tables["vios"] = [{"id": "7", "name": "vios-R1", "uuid": "00000000-0000-4000-8000-000000000007"}]
        baseline.write_snapshot(Path(args[args.index("--output") + 1]), {"version": 1, "scope": {"host": "hmc.example.test", "system": system, "vios_ids": ids}, "tables": tables})
    if phase == "arm":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        now = datetime.now(UTC)
        observed = (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tool = next(tool for tool in inventory.discover_registry() if tool.operation == "system.get")
        observation = {"id": "st1-hmc-get-system", "channel": "live", "result": "passed", "scenario": "st1-system-inventory", "tested_commit": head, "observed_at": observed, "hmc_release": "V10R3", "hardware_family": "POWER10", "schema_version": "V1_0", "cleanup": "not-required", "closure_fingerprint": inventory.closure_fingerprint(root, tool.handler.rsplit(".", 1)[0]), "assertions": ["response-validated"]}
        result = {"run": {"tested_commit": head, "tree_clean": True, "group": "round2", "subtasks": runner.SUBTASK_GROUPS["round2"], "schema_version": "V1_0", "finished": now.isoformat(), "partial": False}, "config": {"private": "PRIVATE-CHILD-MARKER"}, "results": [{"subtask": 1, "tool": "hmc_get_system", "status": "PASS", "result": "passed", "timestamp": observed, "note": "", "data": {}}]}
        Path("test-results-round2.json").write_text(json.dumps(result))
        Path("test-results-round2-observations.json").write_text(json.dumps([{"operation": "system.get", "observation": observation}]))
    return 0
"""


def enable_boundaries(window, tmp_path):
    root, _, _, env = window
    helper = root / "tests/window_boundary.py"
    helper.write_text(BOUNDARY)
    for name, phase in {
        "live_test_baseline.py": "baseline",
        "live_test_preflight.py": "preflight",
        "live_round2.py": "arm",
        "live_test_recovery.py": "recovery",
    }.items():
        target = root / "scripts" / name
        source = target.read_text().split('if __name__ == "__main__":')[0]
        target.write_text(
            source
            + '\nif __name__ == "__main__":\n    from pathlib import Path\n    import sys\n    sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))\n    from window_boundary import run\n    raise SystemExit(run('
            + repr(phase)
            + "))\n"
        )
    git(root, "add", "scripts", str(helper))
    git(root, "commit", "-qm", "Install offline transport boundaries")
    events = tmp_path / "events"
    env["STUB_EVENTS"] = str(events)
    return events


def approval(window, tmp_path):
    result = invoke(window, "--dry-run")
    assert result.returncode == 0, result.stderr
    auth = json.loads(result.stdout)["authorization"]
    auth.update(authorized=True, vios_access=True)
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(auth))
    return path


def summary(window):
    paths = list(window[2].glob("window-*/summary.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text()), paths[0].parent


def test_real_cli_promotes_only_after_complete_window(window, tmp_path):
    root, _, _, _ = window
    events = enable_boundaries(window, tmp_path)
    candidate = git(root, "rev-parse", "HEAD")
    auth = approval(window, tmp_path)
    result = invoke(window, "--authorization", str(auth))
    report, output = summary(window)
    assert result.returncode == 0, (
        result.stderr,
        report,
        [(p.name, p.read_text()[-2000:]) for p in output.glob("*.log")],
    )
    assert report["cli_coverage"]["system.get"].startswith("gap:")
    assert report["passed"] is True
    assert events.read_text().splitlines() == [
        "baseline-discovery",
        "baseline-before",
        "preflight",
        "arm",
        "recovery",
        "baseline-after",
        "baseline-compare",
    ]
    assert [phase["phase"] for phase in report["phases"]][-3:] == [
        "round2-evidence",
        "promotion",
        "claimed-set-gate",
    ]
    assert git(root, "branch", "--show-current") == "promotion/window"
    assert git(root, "rev-parse", "fixture-window") == candidate
    assert git(root, "status", "--porcelain") == ""
    assert (
        "PRIVATE-CHILD-MARKER"
        not in result.stdout + result.stderr + (output / "summary.json").read_text()
    )
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert (output.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize(
    "phase",
    [
        "baseline-discovery",
        "preflight",
        "arm",
        "recovery",
        "baseline-after",
        "baseline-compare",
    ],
)
def test_phase_failure_stops_and_arm_failure_still_recovers(window, tmp_path, phase):
    events = enable_boundaries(window, tmp_path)
    auth = approval(window, tmp_path)
    window[3]["STUB_FAIL"] = phase
    result = invoke(window, "--authorization", str(auth))
    assert result.returncode != 0
    report, _ = summary(window)
    assert report["passed"] is False
    actual = events.read_text().splitlines()
    assert actual[-1] == ("recovery" if phase == "arm" else phase)
    assert not any(item["phase"] == "promotion" for item in report["phases"])
    assert git(window[0], "branch", "--show-current") == "fixture-window"


def test_parent_interrupt_reaps_arm_then_runs_recovery(window, tmp_path):
    events = enable_boundaries(window, tmp_path)
    auth = approval(window, tmp_path)
    window[3]["STUB_INTERRUPT"] = "1"
    result = invoke(window, "--authorization", str(auth))
    assert result.returncode != 0
    report, _ = summary(window)
    assert report["interrupted"] is True
    assert events.read_text().splitlines()[-2:] == ["arm", "recovery"]
    assert not any(item["phase"] == "promotion" for item in report["phases"])


@pytest.mark.parametrize(
    "field",
    ["candidate", "arms", "mutation_boundary", "authorized", "vios_access", "extra"],
)
def test_authorization_mismatch_refuses_before_any_hardware(window, tmp_path, field):
    events = enable_boundaries(window, tmp_path)
    auth = approval(window, tmp_path)
    data = json.loads(auth.read_text())
    data[field] = False if field in {"authorized", "vios_access"} else "different"
    auth.write_text(json.dumps(data))
    result = invoke(window, "--authorization", str(auth))
    assert result.returncode == 2
    assert not events.exists()
    assert not list(window[2].iterdir())


def test_cancellation_before_dispatch_never_starts_another_child(tmp_path, monkeypatch):
    import run_release_window as window_command

    execution = window_command.Execution(tmp_path, tmp_path)
    execution.cancelled = True
    monkeypatch.setattr(
        window_command.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("cancelled window started a child"),
    )
    assert execution.run("not-started", ["no-such-script"]) is False


def test_summary_write_failure_cannot_return_success(tmp_path, monkeypatch):
    import run_release_window as window_command

    root, output = tmp_path / "root", tmp_path / "private"
    root.mkdir()
    output.mkdir(mode=0o700)
    plan = {
        "root": root,
        "authorization": {"candidate": "candidate", "arms": ["round2"]},
    }
    monkeypatch.setattr(window_command, "prepare", lambda args: plan)
    monkeypatch.setattr(window_command, "authorize", lambda *args: None)
    monkeypatch.setattr(window_command, "run_window", lambda *args: True)
    original = Path.open

    def open_path(self, *args, **kwargs):
        if self.name == "summary.json":
            raise OSError("disk failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    assert (
        window_command.main(
            [
                "--claims",
                "unused",
                "--branch",
                "promotion/test",
                "--output-dir",
                str(output),
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "content", ['{"authorized":true,"authorized":false}', "x" * 65537]
)
def test_authorization_json_rejects_duplicate_or_oversized_input(tmp_path, content):
    import run_release_window as command

    path = tmp_path / "authorization.json"
    path.write_text(content)
    with pytest.raises(command.WindowError):
        command.read_json(path, 65536)


@pytest.mark.parametrize("kind", ["public", "inside", "symlink"])
def test_output_directory_requires_physical_private_external_path(tmp_path, kind):
    import run_release_window as command

    root = tmp_path / "checkout"
    root.mkdir()
    output = tmp_path / "private"
    output.mkdir(mode=0o700)
    if kind == "public":
        output.chmod(0o755)
    elif kind == "inside":
        output = root / "inside"
        output.mkdir(mode=0o700)
    else:
        link = tmp_path / "link"
        link.symlink_to(output, target_is_directory=True)
        output = link
    with pytest.raises(command.WindowError):
        command.private_directory(output, root)


def test_child_output_is_bounded_and_truncation_fails(tmp_path, monkeypatch):
    import run_release_window as command

    monkeypatch.setattr(command, "MAX_LOG", 128)
    execution = command.Execution(tmp_path, tmp_path)
    assert not execution.run("noisy", ["-c", 'print("x" * 4096)'])
    assert (tmp_path / "noisy.log").stat().st_size == 128
    assert execution.phases[-1]["output_truncated"] is True
    assert execution.safe


def test_arm_spawn_failure_still_attempts_recovery(tmp_path, monkeypatch):
    import run_release_window as command

    calls = []
    execution = command.Execution(tmp_path, tmp_path)
    actual = command.subprocess.Popen

    def spawn(arguments, **kwargs):
        calls.append(arguments)
        if arguments[-1] == "scripts/live_round2.py":
            raise OSError("spawn refused")
        return actual([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(command.subprocess, "Popen", spawn)
    plan = {"root": tmp_path, "systems": [], "authorization": {"arms": ["round2"]}}
    assert command.run_window(plan, execution) is False
    assert [item["phase"] for item in execution.phases] == [
        "round2-preflight",
        "round2-arm",
        "round2-recovery",
    ]
    assert calls[-1][1] == "scripts/live_test_recovery.py"


def test_vios_arm_refuses_missing_independent_vios_inventory(tmp_path, monkeypatch):
    import run_release_window as command

    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 1,
                "scope": {"host": "h", "system": "system-R1", "vios_ids": []},
                "tables": {
                    name: (
                        [
                            {
                                "name": "system-R1",
                                "type_model": "9009-42A",
                                "serial_num": "anonymous",
                            }
                        ]
                        if name == "system"
                        else []
                    )
                    for name in command.baseline.TABLES
                },
            }
        )
    )
    monkeypatch.setattr(command, "capture", lambda *args: snapshot)
    execution = command.Execution(tmp_path, tmp_path)
    monkeypatch.setattr(
        execution,
        "run",
        lambda *args, **kwargs: pytest.fail("missing VIOS permitted arm"),
    )
    plan = {
        "root": tmp_path,
        "systems": ["system-R1"],
        "authorization": {"arms": ["round2"]},
    }
    with pytest.raises(command.WindowError, match="VIOS"):
        command.run_window(plan, execution)


def test_descendant_holding_output_open_cannot_report_clean(tmp_path, monkeypatch):
    import run_release_window as command

    monkeypatch.setattr(command, "GRACE", 0.1)
    execution = command.Execution(tmp_path, tmp_path)
    script = 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])'
    assert not execution.run("orphan", ["-c", script])
    assert execution.safe


@pytest.mark.parametrize(
    "condition",
    [
        "main",
        "detached",
        "dirty",
        "stale",
        "branch-exists",
        "claims-symlink",
        "default-unknown",
    ],
)
def test_candidate_refusals_never_start_hardware(window, condition):
    root, claims, output, _ = window
    if condition == "main":
        git(root, "switch", "-C", "main")
    elif condition == "detached":
        git(root, "checkout", "--detach")
    elif condition == "dirty":
        (root / "unexpected").write_text("dirty")
    elif condition == "stale":
        (root / "test-results-vmedia.json").write_text("{}")
    elif condition == "branch-exists":
        git(root, "branch", "promotion/window")
    elif condition == "default-unknown":
        git(root, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    else:
        original = root / "docs/capabilities/releases/fixture-original.json"
        claims.rename(original)
        claims.symlink_to(original.name)
        git(root, "add", str(claims), str(original))
        git(root, "commit", "-qm", "Use symlink claims")
    result = invoke(window, "--dry-run")
    assert result.returncode == 2
    assert "offline hardware boundary" not in result.stderr
    assert not list(output.iterdir())


@pytest.mark.parametrize("failed", ["round2-evidence", "promotion", "claimed-set-gate"])
def test_post_recovery_phase_failure_stops_exactly_at_failed_phase(
    tmp_path, monkeypatch, failed
):
    import run_release_window as command

    execution = command.Execution(tmp_path, tmp_path)
    phases = []

    def run(label, *args, **kwargs):
        phases.append(label)
        return label != failed

    monkeypatch.setattr(execution, "run", run)
    monkeypatch.setattr(command, "git", lambda *args: "candidate")
    plan = {
        "root": tmp_path,
        "systems": [],
        "claims": tmp_path / "claims",
        "branch": "promotion/test",
        "authorization": {"arms": ["round2"], "candidate": "candidate"},
    }
    assert not command.run_window(plan, execution)
    expected = [
        "round2-preflight",
        "round2-arm",
        "round2-recovery",
        "round2-evidence",
        "promotion",
        "claimed-set-gate",
    ]
    assert phases == expected[: expected.index(failed) + 1]


def test_unclean_first_recovery_never_dispatches_second_arm(tmp_path, monkeypatch):
    import run_release_window as command

    execution = command.Execution(tmp_path, tmp_path)
    phases = []

    def run(label, *args, **kwargs):
        phases.append(label)
        return label != "round2-recovery"

    monkeypatch.setattr(execution, "run", run)
    plan = {
        "root": tmp_path,
        "systems": [],
        "authorization": {"arms": ["round2", "profiles"]},
    }
    assert not command.run_window(plan, execution)
    assert phases == ["round2-preflight", "round2-arm", "round2-recovery"]


def test_profiles_requires_explicit_vios_authorization(tmp_path):
    import run_release_window as command

    plan = {
        "authorization": {
            "candidate": "candidate",
            "arms": ["profiles"],
            "mutation_boundary": {"profiles": ["VIOS MSP"]},
            "authorized": False,
            "vios_access": False,
        }
    }
    path = tmp_path / "approval.json"
    data = dict(plan["authorization"], authorized=True)
    path.write_text(json.dumps(data))
    with pytest.raises(command.WindowError):
        command.authorize(plan, path)
    data["vios_access"] = True
    path.write_text(json.dumps(data))
    command.authorize(plan, path)


def test_cli_coverage_requires_exact_complete_candidate():
    import run_release_window as window_module
    from live_test import cli
    from live_test.observation import Assertion

    run = runner.RunState()
    run.record_verified(
        35,
        cli.LABELS["job.get"],
        operation="job.get",
        scenario=cli.SCENARIO,
        assertions=[Assertion("cli-job-matches-completed-activation", True)],
        cleanup="not-required",
        data={},
        surface="cli",
    )
    document = {
        "run": {
            "tested_commit": "a" * 40,
            "tree_clean": True,
            "partial": False,
            "group": "bare-cec",
            "subtasks": runner.SUBTASK_GROUPS["bare-cec"],
        },
        "results": run.results,
    }
    assert window_module.cli_coverage(["job.get", "system.get"], [], "a" * 40) == {
        "job.get": "gap: CLI not verified in this window",
        "system.get": "gap: CLI not verified in this window",
    }
    assert (
        window_module.cli_coverage(["job.get"], [document], "a" * 40)["job.get"]
        == "exercised: bare-cec CLI at candidate"
    )
    for key, value in [
        ("tested_commit", "b" * 40),
        ("tree_clean", False),
        ("partial", True),
        ("group", "round2"),
    ]:
        changed = {**document, "run": {**document["run"], key: value}}
        assert window_module.cli_coverage(["job.get"], [changed], "a" * 40)[
            "job.get"
        ].startswith("gap:")
    run.results[0]["result"] = "failed"
    assert window_module.cli_coverage(["job.get"], [document], "a" * 40)[
        "job.get"
    ].startswith("gap:")


def test_window_collects_bare_cec_cli_coverage_after_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import run_release_window as window_module
    from live_test import cli
    from live_test.observation import Assertion

    candidate = "a" * 40
    run = runner.RunState()
    run.record_verified(
        35,
        cli.LABELS["job.get"],
        operation="job.get",
        scenario=cli.SCENARIO,
        assertions=[Assertion("cli-job-matches-completed-activation", True)],
        cleanup="not-required",
        data={},
        surface="cli",
    )
    (tmp_path / "test-results-bare-cec.json").write_text(
        json.dumps(
            {
                "run": {
                    "tested_commit": candidate,
                    "tree_clean": True,
                    "partial": False,
                    "group": "bare-cec",
                    "subtasks": runner.SUBTASK_GROUPS["bare-cec"],
                },
                "results": run.results,
            }
        )
    )
    tables = {name: [] for name in window_module.baseline.TABLES}
    tables["system"] = [
        {"name": "system", "type_model": "8375-42A", "serial_num": "anonymous"}
    ]
    snapshot = tmp_path / "snapshot.json"
    window_module.baseline.write_snapshot(
        snapshot,
        {
            "version": 1,
            "scope": {"host": "hmc.example.test", "system": "system", "vios_ids": []},
            "tables": tables,
        },
    )
    monkeypatch.setattr(window_module, "capture", lambda *args: snapshot)
    monkeypatch.setattr(window_module, "git", lambda *args: candidate)
    phases = []

    def child(label, *args, **kwargs):
        phases.append(label)
        return True

    execution = SimpleNamespace(run=child, safe=True, cancelled=False)
    plan = {
        "root": tmp_path,
        "claims": tmp_path / "claims.json",
        "branch": "promotion/test",
        "systems": ["system"],
        "authorization": {"arms": ["bare-cec"], "candidate": candidate},
        "cli_coverage": window_module.cli_coverage(
            ["job.get", "system.get"], [], candidate
        ),
    }
    assert window_module.run_window(plan, execution)
    assert plan["cli_coverage"] == {
        "job.get": "exercised: bare-cec CLI at candidate",
        "system.get": "gap: CLI not verified in this window",
    }
    assert phases.index("bare-cec-recovery") < phases.index("promotion")
