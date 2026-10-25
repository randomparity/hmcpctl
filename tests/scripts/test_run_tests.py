"""Tests for the compact pytest output adapter."""

import contextlib
import importlib.util
import inspect
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

MODULE_PATH = Path(__file__).parents[2] / "scripts" / "run_tests.py"
MODULE_SPEC = importlib.util.spec_from_file_location("run_tests", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
run_tests = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(run_tests)


@pytest.fixture(autouse=True)
def serial_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy adapter unit tests outside process-level ownership changes."""
    monkeypatch.setattr(run_tests, "SERIAL", True)


class TrackingTemporaryFile(io.BytesIO):
    """A binary temporary file that records copy reads and context closure."""

    def __init__(self) -> None:
        super().__init__()
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class RecordingBuffer(io.BytesIO):
    """A binary sink that records each replayed chunk."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[bytes] = []

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self.chunks.append(chunk)
        return super().write(chunk)


class BinaryStderr:
    """Minimal stderr replacement exposing only its byte buffer."""

    def __init__(self) -> None:
        self.buffer = RecordingBuffer()


class InterruptingProcess:
    """A child whose first `interrupts` waits raise `KeyboardInterrupt`.

    One stub covers the whole escalation ladder: one interrupt is the ordinary
    Ctrl-C, two reach `terminate()`, three reach `kill()`. `payload` stands in
    for whatever pytest managed to write before the first interrupt.
    """

    returncode = 2

    def __init__(self, interrupts: int, capture: BinaryIO, payload: bytes) -> None:
        self.interrupts = interrupts
        self.capture = capture
        self.payload = payload
        self.wait_count = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_count += 1
        if self.wait_count == 1:
            self.capture.write(self.payload)
        if self.wait_count <= self.interrupts:
            raise KeyboardInterrupt
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


# A hang ceiling, not a latency budget: the poll loop below returns the moment
# the marker appears, so only a child that never becomes ready ever pays this.
_READINESS_TIMEOUT_SECONDS = 60.0

# Covers what `run_tests._settle_interrupted` does not bound -- the post-kill
# reap, the captured-output replay, and the parent's own teardown.
_INTERRUPT_COLLECTION_SLACK_SECONDS = 10.0


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started.

    `start_new_session=True` makes the child a group leader, so a bare
    `process.kill()` strands the grandchild pytest. Mirrors `_kill_group` in
    `scripts/check_generated_docs.py`, fallback included: the direct child is
    covered by the group kill except when signalling the group was refused.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def _wait_for_process_marker(
    marker: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float = _READINESS_TIMEOUT_SECONDS,
) -> float:
    """Wait for the child to declare readiness, returning how long that took.

    That figure is what the caller reports. Both it and the post-interrupt
    settle interval move with host load -- ADR 0130 measured them tracking each
    other once `run_tests`'s old 3-second clamp stopped hiding the relation --
    so readiness is reported beside the settle interval, never instead of it.
    """
    started = time.monotonic()
    deadline = started + timeout_seconds
    while not marker.exists():
        if process.poll() is not None:
            # The direct child is gone, but the grandchild pytest it started
            # keeps the group alive -- which is both why the kill reaches it and
            # why the pgid is not yet free to be recycled. Not the timeout arm's
            # case below: poll() has already reaped the child here, so this
            # signal rests on that surviving member rather than on the leader.
            # See `_kill_process_group`.
            _kill_process_group(process)
            pytest.fail(f"child exited with {process.returncode} before becoming ready")
        if time.monotonic() >= deadline:
            _kill_process_group(process)
            pytest.fail(f"child did not create readiness marker {marker}")
        time.sleep(0.01)
    return time.monotonic() - started


def _stub_pytest(
    monkeypatch: pytest.MonkeyPatch, output: bytes, returncode: int
) -> tuple[list[tuple[list[str], dict[str, object]]], TrackingTemporaryFile]:
    calls: list[tuple[list[str], dict[str, object]]] = []
    temporary_file = TrackingTemporaryFile()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured_output = kwargs["stdout"]
        assert isinstance(captured_output, TrackingTemporaryFile)
        captured_output.write(output)
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(run_tests.subprocess, "Popen", fake_popen)
    return calls, temporary_file


def _assert_pytest_invocation(
    calls: list[tuple[list[str], dict[str, object]]], output: TrackingTemporaryFile
) -> None:
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [sys.executable, "-m", "pytest"]
    assert kwargs["stdout"] is output
    assert kwargs["stderr"] is subprocess.STDOUT
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment == {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "COVERAGE_RCFILE", "COVERAGE_FILE"}
    }
    assert "cwd" not in kwargs


def test_success_hides_noisy_pytest_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, temporary_file = _stub_pytest(
        monkeypatch, b"================ noisy pytest output ================\n", 0
    )

    assert run_tests.main() == 0

    captured = capsys.readouterr()
    assert captured.out == "test: passed; configured coverage gate passed\n"
    assert "noisy pytest output" not in captured.out
    assert "noisy pytest output" not in captured.err
    _assert_pytest_invocation(calls, temporary_file)
    assert temporary_file.read_sizes == []
    assert temporary_file.closed


def test_success_hides_output_larger_than_one_mebibyte(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _calls, temporary_file = _stub_pytest(monkeypatch, b"x" * (2 * 1024 * 1024), 0)

    assert run_tests.main() == 0

    captured = capsys.readouterr()
    assert captured.out == "test: passed; configured coverage gate passed\n"
    assert captured.err == ""
    assert temporary_file.closed


def test_failure_replays_pytest_output_as_bytes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = b"FAILED test_example.py::test_case\n"
    stderr = BinaryStderr()
    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    calls, temporary_file = _stub_pytest(monkeypatch, output, 1)

    assert run_tests.main() == 1

    assert stderr.buffer.getvalue() == output
    assert capsys.readouterr().out == ""
    _assert_pytest_invocation(calls, temporary_file)
    assert temporary_file.closed


def test_failure_preserves_invalid_utf8_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = b"FAILED test_example.py::test_case \xff\n"
    stderr = BinaryStderr()
    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    _calls, temporary_file = _stub_pytest(monkeypatch, output, 1)

    assert run_tests.main() == 1

    assert stderr.buffer.getvalue() == output
    assert temporary_file.closed


def test_failure_replays_multiple_bounded_binary_chunks_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk_size = 64 * 1024
    output = b"a" * chunk_size + b"b" * chunk_size + b"\xfftail"
    stderr = BinaryStderr()
    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    _calls, temporary_file = _stub_pytest(monkeypatch, output, 1)

    assert run_tests.main() == 1

    assert stderr.buffer.getvalue() == output
    assert stderr.buffer.chunks == [
        b"a" * chunk_size,
        b"b" * chunk_size,
        b"\xfftail",
    ]
    assert temporary_file.read_sizes == [chunk_size] * 4
    assert temporary_file.closed


@pytest.mark.parametrize("timings", [False, True])
def test_interruption_replays_captured_output_without_traceback(
    monkeypatch: pytest.MonkeyPatch, timings: bool
) -> None:
    monkeypatch.setenv("HMCPCTL_TEST_TIMINGS", "1" if timings else "0")
    output = b"partial pytest diagnostic before SIGINT \xff\n"
    stderr = BinaryStderr()
    temporary_file = TrackingTemporaryFile()
    process = InterruptingProcess(1, temporary_file, output)

    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(
        run_tests.subprocess, "Popen", lambda _command, **_kwargs: process
    )

    assert run_tests.main() == 130

    assert stderr.buffer.getvalue() == output
    assert temporary_file.closed


def test_interrupt_during_replay_returns_interrupted_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_chunk = b"a" * run_tests.CHUNK_SIZE
    temporary_file = TrackingTemporaryFile()
    process = InterruptingProcess(1, temporary_file, first_chunk + b"remaining\n")

    class ReplayInterruptingBuffer(RecordingBuffer):
        def write(self, data: Any) -> int:
            if self.chunks:
                raise KeyboardInterrupt
            return super().write(data)

    stderr = BinaryStderr()
    stderr.buffer = ReplayInterruptingBuffer()
    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(
        run_tests.subprocess, "Popen", lambda _command, **_kwargs: process
    )

    try:
        status = run_tests.main()
    except KeyboardInterrupt:
        pytest.fail("an interrupt during replay escaped main()")

    assert status == 130
    assert stderr.buffer.getvalue() == first_chunk
    assert temporary_file.closed


def test_the_reap_is_the_smaller_of_the_two_bounds() -> None:
    """The ladder's invariant: the reap follows the diagnostic window.

    Only the ordering is asserted. The window's size answers to the suite
    `just test` wraps, which ADR 0130 measures and no test here can; and it is
    deliberately not floored against `_READINESS_TIMEOUT_SECONDS`, because
    pinning a production constant to a test-file one would make raising this
    module's readiness ceiling raise the shipped window with it.
    """
    assert run_tests.TERMINATE_GRACE_SECONDS < run_tests.INTERRUPT_GRACE_SECONDS


def test_timeout_is_below_the_ci_leg_budget() -> None:
    """The script's timeout must fire before the CI leg budget expires.

    The approved #1775 allowances are 25 minutes for tests and 30 minutes for
    the named ``ci`` job, whose actual workflow value is pinned by the CI-shape
    tests. Preserve five minutes for the surrounding job lifecycle; this is an
    allowance change, not evidence that the earlier timeout's cause was fixed.
    Interrupt and termination grace decisions remain separate (ADR 0129/0130).
    """
    _CI_LEG_BUDGET_SECONDS = 30 * 60
    assert run_tests.TEST_TIMEOUT_SECONDS == 25 * 60
    assert _CI_LEG_BUDGET_SECONDS - run_tests.TEST_TIMEOUT_SECONDS == 5 * 60


@pytest.mark.parametrize(("interrupts", "killed"), [(2, False), (3, True), (4, True)])
def test_further_interrupts_escalate_without_escaping_main(
    monkeypatch: pytest.MonkeyPatch, interrupts: int, killed: bool
) -> None:
    """A further Ctrl-C escalates; it must not escape `main` and strand the child."""
    payload = b"pytest report before the next interrupt \xff\n"
    stderr = BinaryStderr()
    temporary_file = TrackingTemporaryFile()
    process = InterruptingProcess(interrupts, temporary_file, payload)

    monkeypatch.setattr(run_tests.sys, "stderr", stderr)
    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(
        run_tests.subprocess, "Popen", lambda _command, **_kwargs: process
    )

    assert run_tests.main() == 130

    assert process.terminated
    assert process.killed is killed
    assert stderr.buffer.getvalue() == payload
    assert temporary_file.closed


def test_a_wedged_child_is_terminated_then_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that never exits is escalated through both rungs, not waited on.

    This is the only test driving the window's and the reap's timeout branches
    rather than their interrupt branches, so it is what proves an interrupted
    run still fails on a bounded budget instead of stalling.
    """
    temporary_file = TrackingTemporaryFile()

    class WedgedProcess:
        returncode = -9

        def __init__(self) -> None:
            self.timeouts: list[float | None] = []
            self.terminated = False
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1:
                raise KeyboardInterrupt
            if timeout is not None:
                raise subprocess.TimeoutExpired(["pytest"], timeout)
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = WedgedProcess()
    monkeypatch.setattr(run_tests.sys, "stderr", BinaryStderr())
    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(
        run_tests.subprocess, "Popen", lambda _command, **_kwargs: process
    )

    assert run_tests.main() == 130

    assert process.terminated
    assert process.killed
    # Pins the routing, both constants, and both escalation rungs at once.
    assert process.timeouts == [
        run_tests.TEST_TIMEOUT_SECONDS,
        run_tests.INTERRUPT_GRACE_SECONDS,
        run_tests.TERMINATE_GRACE_SECONDS,
        None,
    ]


@pytest.mark.parametrize("timings", [False, True])
def test_timeout_terminates_pytest_and_returns_timeout_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timings: bool,
) -> None:
    monkeypatch.setenv("HMCPCTL_TEST_TIMINGS", "1" if timings else "0")
    temporary_file = TrackingTemporaryFile()

    class TimedOutProcess:
        returncode = 143

        def __init__(self) -> None:
            self.wait_count = 0
            self.timeouts: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            self.timeouts.append(timeout)
            if self.wait_count == 1:
                temporary_file.write(b"pytest stalled\n")
                assert timeout is not None
                raise subprocess.TimeoutExpired(["pytest"], timeout)
            return self.returncode

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    process = TimedOutProcess()
    monkeypatch.setattr(run_tests.tempfile, "TemporaryFile", lambda: temporary_file)
    monkeypatch.setattr(
        run_tests.subprocess, "Popen", lambda _command, **_kwargs: process
    )

    assert run_tests.main() == 124

    error_output = capsys.readouterr().err
    assert "pytest stalled" in error_output
    assert "timed out" in error_output
    assert process.wait_count == 2
    # The timeout arm escalates straight away: nothing has asked this child to
    # stop, so a diagnostic window would only delay the report (ADR 0130).
    assert process.timeouts == [
        run_tests.TEST_TIMEOUT_SECONDS,
        run_tests.TERMINATE_GRACE_SECONDS,
    ]
    assert temporary_file.closed


def test_a_refused_group_kill_still_kills_the_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`killpg` can be refused, and then nothing has killed the direct child."""
    killed: list[int] = []

    class RefusedProcess:
        pid = 4321

        def kill(self) -> None:
            killed.append(self.pid)

    def refused(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "killpg", refused)

    _kill_process_group(cast(subprocess.Popen[bytes], RefusedProcess()))

    assert killed == [4321]


def test_killing_a_group_that_has_already_gone_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both signals raise when nothing is left, and neither may escape."""

    def gone(*_args: object, **_kwargs: object) -> None:
        raise ProcessLookupError("no such process")

    class GoneProcess:
        pid = 4322

        def kill(self) -> None:
            raise ProcessLookupError("no such process")

    monkeypatch.setattr(os, "killpg", gone)

    _kill_process_group(cast(subprocess.Popen[bytes], GoneProcess()))


def test_a_child_that_exits_early_has_its_group_killed_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The early-exit arm must tear the group down, as the timeout arm does.

    A child that dies before writing the marker can still have started the
    grandchild pytest, and that grandchild outlives the direct child --
    `_kill_process_group`'s own docstring is why. Failing without the group kill
    strands it for the rest of the run.
    """
    killed: list[int] = []

    def recording_killpg(pid: int, _signal: int) -> None:
        killed.append(pid)

    class ExitedProcess:
        pid = 4323
        returncode = 3

        def poll(self) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    monkeypatch.setattr(os, "killpg", recording_killpg)

    with pytest.raises(pytest.fail.Exception) as failure:
        _wait_for_process_marker(
            tmp_path / "never-written",
            cast(subprocess.Popen[bytes], ExitedProcess()),
        )

    assert "exited with 3" in str(failure.value)
    assert killed == [4323]


def test_main_accepts_no_arguments() -> None:
    assert list(inspect.signature(run_tests.main).parameters) == []


@pytest.mark.parametrize("status", [1, 2, -signal.SIGTERM])
def test_timing_failure_preserves_status_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    monkeypatch.setenv("HMCPCTL_TEST_TIMINGS", "1")
    _stub_pytest(monkeypatch, b"pytest failure\n", status)

    assert run_tests.main() == (128 + abs(status) if status < 0 else status)

    captured = capsys.readouterr()
    assert captured.err == "pytest failure\n"
    assert captured.out == ""


def test_timing_success_replay_interruption_returns_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HMCPCTL_TEST_TIMINGS", "1")
    _stub_pytest(monkeypatch, b"slowest durations\n", 0)

    def interrupt(_output: BinaryIO) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_tests, "_replay", interrupt)

    assert run_tests.main() == 130
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["1", "0", "", "--no-cov"])
def test_timing_switch_only_changes_presentation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setenv("HMCPCTL_TEST_TIMINGS", value)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--no-cov")
    calls, output = _stub_pytest(monkeypatch, b"slowest durations\n", 0)

    assert run_tests.main() == 0

    captured = capsys.readouterr()
    assert captured.err == ("slowest durations\n" if value == "1" else "")
    assert captured.out == "test: passed; configured coverage gate passed\n"
    assert calls[0][0] == [sys.executable, "-m", "pytest"] + (
        ["--durations=30", "--durations-min=0"] if value == "1" else []
    )
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    environment_keys = set(environment)
    assert "HMCPCTL_TEST_TIMINGS" not in environment_keys
    assert "PYTEST_ADDOPTS" not in environment_keys
    assert output.closed


@pytest.mark.parametrize(
    "argument", ["--no-cov", "--cov-fail-under=0", "tests/", "--tim"]
)
def test_timing_cli_rejects_other_arguments(tmp_path: Path, argument: str) -> None:
    tmp_path.joinpath("pytest.py").write_text(
        "raise AssertionError('pytest launched')\n"
    )
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--timings", argument],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert "pytest launched" not in result.stderr


def test_timing_cli_retains_successful_output(tmp_path: Path) -> None:
    tmp_path.joinpath("pytest.py").write_text(
        "import sys\nprint('pytest arguments:', sys.argv[1:])\n"
    )
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--serial", "--timings"],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "['--durations=30', '--durations-min=0']" in result.stderr
    assert "configured coverage gate passed" in result.stdout


def test_signal_return_code_maps_to_shell_status() -> None:
    assert run_tests._exit_status(-signal.SIGTERM) == 128 + signal.SIGTERM


def test_real_interrupt_preserves_pytest_diagnostic(tmp_path: Path) -> None:
    ready = tmp_path / "pytest-ready"
    tmp_path.joinpath("test_slow.py").write_text(
        "import pathlib\nimport time\n\ndef test_slow():\n"
        "    pathlib.Path('pytest-ready').touch()\n    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(MODULE_PATH), "--serial"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    ready_in = _wait_for_process_marker(ready, process)
    # A group that has already gone means the child exited on its own; the
    # returncode assertion below reports that far better than an errno would.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGINT)
    signalled_at = time.monotonic()
    window = run_tests.INTERRUPT_GRACE_SECONDS
    reap = run_tests.TERMINATE_GRACE_SECONDS
    budget = window + reap + _INTERRUPT_COLLECTION_SLACK_SECONDS
    try:
        stdout, stderr = process.communicate(timeout=budget)
    except subprocess.TimeoutExpired as expired:
        _kill_process_group(process)
        # Whatever the timed-out call had already read, so a drain that cannot
        # finish still reports something. The drain is bounded rather than bare:
        # an unbounded wait here would replace the budget it just enforced.
        stderr = expired.stderr or b""
        with contextlib.suppress(subprocess.TimeoutExpired):
            _stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"child did not exit within {budget}s of SIGINT "
            f"(window {window}s, reap {reap}s) "
            f"after becoming ready in {ready_in:.1f}s; "
            f"stderr tail: {stderr[-2000:]!r}"
        )
    settled_in = time.monotonic() - signalled_at

    assert process.returncode == 130
    assert stdout == b""
    # Report, do not attribute. Both figures now move with load: ADR 0130
    # measured the settle interval tracking readiness at 0.95x once the old
    # 3-second clamp stopped hiding it. That is why neither is evidence on its
    # own -- a regression that lowered INTERRUPT_GRACE_SECONDS would satisfy any
    # `settled_in >= window` guard trivially and be reported as host slowness.
    assert b"KeyboardInterrupt" in stderr, (
        f"no KeyboardInterrupt: the child became ready in {ready_in:.1f}s and "
        f"settled in {settled_in:.1f}s against a window of {window}s "
        f"and a reap of {reap}s; "
        f"stderr tail: {stderr[-2000:]!r}"
    )


@pytest.mark.parametrize(
    ("change", "eligible"),
    [
        ({}, True),
        ({"affinity": 1}, False),
        ({"affinity": 2}, True),
        ({"parent/cpu.max": "100000 100000"}, False),
        ({"parent/cpu.max": "199999 100000"}, False),
        ({"parent/cpu.max": "200000 100000"}, True),
        ({"parent/cpu.max": "max 0"}, False),
        ({"parent/cpu.max": "-1 100000"}, False),
        ({"parent/cpu.max": "broken"}, False),
        ({"cgroup.controllers": None}, False),
        ({"cgroup.controllers": ""}, True),
        ({"parent/cpu.max": None}, False),
        ({"parent/memory.max": str(3 * 1024**3)}, True),
        ({"parent/memory.max": str(3 * 1024**3 - 1)}, False),
        (
            {
                "parent/memory.max": str(4 * 1024**3),
                "parent/memory.current": str(2 * 1024**3),
            },
            False,
        ),
        ({"parent/memory.current": "-1"}, False),
        ({"parent/memory.max": "broken"}, False),
        ({"parent/memory.current": None}, False),
        ({"meminfo": "MemAvailable: 3145727 kB\n"}, False),
        ({"meminfo": "MemAvailable: 3145728 kB\n"}, True),
        ({"meminfo": "MemTotal: 999999999 kB\n"}, False),
        ({"meminfo": "MemAvailable: -1 kB\n"}, False),
        ({"membership": "0::/../outside\n"}, False),
        ({"membership": "1:memory:/parent/child\n"}, False),
        ({"membership": "0::/\n", "cgroup.controllers": "io pids"}, True),
        (
            {
                "membership": "4:cpu,cpuacct:/restricted\n0::/\n",
                "cgroup.controllers": "io pids",
            },
            False,
        ),
        (
            {
                "membership": "5:memory:/restricted\n0::/\n",
                "cgroup.controllers": "io pids",
            },
            False,
        ),
        (
            {
                "membership": "4:cpu,cpuacct:/restricted\n5:memory:/restricted\n0::/\n",
                "cgroup.controllers": "io pids",
            },
            False,
        ),
        ({"membership": "1:name=systemd:/session\n0::/\n"}, True),
        ({"platform": "darwin"}, False),
        ({"cpu.max": "100000 100000"}, False),
        ({"memory.max": str(2 * 1024**3), "memory.current": "0"}, False),
    ],
)
def test_parallel_resource_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict, eligible: bool
) -> None:
    proc, root = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (root / "parent/child").mkdir(parents=True)
    files = {
        "cgroup.controllers": "cpu memory",
        "parent/cpu.max": "max 100000",
        "parent/memory.max": "max",
        "parent/memory.current": "0",
        "parent/child/cpu.max": "max 100000",
        "parent/child/memory.max": "max",
        "parent/child/memory.current": "0",
    }
    files.update({key: value for key, value in change.items() if "." in key})
    for name, value in files.items():
        if value is not None:
            (root / name).write_text(value)
    (proc / "meminfo").write_text(change.get("meminfo", "MemAvailable: 99999999 kB\n"))
    (proc / "self/cgroup").write_text(change.get("membership", "0::/parent/child\n"))
    monkeypatch.setattr(
        run_tests.os,
        "sched_getaffinity",
        lambda _pid: set(range(change.get("affinity", 48))),
        raising=False,
    )
    monkeypatch.setattr(run_tests.sys, "platform", change.get("platform", "linux"))
    assert run_tests._resources_allow_parallel(proc, root) is eligible


def test_serial_cli_preserves_original_invocation(tmp_path: Path) -> None:
    tmp_path.joinpath("pytest.py").write_text("import sys; print(sys.argv[1:])\n")
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--serial", "--timings"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == "['--durations=30', '--durations-min=0']\n"
    assert result.stdout == "test: passed; configured coverage gate passed\n"


def _parallel_probe(
    tmp_path: Path,
    controller: str | None,
    *,
    interrupts: int = 0,
    group: bool = False,
    initial: int = 1,
    launch_interrupt: bool = False,
    inventory_error: bool = False,
    ready_workers: int = 0,
) -> dict:
    """Exercise the real runner in isolation; contain only after recording survivors."""
    if controller is not None:
        tmp_path.joinpath("pytest.py").write_text(controller)
    probe = f"""
import ctypes, importlib.util, json, os, signal, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("runner", {str(MODULE_PATH)!r})
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
libc = ctypes.CDLL(None)
libc.prctl(36, {initial}, 0, 0, 0)
before = signal.getsignal(signal.SIGINT)
r._resources_allow_parallel = lambda: True
r.TEST_TIMEOUT_SECONDS = 30 if {bool(interrupts) or controller is None!r} else 2
r.TERMINATE_GRACE_SECONDS = .3
r.INTERRUPT_GRACE_SECONDS = 5
original_wait = getattr(r, '_wait_parallel', None)
def ready_timeout(process, timeout):
    deadline = time.monotonic() + 60
    while len(list(Path('.').glob('nested-ready-*'))) < {ready_workers}:
        if process.poll() is not None or time.monotonic() >= deadline:
            raise OSError('workers did not reach timeout fixture')
        time.sleep(.01)
    return original_wait(process, .3)
if {ready_workers}:
    r._wait_parallel = ready_timeout
original_popen = r.subprocess.Popen
def interrupted_launch(*args, **kwargs):
    process = original_popen(*args, **kwargs)
    deadline = time.monotonic() + 5
    while not Path('ready').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    raise KeyboardInterrupt
if {launch_interrupt!r}:
    r.subprocess.Popen = interrupted_launch
original_stop = getattr(r, '_stop_parallel', None)
def broken_inventory_stop(process):
    original_children = r._direct_children
    failed = False
    def children():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError('inventory unavailable')
        return original_children()
    r._direct_children = children
    return original_stop(process)
if {inventory_error!r}:
    r._stop_parallel = broken_inventory_stop
try:
    status = r.main()
except BaseException as error:
    status = type(error).__name__
children = Path(f"/proc/self/task/{{os.getpid()}}/children")
survivors = children.read_text().split()
state = ctypes.c_int()
libc.prctl(37, ctypes.byref(state), 0, 0, 0)
result = dict(status=status, survivors=survivors, state=state.value,
              handler_restored=signal.getsignal(signal.SIGINT) == before)
# Fixture containment follows, and cannot turn the captured survivor list green.
signal.signal(signal.SIGINT, signal.SIG_IGN)
libc.prctl(36, 1, 0, 0, 0)
deadline = time.monotonic() + 3
while children.read_text().strip() and time.monotonic() < deadline:
    for pid in map(int, children.read_text().split()):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                os.kill(pid, signal.SIGKILL)
        except (ChildProcessError, ProcessLookupError):
            pass
    time.sleep(.01)
print(json.dumps(result))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if interrupts:
            _wait_for_process_marker(tmp_path / "ready", process)
            (os.killpg if group else os.kill)(process.pid, signal.SIGINT)
            if controller is not None:
                _wait_for_process_marker(tmp_path / "interrupted", process)
            if interrupts > 1:
                os.kill(process.pid, signal.SIGINT)
                _wait_for_process_marker(tmp_path / "term-ready", process)
                os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=75)
    finally:
        if process.poll() is None:
            _kill_process_group(process)
            process.wait(timeout=5)
    assert process.returncode == 0, stderr.decode(errors="replace")
    result = json.loads(stdout.splitlines()[-1])
    result["stdout"], result["stderr"] = stdout, stderr
    return result


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child ownership")
@pytest.mark.parametrize("status", [0, 7, -signal.SIGTERM])
def test_parallel_preserves_status_and_restores_state(
    tmp_path: Path, status: int
) -> None:
    result = _parallel_probe(
        tmp_path,
        "import os, signal, sys\nprint(sys.argv[1:], flush=True)\n"
        + (
            f"os.kill(os.getpid(), {-status})\n"
            if status < 0
            else f"sys.exit({status})\n"
        ),
        initial=0,
    )
    assert result["status"] == run_tests._exit_status(status)
    assert result["survivors"] == []
    assert result["state"] == 0 and result["handler_restored"]
    if status == 0:
        assert b"workers=2" in result["stdout"]
    else:
        assert (
            b"'-n', '2', '--dist=loadfile', '--max-worker-restart=0'"
            in result["stderr"]
        )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child ownership")
@pytest.mark.parametrize("early", [False, True])
def test_parallel_cleans_nested_session_descendants(
    tmp_path: Path, early: bool
) -> None:
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    controller = f"""import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
print('original diagnostic', flush=True)
{"sys.exit(7)" if early else "time.sleep(60)"}
"""
    result = _parallel_probe(tmp_path, controller)
    assert result["status"] == (7 if early else 124)
    assert result["survivors"] == []
    assert result["state"] == 1 and result["handler_restored"]
    assert b"original diagnostic" in result["stderr"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child ownership")
@pytest.mark.parametrize(
    ("group", "repeated"), [(False, False), (True, False), (False, True)]
)
def test_parallel_interrupt_delivery_and_escalation(
    tmp_path: Path, group: bool, repeated: bool
) -> None:
    controller = f"""import pathlib, signal, time
count = 0
def interrupt(sig, frame):
    global count
    count += 1
    pathlib.Path('interrupted').write_text(str(count))
    print('diagnostic sentinel', flush=True)
    {"return" if repeated else "raise SystemExit(2)"}
def terminate(sig, frame):
    pathlib.Path('term-ready').touch()
signal.signal(signal.SIGINT, interrupt)
{"signal.signal(signal.SIGTERM, terminate)" if repeated else ""}
pathlib.Path('ready').touch()
while True: time.sleep(.01)
"""
    result = _parallel_probe(
        tmp_path, controller, interrupts=3 if repeated else 1, group=group
    )
    assert (result["status"], result["survivors"]) == (130, [])
    assert result["state"] == 1 and result["handler_restored"]
    assert (tmp_path / "interrupted").read_text() == "1"
    assert b"diagnostic sentinel" in result["stderr"]


@pytest.mark.parametrize(
    "condition",
    ["serial", "resources", "thread", "reaper", "children", "unavailable", "malformed"],
)
def test_parallel_ownership_admission(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    monkeypatch.setattr(run_tests, "SERIAL", condition == "serial")
    monkeypatch.setattr(
        run_tests, "_resources_allow_parallel", lambda: condition != "resources"
    )
    monkeypatch.setattr(
        run_tests.threading, "active_count", lambda: 2 if condition == "thread" else 1
    )
    monkeypatch.setattr(
        run_tests.signal,
        "getsignal",
        lambda _sig: signal.SIG_IGN if condition == "reaper" else signal.SIG_DFL,
    )

    def children():
        if condition == "malformed":
            raise ValueError("invalid child inventory")
        return [123] if condition == "children" else []

    monkeypatch.setattr(run_tests, "_direct_children", children)

    def unavailable(_value=None):
        assert condition == "unavailable"
        raise OSError("subreaper unavailable")

    monkeypatch.setattr(run_tests, "_subreaper", unavailable)
    with run_tests._parallel_mode() as parallel:
        assert not parallel


@pytest.mark.parametrize("returncode", [None, 0])
def test_adopted_pid_ownership_and_direct_status(
    monkeypatch: pytest.MonkeyPatch, returncode: int | None
) -> None:
    process = cast(
        subprocess.Popen[bytes],
        type("Process", (), {"pid": 123, "returncode": returncode})(),
    )
    monkeypatch.setattr(run_tests, "_direct_children", lambda: [123, 456, 789])
    waited, killed = [], []

    def waitpid(pid, options):
        waited.append(pid)
        if pid == 789:
            raise ChildProcessError
        return (0, 0) if pid == 123 else (pid, 0)

    monkeypatch.setattr(run_tests.os, "waitpid", waitpid)
    monkeypatch.setattr(run_tests.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(run_tests.os, "killpg", lambda pid, sig: killed.append(-pid))
    run_tests._adopted_children(process, signal.SIGTERM)
    run_tests._signal_group(process, signal.SIGINT)
    assert waited == ([456, 789] if returncode is None else [123, 456, 789])
    assert killed == ([-123] if returncode is None else [123])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bounded parallel path")
@pytest.mark.parametrize("fault", ["none", "coverage", "assertion"])
def test_real_workers_combine_coverage_and_preserve_failures(
    tmp_path: Path, fault: str
) -> None:
    tmp_path.joinpath("target.py").write_text(
        "def route(flag):\n    if flag:\n        return 1\n    return 2\n"
    )
    tmp_path.joinpath("pytest.ini").write_text(
        "[pytest]\naddopts = --cov=target --cov-branch --cov-report=json:coverage.json --cov-fail-under=90.5 --junitxml=junit.xml\n"
    )
    for number, flag in enumerate([True, fault == "coverage"]):
        tmp_path.joinpath(
            f"test_{number}.py"
        ).write_text(f"""import json, os, socket, time
from pathlib import Path
from target import route

def test_case(tmp_path, monkeypatch, worker_id):
    assert worker_id in ('gw0', 'gw1')
    assert 'OWNED_PARALLEL_PROBE' not in os.environ
    monkeypatch.setenv('OWNED_PARALLEL_PROBE', worker_id)
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        (tmp_path / 'isolation').write_text(worker_id)
        marker = Path(worker_id + '.tmp')
        marker.write_text(json.dumps(dict(port=connection.getsockname()[1], directory=str(tmp_path))))
        marker.replace(worker_id)
        other = Path('gw1' if worker_id == 'gw0' else 'gw0')
        deadline = time.monotonic() + 10
        while not other.exists() and time.monotonic() < deadline: time.sleep(.01)
        peer = json.loads(other.read_text())
        assert peer['port'] != connection.getsockname()[1]
        assert peer['directory'] != str(tmp_path)
        assert (tmp_path / 'isolation').read_text() == worker_id
        assert os.environ['OWNED_PARALLEL_PROBE'] == worker_id
    value = route({flag!r})
    assert value == {0 if fault == "assertion" and number == 1 else (1 if flag else 2)}
""")
    result = _parallel_probe(tmp_path, None)
    assert result["status"] == (0 if fault == "none" else 1)
    assert result["survivors"] == []
    assert {p.name for p in tmp_path.glob("gw*")} == {"gw0", "gw1"}
    totals = json.loads((tmp_path / "coverage.json").read_text())["totals"]
    assert totals["num_branches"] == 2
    assert (totals["percent_covered"] >= 90.5) is (fault != "coverage")


@pytest.mark.parametrize("failure", ["launch", "restore"])
def test_parallel_errors_restore_state_without_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    monkeypatch.setattr(run_tests, "SERIAL", False)
    monkeypatch.setattr(run_tests, "_resources_allow_parallel", lambda: True)
    monkeypatch.setattr(run_tests.threading, "active_count", lambda: 1)
    monkeypatch.setattr(run_tests, "_direct_children", list)
    states = []

    def subreaper(value=None):
        if value is None:
            return 0
        states.append(value)
        if failure == "restore" and value == 0:
            raise OSError("restoration denied")
        return 0

    def launch(*_args, **_kwargs):
        raise OSError("launch denied")

    monkeypatch.setattr(run_tests, "_subreaper", subreaper)
    if failure == "launch":
        monkeypatch.setattr(run_tests.subprocess, "Popen", launch)
    else:
        _stub_pytest(monkeypatch, b"", 0)
        monkeypatch.setattr(run_tests, "_wait_parallel", lambda *_args: None)
        monkeypatch.setattr(run_tests, "_stop_parallel", lambda *_args: False)
    assert run_tests.main() == 1
    assert states == [1, 0]
    captured = capsys.readouterr()
    assert "denied" in captured.err and "passed" not in captured.out


def test_further_interrupts_cannot_escape_kill_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases = []
    handler = signal.getsignal(signal.SIGINT)

    class Process:
        returncode = None

        def poll(self):
            if len(phases) == 3:
                self.returncode = 0
            return self.returncode

    process = Process()
    monkeypatch.setattr(
        run_tests, "_signal_group", lambda _process, sig: phases.append(sig)
    )
    monkeypatch.setattr(run_tests, "_adopted_children", lambda *_args: None)
    monkeypatch.setattr(
        run_tests,
        "_direct_children",
        lambda: [] if process.returncode is not None else [123],
    )

    def interrupt(_seconds):
        callback = signal.getsignal(signal.SIGINT)
        assert callable(callback) and not isinstance(callback, int)
        callback(signal.SIGINT, None)
        callback(signal.SIGINT, None)

    monkeypatch.setattr(run_tests.time, "sleep", interrupt)
    assert run_tests._stop_parallel(cast(subprocess.Popen[bytes], process))
    assert phases == [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL]
    assert signal.getsignal(signal.SIGINT) == handler


@pytest.mark.skipif(sys.platform != "linux", reason="Linux orphan reaping")
def test_parallel_reaps_orphans_without_consuming_controller_status(
    tmp_path: Path,
) -> None:
    child = "import os,time; from pathlib import Path; Path('orphan').write_text(str(os.getpid())); time.sleep(.1)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
    controller = f"""import subprocess, sys, time
from pathlib import Path
subprocess.run([sys.executable, '-c', {parent!r}], check=True)
while not Path('orphan').exists(): time.sleep(.01)
pid = Path('orphan').read_text()
deadline = time.monotonic() + 1
while Path('/proc/' + pid).exists() and time.monotonic() < deadline: time.sleep(.01)
sys.exit(9 if Path('/proc/' + pid).exists() else 7)
"""
    result = _parallel_probe(tmp_path, controller)
    assert result["status"] == 7
    assert result["survivors"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux launch ownership")
def test_interrupt_after_real_launch_cleans_before_restoring(tmp_path: Path) -> None:
    result = _parallel_probe(
        tmp_path,
        "from pathlib import Path; import time; Path('ready').touch(); time.sleep(60)",
        launch_interrupt=True,
    )
    assert (result["status"], result["survivors"]) == (130, [])
    assert result["state"] == 1 and result["handler_restored"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux cleanup inventory")
def test_inventory_error_still_cleans_real_child(tmp_path: Path) -> None:
    result = _parallel_probe(
        tmp_path,
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        inventory_error=True,
    )
    assert (result["status"], result["survivors"]) == (1, [])
    assert b"inventory unavailable" in result["stderr"]
    assert result["state"] == 1 and result["handler_restored"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux real worker timeout")
def test_real_worker_timeout_owns_nested_sessions(tmp_path: Path) -> None:
    child = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path('nested-ready-' + str(os.getpid())).touch(); time.sleep(60)"
    for number in range(2):
        tmp_path.joinpath(
            f"test_{number}.py"
        ).write_text(f"""import subprocess, sys, time

def test_hang():
    subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
    time.sleep(60)
""")
    result = _parallel_probe(tmp_path, None, ready_workers=2)
    assert len(list(tmp_path.glob("nested-ready-*"))) == 2
    assert (result["status"], result["survivors"]) == (124, [])
    assert b"timed out" in result["stderr"]
    assert result["state"] == 1 and result["handler_restored"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux real worker interrupt")
@pytest.mark.parametrize("group", [False, True])
def test_real_parallel_interrupt_keeps_pytest_diagnostic(
    tmp_path: Path, group: bool
) -> None:
    for number in range(2):
        tmp_path.joinpath(f"test_{number}.py").write_text(
            "from pathlib import Path\nimport time\ndef test_slow():\n    Path('ready').touch()\n    time.sleep(60)\n"
        )
    result = _parallel_probe(tmp_path, None, interrupts=1, group=group)
    assert (result["status"], result["survivors"]) == (130, [])
    assert b"KeyboardInterrupt" in result["stderr"]
    assert b"INTERNALERROR" not in result["stderr"]
    assert result["state"] == 1 and result["handler_restored"]
