"""Boundary tests for the fixed hosted measurement; never run real hosted services."""

import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[2]


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "measure_test_cost", ROOT / "scripts/measure_test_cost.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    return module


def xml(path: Path, name: str = "case", child: str = "") -> None:
    path.write_text(
        "<testsuites><testsuite>"
        f'<testcase classname="tests.app.x" name="{name}">{child}</testcase>'
        "</testsuite></testsuites>"
    )


def test_service_owns_fresh_bounded_same_user_cgroup(observer, monkeypatch):
    observer.OUTPUT.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        observer,
        "capture_log",
        lambda command, directory, timeout: calls.append((command, timeout)),
    )
    assert observer.service(1, 720) >= 0
    command, timeout = calls[0]
    for flag in (
        "--property=MemoryAccounting=yes",
        "--property=MemoryMax=12G",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=5",
        "--property=RuntimeMaxSec=720",
        (
            f"--setenv=PATH={Path(observer.sys.executable).parent}:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        f"--uid={observer.os.getuid()}",
    ):
        assert flag in command
    assert command[:3] == ["sudo", "-n", "systemd-run"]
    assert command[-2:] == ["--inside", "1"]
    assert timeout == 735


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "service"),
        subprocess.TimeoutExpired("service", 735),
    ],
)
def test_service_failure_stops_instead_of_retrying(observer, monkeypatch, error):
    observer.OUTPUT.mkdir(parents=True)
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(observer, "capture_log", fail)
    with pytest.raises(type(error)):
        observer.service(1, 720)
    assert len(calls) == 1


@pytest.mark.parametrize("slot", range(1, 9))
def test_fixed_inside_modes_reuse_application_selector(observer, monkeypatch, slot):
    directory = observer.OUTPUT / str(slot)
    directory.mkdir(parents=True)
    monkeypatch.setattr(observer, "resource_context", lambda: {"peak_bytes": 42})
    calls = []
    monkeypatch.setattr(pytest, "main", lambda args: calls.append(args) or 0)
    with pytest.raises(SystemExit) as result:
        observer.inside(slot)
    assert result.value.code == 0
    expected = (
        (2, True),
        (2, False),
        (3, True),
        (4, True),
        (4, True),
        (3, True),
        (2, False),
        (2, True),
    )
    assert observer.SAMPLES == expected
    workers, covered = expected[slot - 1]
    args = calls[0]
    selected = observer._group_arguments("application")
    assert args[: len(selected)] == selected
    assert args[args.index("-n") + 1] == str(workers)
    assert "--dist=loadfile" in args and "--max-worker-restart=0" in args
    assert "--cov-fail-under=0" in args
    assert ("--no-cov" in args) is (not covered)
    assert json.loads((directory / "resources.json").read_text())["peak_bytes"] == 42


@pytest.mark.parametrize(
    "payload",
    [
        "<testsuites/>",
        '<testcase classname="a" name="b"><failure/></testcase>',
        '<testcase classname="a" name="b"><error/></testcase>',
        (
            '<testsuites><testcase classname="a" name="b"/>'
            '<testcase classname="a" name="b"/></testsuites>'
        ),
    ],
)
def test_unusable_cases_are_rejected(observer, tmp_path, payload):
    path = tmp_path / "junit.xml"
    path.write_text(payload)
    with pytest.raises(ValueError):
        observer.junit_result(path)


@pytest.mark.parametrize("branch,files", [(False, {"x": {}}), (True, {})])
def test_coverage_requires_branch_and_nonempty_files(observer, tmp_path, branch, files):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"meta": {"branch_coverage": branch}, "files": files}))
    with pytest.raises(ValueError):
        observer.coverage_result(path)


@pytest.mark.parametrize(
    "mismatch", ["identity", "skipped", "coverage", "peak", "none"]
)
def test_measurement_validates_matching_inputs_and_stops(
    observer, monkeypatch, mismatch
):
    observer.OUTPUT.mkdir(parents=True)
    (observer.OUTPUT / "metadata.json").write_text(
        '{"head":"fixed","inputs":{"workload":"fixed"}}'
    )
    monkeypatch.setattr(
        observer, "input_fingerprints", lambda: {"workload": "fixed"}, raising=False
    )
    calls = []

    def sample(slot, timeout):
        calls.append(slot)
        directory = observer.OUTPUT / str(slot)
        directory.mkdir(exist_ok=True)
        xml(
            directory / "junit.xml",
            "changed" if mismatch == "identity" and slot == 2 else "case",
            "<skipped/>" if mismatch == "skipped" and slot == 2 else "",
        )
        (directory / "coverage.json").write_text(
            json.dumps(
                {
                    "meta": {"branch_coverage": True},
                    "files": {"x": {"lines": [slot if mismatch == "coverage" else 1]}},
                }
            )
        )
        (directory / "resources.json").write_text(
            json.dumps({"peak_bytes": 0 if mismatch == "peak" else 42})
        )
        assert timeout == 720
        return 1.25

    monkeypatch.setattr(observer, "service", sample)
    if mismatch == "none":
        observer.measure()
        assert calls == list(range(1, 9))
        assert (
            len(json.loads((observer.OUTPUT / "samples.json").read_text())["rows"]) == 8
        )
    else:
        with pytest.raises(ValueError):
            observer.measure()
        assert (
            len(calls)
            == {"identity": 2, "skipped": 2, "coverage": 3, "peak": 1}[mismatch]
        )


def test_workflow_is_isolated_fixed_and_read_only():
    data = yaml.load(
        (ROOT / ".github/workflows/coverage-worker-cost.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert data["permissions"] == {"contents": "read"}
    assert set(data["on"]) == {"push", "workflow_dispatch"}
    job = data["jobs"]["observe"]
    assert job["if"] == (
        "(github.event_name == 'workflow_dispatch' && inputs.measure) || "
        "(github.event_name == 'push' && "
        "contains(github.event.head_commit.message, '[diagnose-2000]'))"
    )
    assert job["timeout-minutes"] == "60"
    assert job["strategy"]["matrix"] == {"python": ["3.12"]}
    assert job["strategy"]["max-parallel"] == "1"
    steps = job["steps"]
    long = next(step for step in steps if step.get("run", "").endswith("--measure"))
    assert "if" not in long
    assert data["on"]["push"]["branches"] == ["feat/coverage-worker-cost-2000"]
    upload = steps[-1]["with"]
    assert upload["path"].splitlines() == [
        "dist/test-cost/metadata.json",
        "dist/test-cost/samples.json",
        "dist/test-cost/*/resources.json",
        "dist/test-cost/*/observation.json",
        "dist/test-cost/*/junit.xml",
        "dist/test-cost/*/coverage.json",
        "dist/test-cost/*/pytest.log",
        "dist/test-cost/*/log.json",
    ]
    assert steps[0]["with"]["persist-credentials"] == "false"
    for step in steps:
        if "uses" in step:
            _, pin = step["uses"].split("@")
            assert len(pin) == 40 and set(pin) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "membership",
        "affinity",
        "quota",
        "period",
        "memory",
        "memory-unit",
        "memory-shape",
        "escape",
    ],
)
def test_fresh_resource_admission(observer, monkeypatch, tmp_path, fault):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    membership = (
        "bad"
        if fault == "membership"
        else "0::/../escape"
        if fault == "escape"
        else "0::/sample"
    )
    (proc / "self/cgroup").write_text(membership)
    (proc / "meminfo").write_text(
        f"MemAvailable: {1 if fault == 'memory' else 16 * 1024**2} kB\n"
    )
    if fault == "memory-unit":
        (proc / "meminfo").write_text("MemAvailable: 17179869184 B\n")
    if fault == "memory-shape":
        (proc / "meminfo").write_text("MemAvailable: 17179869184\n")
    root = tmp_path / "cgroup"
    group = root / "sample"
    group.mkdir(parents=True)
    for directory in (group, root):
        (directory / "cpu.max").write_text(
            "200000 100000" if fault == "quota" else "max 100000"
        )
        if fault == "period":
            (directory / "cpu.max").write_text("max 0")
        (directory / "memory.max").write_text("12884901888")
        (directory / "memory.current").write_text("123")
    (group / "memory.peak").write_text("456")
    mapping = {
        "/proc/self/cgroup": proc / "self/cgroup",
        "/proc/meminfo": proc / "meminfo",
        "/sys/fs/cgroup": root,
    }
    monkeypatch.setattr(
        observer, "Path", lambda value: mapping.get(str(value), Path(value))
    )
    monkeypatch.setattr(
        observer.os,
        "sched_getaffinity",
        lambda pid: set(range(2 if fault == "affinity" else 4)),
        raising=False,
    )
    if fault == "none":
        result = observer.resource_context()
        assert result["peak_bytes"] == 456 and result["affinity_cpus"] == 4
        assert len(result["ancestors"]) == 2
    else:
        with pytest.raises(ValueError):
            observer.resource_context()


@pytest.mark.parametrize(
    "fault",
    [
        "host",
        "architecture",
        "version",
        "actions",
        "source",
        "none",
        "runtime-python",
        "runtime-packages",
        "runtime-lock",
        "runtime-core",
    ],
)
def test_preflight_binds_supported_host_and_commit(observer, monkeypatch, fault):
    baseline = {
        "python": observer.sys.version,
        "packages": {
            name: observer.importlib.metadata.version(name)
            for name in ("pytest", "pytest-cov", "pytest-xdist", "coverage")
        },
        "core": "CTracer",
        "lock": "locked",
    }
    actual = baseline | {
        "runtime-python": {"python": "changed"},
        "runtime-packages": {
            "packages": {name: "changed" for name in baseline["packages"]}
        },
        "runtime-lock": {"lock": "changed"},
        "runtime-core": {"core": "changed"},
    }.get(fault, {})
    expected_runtime = {
        "python": baseline["python"],
        "packages": baseline["packages"],
        "lock_sha256": observer.hashlib.sha256(baseline["lock"].encode()).hexdigest(),
        "coverage_core": baseline["core"],
    }
    monkeypatch.setattr(
        observer, "ORIGINAL_RUNTIME_SHA256", observer.digest(expected_runtime)
    )
    monkeypatch.setattr(
        observer,
        "importlib",
        SimpleNamespace(
            metadata=SimpleNamespace(
                version=lambda name: (
                    "0.2.1.dev159+g123456789"
                    if name == "hmcpctl"
                    else actual["packages"][name]
                )
            ),
        ),
    )
    monkeypatch.setattr(
        observer,
        "platform",
        SimpleNamespace(
            system=lambda: "Darwin" if fault == "host" else "Linux",
            machine=lambda: "aarch64" if fault == "architecture" else "x86_64",
            platform=lambda: "controlled Linux amd64",
        ),
    )
    monkeypatch.setattr(
        observer,
        "sys",
        SimpleNamespace(
            version=actual["python"],
            version_info=SimpleNamespace(
                major=3, minor=14 if fault == "version" else 11
            ),
        ),
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "false" if fault == "actions" else "true")
    monkeypatch.setenv("GITHUB_SHA", "different" if fault == "source" else "fixed")
    monkeypatch.setattr(
        observer.subprocess, "check_output", lambda *args, **kwargs: "fixed\n"
    )
    Path("uv.lock").write_text(str(actual["lock"]))

    def probe(slot, timeout):
        assert slot == 0 and timeout == 20
        directory = observer.OUTPUT / "0"
        directory.mkdir()
        (directory / "resources.json").write_text(
            json.dumps({"peak_bytes": 123, "coverage_core": actual["core"]})
        )
        return 0.1

    monkeypatch.setattr(observer, "service", probe)
    monkeypatch.setattr(observer, "input_fingerprints", lambda: {"workload": "fixed"})
    if fault == "none":
        assert observer.preflight()["head"] == "fixed"
        assert (observer.OUTPUT / "metadata.json").is_file()
        assert (
            json.loads((observer.OUTPUT / "metadata.json").read_text())[
                "hmcpctl_version"
            ]
            == "0.2.1.dev159+g123456789"
        )
    else:
        with pytest.raises(ValueError):
            observer.preflight()
        assert (observer.OUTPUT / "metadata.json").is_file() is fault.startswith(
            "runtime-"
        )


@pytest.mark.parametrize("found", ["own", "missing", "foreign", "core-error"])
def test_same_service_probe_requires_own_console_and_observes_core(
    observer, monkeypatch, found
):
    import coverage

    directory = observer.OUTPUT / "0"
    directory.mkdir(parents=True)
    monkeypatch.setattr(observer, "resource_context", lambda: {"peak_bytes": 42})
    monkeypatch.setattr(
        observer.shutil,
        "which",
        lambda name: (
            None
            if found == "missing"
            else "/foreign/bin/hmcpctl"
            if found == "foreign"
            else str(Path(observer.sys.prefix) / "bin/hmcpctl")
        ),
    )
    stopped = []

    def info():
        if found == "core-error":
            raise ValueError("core unavailable")
        return [("core", "CTracer")]

    probe = SimpleNamespace(
        start=lambda: None, stop=lambda: stopped.append(True), sys_info=info
    )
    monkeypatch.setattr(coverage, "Coverage", lambda **kwargs: probe)
    if found == "own":
        observer.inside(0)
        assert (
            json.loads((directory / "resources.json").read_text())["coverage_core"]
            == "CTracer"
        )
        assert stopped == [True]
    elif found == "core-error":
        with pytest.raises(ValueError, match="core unavailable"):
            observer.inside(0)
        assert stopped == [True]
    else:
        with pytest.raises(ValueError, match="service PATH"):
            observer.inside(0)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_capture_drains_and_bounds_logs_without_changing_child_exit(
    observer, tmp_path, exit_code
):
    command = [
        sys.executable,
        "-c",
        (
            "import sys; sys.stdout.buffer.write(b'first'+b'x'*3000000+b'last'); "
            f"sys.stdout.flush(); sys.exit({exit_code})"
        ),
    ]
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as error:
            observer.capture_log(command, tmp_path, 5)
        assert error.value.returncode == 7
    else:
        observer.capture_log(command, tmp_path, 5)
    output = (tmp_path / "pytest.log").read_bytes()
    assert output.startswith(b"first") and output.endswith(b"last")
    assert len(output) <= 2 * 1024**2 + 128
    counts = json.loads((tmp_path / "log.json").read_text())
    assert counts["output_bytes"] == 3000009
    assert counts["omitted_bytes"] == 3000009 - 2 * 1024**2


def test_capture_timeout_retains_partial_log_and_reaps_child(
    observer, monkeypatch, tmp_path
):
    children = []
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(
        observer, "subprocess", SimpleNamespace(**(vars(subprocess) | {"Popen": spawn}))
    )
    command = [
        sys.executable,
        "-c",
        "import time; print('started', flush=True); time.sleep(10)",
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        observer.capture_log(command, tmp_path, 0.1)
    assert (tmp_path / "pytest.log").is_file()
    assert (tmp_path / "log.json").is_file()
    assert len(children) == 1 and children[0].poll() is not None


def test_capture_preserves_complete_small_stderr(observer, tmp_path):
    observer.capture_log(
        [sys.executable, "-c", "import sys; sys.stderr.write('detail')"], tmp_path, 5
    )
    assert (tmp_path / "pytest.log").read_text() == "detail"
    assert json.loads((tmp_path / "log.json").read_text()) == {
        "output_bytes": 6,
        "omitted_bytes": 0,
    }


def test_input_fingerprint_tracks_contents_and_excludes_only_observer_test(
    observer, monkeypatch
):
    Path("src").mkdir()
    Path("src/input.py").write_text("original")
    Path(".github/workflows").mkdir(parents=True)
    Path(".github/workflows/coverage-worker-cost.yml").write_text("workflow")
    calls = []

    def inventory(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 10
        return b"src/input.py\0tests/scripts/test_measure_test_cost.py\0"

    monkeypatch.setattr(observer.subprocess, "check_output", inventory)
    first = observer.input_fingerprints()
    Path("src/input.py").write_text("changed")
    second = observer.input_fingerprints()
    assert first["workload_sha256"] != second["workload_sha256"]
    assert set(first["files"]) == {"src/input.py"}
    assert first["observer_sha256"] == second["observer_sha256"]
    assert first["selector"] == observer._group_arguments("application")
    assert calls[0] == [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        "src",
        "tests",
        "scripts/run_tests.py",
        "pyproject.toml",
        "uv.lock",
    ]


def test_input_fingerprint_rejects_empty_inventory(observer, monkeypatch):
    monkeypatch.setattr(
        observer.subprocess, "check_output", lambda *args, **kwargs: b""
    )
    with pytest.raises(ValueError, match="nonempty"):
        observer.input_fingerprints()


@pytest.mark.parametrize(
    "failure,error_type,expected_errors",
    [
        ("timeout", subprocess.TimeoutExpired, {"service"}),
        ("service", subprocess.CalledProcessError, {"service"}),
        ("junit", ET.ParseError, {"identity_actual"}),
        ("failed-junit", subprocess.CalledProcessError, {"service", "identity_actual"}),
        ("coverage", json.JSONDecodeError, {"coverage_actual"}),
        ("resources", json.JSONDecodeError, {"resources"}),
        ("inputs", ValueError, set()),
        ("fingerprints", ValueError, {"inputs_after"}),
    ],
)
def test_failed_sample_keeps_boundary_evidence_before_any_next_sample(
    observer, monkeypatch, failure, error_type, expected_errors
):
    observer.OUTPUT.mkdir(parents=True)
    calls = []
    count = 0
    original_error = {
        "timeout": subprocess.TimeoutExpired("service", 735),
        "service": subprocess.CalledProcessError(23, "service"),
        "failed-junit": subprocess.CalledProcessError(1, "service"),
    }.get(failure)

    def fingerprints():
        nonlocal count
        count += 1
        if failure == "fingerprints" and count > 1:
            raise ValueError("source inventory unavailable")
        return {"workload": "changed" if failure == "inputs" and count > 1 else "fixed"}

    monkeypatch.setattr(observer, "input_fingerprints", fingerprints)

    def service(slot, timeout):
        calls.append(slot)
        directory = observer.OUTPUT / str(slot)
        (directory / "pytest.log").write_text("boundary detail")
        (directory / "resources.json").write_text(
            "invalid JSON" if failure == "resources" else '{"peak_bytes":42}'
        )
        xml(
            directory / "junit.xml",
            child="<failure/>" if failure == "failed-junit" else "",
        )
        (directory / "coverage.json").write_text(
            "invalid JSON"
            if failure == "coverage"
            else '{"meta":{"branch_coverage":true},"files":{"module":{"executed_lines":[1]}}}'
        )
        if failure == "junit":
            (directory / "junit.xml").write_text("invalid XML")
        if original_error is not None:
            raise original_error
        return 1.25

    monkeypatch.setattr(observer, "service", service)
    (observer.OUTPUT / "metadata.json").write_text(
        '{"head":"fixed","inputs":{"workload":"fixed"}}'
    )
    with pytest.raises(error_type) as error:
        observer.measure()
    if original_error is not None:
        assert error.value is original_error
    assert calls == [1]
    directory = observer.OUTPUT / "1"
    record = json.loads((directory / "observation.json").read_text())
    assert record["wall_seconds"] >= 0
    assert record["resources"] == {"resources": None}.get(failure, {"peak_bytes": 42})
    assert record["expected"]["inputs"] == {"workload": "fixed"}
    expected_identity = {
        "cases": 1,
        "skipped": 0,
        "identity_outcome_sha256": observer.digest([("tests.app.x", "case", "passed")]),
    }
    assert record["identity_actual"] == {"junit": None, "failed-junit": None}.get(
        failure, expected_identity
    )
    assert record["coverage_actual"] == {"coverage": None}.get(
        failure, observer.digest({"module": {"executed_lines": [1]}})
    )
    assert record["inputs_after"] == {
        "fingerprints": None,
        "inputs": {"workload": "changed"},
    }.get(failure, {"workload": "fixed"})
    assert set(record["errors"]) == expected_errors
    assert all(record["errors"][field] for field in expected_errors)
    assert (directory / "pytest.log").read_text() == "boundary detail"
    assert (directory / "junit.xml").is_file() and (
        directory / "coverage.json"
    ).is_file()
    assert not (observer.OUTPUT / "samples.json").exists()


def test_changed_inputs_stop_before_service_and_preserve_expected_actual(
    observer, monkeypatch
):
    observer.OUTPUT.mkdir(parents=True)
    monkeypatch.setattr(observer, "input_fingerprints", lambda: {"workload": "changed"})
    monkeypatch.setattr(
        observer, "service", lambda *args: pytest.fail("changed inputs reached service")
    )
    (observer.OUTPUT / "metadata.json").write_text(
        '{"head":"fixed","inputs":{"workload":"fixed"}}'
    )
    with pytest.raises(ValueError, match="fingerprints"):
        observer.measure()
    record = json.loads((observer.OUTPUT / "1/observation.json").read_text())
    assert record["inputs_before"] == {"workload": "changed"}
    assert record["expected"]["inputs"] == {"workload": "fixed"}


def test_early_timeout_records_unavailable_fields_and_preserves_failure(
    observer, monkeypatch
):
    observer.OUTPUT.mkdir(parents=True)
    original_error = subprocess.TimeoutExpired("service", 735)
    calls = []

    def service(slot, timeout):
        calls.append(slot)
        (observer.OUTPUT / str(slot) / "pytest.log").write_text("timeout before report")
        raise original_error

    monkeypatch.setattr(observer, "service", service)
    monkeypatch.setattr(observer, "input_fingerprints", lambda: {"workload": "fixed"})
    (observer.OUTPUT / "metadata.json").write_text(
        '{"head":"fixed","inputs":{"workload":"fixed"}}'
    )
    with pytest.raises(subprocess.TimeoutExpired) as error:
        observer.measure()
    assert error.value is original_error and calls == [1]
    record = json.loads((observer.OUTPUT / "1/observation.json").read_text())
    assert record["wall_seconds"] >= 0 and record["inputs_after"] == {
        "workload": "fixed"
    }
    assert (
        record["identity_actual"]
        is record["coverage_actual"]
        is record["resources"]
        is None
    )
    assert set(record["errors"]) == {
        "service",
        "identity_actual",
        "coverage_actual",
        "resources",
    }
    assert all(
        record["errors"][name].startswith("FileNotFoundError:")
        for name in ("identity_actual", "coverage_actual", "resources")
    )


@pytest.mark.parametrize("mismatch", ["identity", "coverage", "both", "peak"])
def test_rejected_covered_sample_keeps_both_hashes_and_raw_evidence(
    observer, monkeypatch, mismatch
):
    observer.OUTPUT.mkdir(parents=True)
    (observer.OUTPUT / "metadata.json").write_text(
        '{"head":"fixed","inputs":{"workload":"fixed"}}'
    )
    monkeypatch.setattr(
        observer, "input_fingerprints", lambda: {"workload": "fixed"}, raising=False
    )
    calls = []

    def sample(slot, timeout):
        calls.append(slot)
        directory = observer.OUTPUT / str(slot)
        directory.mkdir(exist_ok=True)
        xml(
            directory / "junit.xml",
            "changed" if slot == 3 and mismatch in ("identity", "both") else "case",
        )
        line = 2 if slot == 3 and mismatch in ("coverage", "both") else 1
        (directory / "coverage.json").write_text(
            json.dumps(
                {
                    "meta": {"branch_coverage": True},
                    "files": {"module": {"executed_lines": [line]}},
                }
            )
        )
        (directory / "resources.json").write_text(
            json.dumps({"peak_bytes": 0 if slot == 3 and mismatch == "peak" else 42})
        )
        (directory / "pytest.log").write_text("sample detail")
        return 1.25

    monkeypatch.setattr(observer, "service", sample)
    with pytest.raises(ValueError):
        observer.measure()
    assert calls == [1, 2, 3]
    directory = observer.OUTPUT / "3"
    record = json.loads((directory / "observation.json").read_text())
    assert record["wall_seconds"] == 1.25
    assert record["resources"] == {"peak_bytes": 0 if mismatch == "peak" else 42}
    assert record["inputs_before"] == record["inputs_after"] == {"workload": "fixed"}
    assert (record["expected"]["identity"] != record["identity_actual"]) is (
        mismatch in ("identity", "both")
    )
    assert (record["expected"]["coverage"] != record["coverage_actual"]) is (
        mismatch in ("coverage", "both")
    )
    assert record["identity_actual"] == observer.junit_result(directory / "junit.xml")
    assert record["coverage_actual"] == observer.coverage_result(
        directory / "coverage.json"
    )
    assert (directory / "pytest.log").read_text() == "sample detail"
    assert json.loads((directory / "coverage.json").read_text())["files"] == {
        "module": {"executed_lines": [2 if mismatch in ("coverage", "both") else 1]}
    }
    assert len(json.loads((observer.OUTPUT / "samples.json").read_text())["rows"]) == 2
