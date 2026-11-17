"""Exercise the configured pytest plugins through fresh interpreter processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOOP_PROBE = """\
import asyncio
import warnings

import pytest
import pytest_asyncio

completed = []

@pytest_asyncio.fixture
async def fixture_loop():
    loop = asyncio.get_running_loop()
    yield loop
    assert asyncio.get_running_loop() is loop
    completed.append(loop)

@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", [0, 1])
async def test_fixture_loop(fixture_loop, iteration):
    assert asyncio.get_running_loop() is fixture_loop
    assert len(completed) == iteration

def test_teardown():
    assert len(completed) == 2
    assert completed[0] is not completed[1]
    assert all(loop.is_closed() for loop in completed)
    warnings.warn("unrelated warning probe", RuntimeWarning)
"""


@pytest.fixture
def configured_project(tmp_path: Path) -> Path:
    tmp_path.joinpath("pyproject.toml").write_text(
        ROOT.joinpath("pyproject.toml").read_text()
    )
    tmp_path.joinpath("test_loop_probe.py").write_text(LOOP_PROBE)
    return tmp_path


def _run_pytest(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_")
        and key not in {"PYTHONWARNINGS", "COVERAGE_RCFILE", "COVERAGE_FILE"}
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "-q",
            *arguments,
            "test_loop_probe.py",
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    "arguments",
    [(), ("--collect-only",), ("-n", "2", "--dist=loadfile", "--max-worker-restart=0")],
    ids=["serial", "collection", "workers"],
)
def test_pytest_entrypoints_preserve_fixture_loops(
    configured_project: Path, arguments: tuple[str, ...]
) -> None:
    result = _run_pytest(configured_project, *arguments)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert '"asyncio_default_fixture_loop_scope" is unset' not in output
    assert "CallSpec2" not in output
    if "--collect-only" not in arguments:
        assert "3 passed" in output
        assert "unrelated warning probe" in output


def test_unrelated_warnings_keep_the_error_gate(configured_project: Path) -> None:
    result = _run_pytest(configured_project, "-W", "error::RuntimeWarning")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed, 2 passed" in output
    assert "RuntimeWarning: unrelated warning probe" in output
