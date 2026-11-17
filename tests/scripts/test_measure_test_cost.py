"""Boundary tests for the fixed hosted measurement; never run real hosted services."""

import importlib.util
import json
import subprocess
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
        observer.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    assert observer.service(1, 720) >= 0
    command, options = calls[0]
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
    assert options["timeout"] == 735 and options["check"] is True


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

    monkeypatch.setattr(observer.subprocess, "run", fail)
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
    assert args[:3] == observer._group_arguments("application")
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
    (observer.OUTPUT / "metadata.json").write_text('{"head":"fixed"}')
    calls = []

    def sample(slot, timeout):
        calls.append(slot)
        directory = observer.OUTPUT / str(slot)
        directory.mkdir()
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
    condition = (
        "inputs.measure || contains(github.event.head_commit.message, '[measure-2000]')"
    )
    assert job["timeout-minutes"] == "${{ (" + condition + ") && 60 || 5 }}"
    assert job["strategy"]["matrix"] == {"python": ["3.11", "3.12", "3.13"]}
    steps = job["steps"]
    long = next(step for step in steps if step.get("run", "").endswith("--measure"))
    assert long["if"] == (
        "(github.event_name == 'workflow_dispatch' && inputs.measure) || "
        "(github.event_name == 'push' && "
        "contains(github.event.head_commit.message, '[measure-2000]'))"
    )
    assert data["on"]["push"]["branches"] == ["feat/coverage-worker-cost-2000"]
    upload = steps[-1]["with"]
    assert upload["path"].splitlines() == [
        "dist/test-cost/metadata.json",
        "dist/test-cost/samples.json",
        "dist/test-cost/*/resources.json",
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
    "fault", ["host", "architecture", "version", "actions", "source", "none"]
)
def test_preflight_binds_supported_host_and_commit(observer, monkeypatch, fault):
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
            version=observer.sys.version,
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
    Path("uv.lock").write_text("locked")

    def probe(slot, timeout):
        assert slot == 0 and timeout == 20
        directory = observer.OUTPUT / "0"
        directory.mkdir()
        (directory / "resources.json").write_text('{"peak_bytes":123}')
        return 0.1

    monkeypatch.setattr(observer, "service", probe)
    if fault == "none":
        assert observer.preflight()["head"] == "fixed"
        assert (observer.OUTPUT / "metadata.json").is_file()
    else:
        with pytest.raises(ValueError):
            observer.preflight()


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
