"""Fixed hosted application-shard experiment for issue #2000; no production policy."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import selectors
import shutil
import subprocess  # nosec B404 # Fixed local git/systemd/pytest commands only.
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from run_tests import _group_arguments

OUTPUT = Path("dist/test-cost")
MODES = ((2, True), (2, False), (3, True), (4, True))
SAMPLES = (*MODES, *reversed(MODES))
SAMPLE_SECONDS = 720
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOG_HALF_BYTES = 1024 * 1024
# This digest fingerprints public runtime metadata, not a credential.
ORIGINAL_RUNTIME_SHA256 = "9057b049cc5454471cb2b440c08e2a53f6f0cefd6f3e35a6e6c873f59fe7e2f5"  # pragma: allowlist secret


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def input_fingerprints() -> dict[str, object]:
    """Fingerprint source, fixtures and selectors without generated/ignored files."""
    names = (
        subprocess.check_output(
            [
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
            ],
            timeout=10,
        )
        .decode()
        .split("\0")
    )  # nosec B603 B607 # Fixed local input inventory.
    files = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in names
        if name and name != "tests/scripts/test_measure_test_cost.py"
    }
    if not files:
        raise ValueError("measurement requires a nonempty source/input inventory")
    return {
        "files": files,
        "workload_sha256": digest(files),
        "selector": _group_arguments("application"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "workflow_sha256": hashlib.sha256(
            Path(".github/workflows/coverage-worker-cost.yml").read_bytes()
        ).hexdigest(),
    }


def capture_log(command: list[str], directory: Path, timeout: float) -> None:
    """Drain output continuously, retaining at most its first/last MiB and a marker."""
    prefix, tail = bytearray(), bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )  # nosec B603 # Fixed owned service or controlled regression child.
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    keep = min(len(chunk), LOG_HALF_BYTES - len(prefix))
                    prefix.extend(chunk[:keep])
                    tail.extend(chunk[keep:])
                    del tail[:-LOG_HALF_BYTES]
        result = process.wait(timeout=max(0, deadline - time.monotonic()))
        if result:
            raise subprocess.CalledProcessError(result, command)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        omitted = total - len(prefix) - len(tail)
        marker = f"\n[{omitted} output bytes omitted]\n".encode() if omitted else b""
        (directory / "pytest.log").write_bytes(prefix + marker + tail)
        (directory / "log.json").write_text(
            json.dumps({"output_bytes": total, "omitted_bytes": omitted}) + "\n"
        )


def host_memory() -> int:
    """Require the host memory prerequisite independently of cgroup accounting."""
    memory = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
    )
    available = memory["MemAvailable"].split()
    if (
        len(available) != 2
        or available[1] != "kB"
        or int(available[0]) * 1024 < 12 * 1024**3
    ):
        raise ValueError("measurement requires at least 12 GiB host available memory")
    return int(available[0]) * 1024


def resource_context() -> dict[str, object]:
    """Read the fresh sample's cgroup and its visible ancestors."""
    memberships = Path("/proc/self/cgroup").read_text().splitlines()
    selected = [line[3:] for line in memberships if line.startswith("0::/")]
    if len(selected) != 1:
        raise ValueError("measurement requires one cgroup-v2 membership")
    root = Path("/sys/fs/cgroup").resolve()
    group = (root / selected[0].lstrip("/")).resolve()
    if not group.is_relative_to(root):
        raise ValueError("measurement cgroup escapes its mounted root")
    memory = host_memory()
    ancestors = []
    current = group
    while True:
        values = {}
        for name in ("cpu.max", "memory.max", "memory.current"):
            path = current / name
            values[name] = path.read_text().strip() if path.exists() else "unavailable"
        quota, period = (
            str(values["cpu.max"]).split()
            if values["cpu.max"] != "unavailable"
            else ("max", "1")
        )
        if int(period) <= 0 or (quota != "max" and int(quota) / int(period) < 4):
            raise ValueError("measurement requires four effective cgroup CPUs")
        ancestors.append(values)
        if current == root:
            break
        current = current.parent
    cpus = len(getattr(os, "sched_getaffinity", lambda _pid: set())(0))
    if cpus != 4:
        raise ValueError("measurement requires exactly four affinity CPUs")
    return {
        "ancestors": ancestors,
        "host_available_bytes": memory,
        "affinity_cpus": cpus,
        "cpu_count": os.cpu_count(),
        "peak_bytes": int((group / "memory.peak").read_text()),
    }


def inside(slot: int) -> None:
    """Run inside the owned service, writing the peak before cgroup destruction."""
    directory = OUTPUT / str(slot)
    context = resource_context()
    if slot == 0:
        executable = shutil.which("hmcpctl")
        if executable is None or not Path(executable).resolve().is_relative_to(
            Path(sys.prefix).resolve()
        ):
            raise ValueError(
                "measurement requires this virtualenv's hmcpctl on the service PATH"
            )
        import coverage

        probe = coverage.Coverage(branch=True)
        probe.start()
        try:
            context["coverage_core"] = dict(probe.sys_info())["core"]
        finally:
            probe.stop()
        (directory / "resources.json").write_text(json.dumps(context) + "\n")
        return
    import pytest

    workers, covered = SAMPLES[slot - 1]
    args = [
        *_group_arguments("application"),
        "-n",
        str(workers),
        "--dist=loadfile",
        "--max-worker-restart=0",
        "--cov-fail-under=0",
        f"--junitxml={directory}/junit.xml",
    ]
    args.append(
        f"--cov-report=json:{directory}/coverage.json" if covered else "--no-cov"
    )
    result = pytest.main(args)
    context = resource_context()
    (directory / "resources.json").write_text(json.dumps(context) + "\n")
    raise SystemExit(result)


def service(slot: int, timeout: int) -> float:
    """Give each sample a fresh memory-accounted service with a bounded lifetime."""
    directory = OUTPUT / str(slot)
    directory.mkdir(exist_ok=True)
    unit = f"hmcp2000-{os.getpid()}-{slot}"
    command = [
        "sudo",
        "-n",
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        f"--unit={unit}",
        f"--uid={os.getuid()}",
        f"--gid={os.getgid()}",
        f"--working-directory={Path.cwd()}",
        f"--setenv=PATH={Path(sys.executable).parent}:{SYSTEM_PATH}",
        "--property=MemoryAccounting=yes",
        "--property=MemoryMax=12G",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=5",
        f"--property=RuntimeMaxSec={timeout}",
        sys.executable,
        str(Path(__file__).resolve()),
        "--inside",
        str(slot),
    ]
    started = time.monotonic()
    capture_log(command, directory, timeout + 15)
    return time.monotonic() - started


def junit_result(path: Path) -> dict[str, object]:
    """Hash complete case identities and outcomes, omitting machine-private XML fields."""
    root = ET.parse(path).getroot()  # nosec B314 # Locally generated pytest output only.
    cases = []
    for case in root.iter("testcase"):
        state = "skipped" if case.find("skipped") is not None else "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError("measurement contains failed cases")
        cases.append((case.attrib["classname"], case.attrib["name"], state))
    if not cases or len(cases) != len(set(cases)):
        raise ValueError("measurement requires nonempty unique case identities")
    return {
        "cases": len(cases),
        "skipped": sum(row[2] == "skipped" for row in cases),
        "identity_outcome_sha256": digest(sorted(cases)),
    }


def coverage_result(path: Path) -> str:
    data = json.loads(path.read_text())
    if data["meta"]["branch_coverage"] is not True or not data["files"]:
        raise ValueError(
            "measurement requires branch coverage from the application shard"
        )
    return digest(data["files"])


def preflight() -> dict[str, object]:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("measurement requires native hosted Linux amd64")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (
        version not in ("3.11", "3.12", "3.13")
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        raise ValueError("measurement requires GitHub Actions Python 3.11–3.13")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, timeout=10
    ).strip()  # nosec B603 B607
    if head != os.environ.get("GITHUB_SHA"):
        raise ValueError("measurement source does not match the triggering commit")
    OUTPUT.mkdir(parents=True)
    elapsed = service(0, 20)
    metadata = {
        "head": head,
        "hmcpctl_version": importlib.metadata.version("hmcpctl"),
        "python": sys.version,
        "machine": platform.machine(),
        "platform": platform.platform(),
        "preflight_seconds": elapsed,
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("pytest", "pytest-cov", "pytest-xdist", "coverage")
        },
        "resources": json.loads((OUTPUT / "0/resources.json").read_text()),
        "inputs": input_fingerprints(),
        "cache": "fresh hosted VM; setup-uv cache disabled; samples reuse one venv and OS caches",
    }
    runtime = {key: metadata[key] for key in ("python", "packages", "lock_sha256")}
    runtime["coverage_core"] = metadata["resources"]["coverage_core"]
    metadata["runtime_sha256"] = digest(runtime)
    metadata["expected_runtime_sha256"] = ORIGINAL_RUNTIME_SHA256
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata), flush=True)
    if metadata["runtime_sha256"] != ORIGINAL_RUNTIME_SHA256:
        raise ValueError(
            "diagnostic runtime/lock/core differs from original Python 3.12; see metadata.json"
        )
    return metadata


def retain_sample(slot: int, expected: dict) -> dict:
    """Persist raw output and current observation before a comparison can reject it."""
    directory = OUTPUT / str(slot)
    directory.mkdir()
    record: dict = {
        "slot": slot,
        "expected": expected.copy(),
        "inputs_before": input_fingerprints(),
        "identity_actual": None,
        "coverage_actual": None,
        "resources": None,
        "inputs_after": None,
        "errors": {},
    }
    observation = directory / "observation.json"
    observation.write_text(json.dumps(record, indent=2) + "\n")
    if record["inputs_before"] != expected["inputs"]:
        raise ValueError(
            f"sample {slot} changed source/input fingerprints; no comparison"
        )
    started = time.monotonic()
    failure = None
    try:
        record["wall_seconds"] = service(slot, SAMPLE_SECONDS)
    except Exception as error:  # noqa: BLE001 # Collect evidence, then re-raise this same error.
        failure = error
        record["errors"]["service"] = f"{type(error).__name__}: {error}"
    finally:
        record.setdefault("wall_seconds", time.monotonic() - started)
        observation.write_text(json.dumps(record, indent=2) + "\n")
    covered = SAMPLES[record["slot"] - 1][1]
    collectors = {
        "identity_actual": lambda: junit_result(directory / "junit.xml"),
        "coverage_actual": lambda: (
            coverage_result(directory / "coverage.json") if covered else None
        ),
        "resources": lambda: json.loads((directory / "resources.json").read_text()),
        "inputs_after": input_fingerprints,
    }
    for name, collect in collectors.items():
        try:
            record[name] = collect()
        except Exception as error:  # noqa: BLE001 # Collect other fields; re-raise the first error.
            record["errors"][name] = f"{type(error).__name__}: {error}"
            if failure is None:
                failure = error
        finally:
            observation.write_text(json.dumps(record, indent=2) + "\n")
    if failure is not None:
        raise failure
    if record["inputs_after"] != expected["inputs"]:
        raise ValueError(
            f"sample {slot} changed source/input fingerprints; no comparison"
        )
    return record


def measure() -> None:
    """Stop at the first failed or unmatched sample; do not retry observations."""
    metadata = json.loads((OUTPUT / "metadata.json").read_text())
    rows = []
    identity = coverage = None
    for slot, (workers, covered) in enumerate(SAMPLES, 1):
        record = retain_sample(
            slot,
            {"inputs": metadata["inputs"], "identity": identity, "coverage": coverage},
        )
        result = record["identity_actual"]
        if identity is not None and result != identity:
            raise ValueError(
                f"sample {slot} changed test identities/outcomes; no comparison"
            )
        identity = result
        if covered:
            signature = record["coverage_actual"]
            if coverage is not None and signature != coverage:
                raise ValueError(
                    f"sample {slot} changed per-file coverage; no comparison"
                )
            coverage = signature
        resources = record["resources"]
        if resources["peak_bytes"] <= 0:
            raise ValueError(f"sample {slot} lacks a kernel memory peak")
        row = {
            "slot": slot,
            "workers": workers,
            "coverage": covered,
            "wall_seconds": record["wall_seconds"],
            **result,
            "peak_bytes": resources["peak_bytes"],
            "coverage_sha256": coverage if covered else None,
        }
        rows.append(row)
        (OUTPUT / "samples.json").write_text(
            json.dumps({"head": metadata["head"], "rows": rows}, indent=2) + "\n"
        )
        print(json.dumps(row), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--measure", action="store_true")
    mode.add_argument("--inside", type=int, choices=range(9))
    arguments = parser.parse_args()
    if arguments.inside is not None:
        inside(arguments.inside)
    elif arguments.preflight:
        preflight()
    else:
        measure()


if __name__ == "__main__":
    main()
