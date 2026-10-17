"""Run pytest with compact success output and complete failure diagnostics."""

import argparse
import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO

SERIAL = False
CHUNK_SIZE = 64 * 1024
INTERRUPT_GRACE_SECONDS = 300
TERMINATE_GRACE_SECONDS = 3
TEST_TIMEOUT_SECONDS = 17 * 60
_PYTEST_ENVIRONMENT_OVERRIDES = {
    "PYTEST_ADDOPTS",
    "COVERAGE_RCFILE",
    "COVERAGE_FILE",
    "HMCPCTL_TEST_TIMINGS",
}


def _resources_allow_parallel(
    proc: Path = Path("/proc"), root: Path = Path("/sys/fs/cgroup")
) -> bool:
    """Require two effective CPUs and 3 GiB available under every visible limit."""
    if sys.platform != "linux":
        return False
    try:
        cpus = len(os.sched_getaffinity(0))
        available = [
            line.split()
            for line in (proc / "meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        ]
        if len(available) != 1 or len(available[0]) != 3 or available[0][2] != "kB":
            return False
        memory = int(available[0][1]) * 1024
        memberships = []
        for line in (proc / "self/cgroup").read_text().splitlines():
            hierarchy, controllers, membership = line.split(":", 2)
            if {"cpu", "memory"}.intersection(controllers.split(",")):
                return False  # A hybrid hierarchy may hide tighter v1 resource limits.
            if hierarchy == "0" and not controllers:
                memberships.append(membership)
        if len(memberships) != 1 or not memberships[0].startswith("/"):
            return False
        root = root.resolve()
        (root / "cgroup.controllers").read_text()  # Require a mounted v2 hierarchy.
        group = (root / memberships[0].lstrip("/")).resolve()
        if not group.is_relative_to(root):
            return False
        while True:
            cpu = group / "cpu.max"
            if group != root or cpu.exists():
                quota, period_text = cpu.read_text().split()
                period = int(period_text)
                if period <= 0 or (quota != "max" and int(quota) <= 0):
                    return False
                if quota != "max":
                    cpus = min(cpus, int(quota) // period)
            limit_path = group / "memory.max"
            if group != root or limit_path.exists():
                limit = limit_path.read_text().strip()
                current = int((group / "memory.current").read_text())
                if current < 0 or (limit != "max" and int(limit) < 0):
                    return False
                if limit != "max":
                    memory = min(memory, max(0, int(limit) - current))
            if group == root:
                return cpus >= 2 and memory >= 3 * 1024**3
            group = group.parent
    except (OSError, ValueError):
        return False


def _direct_children() -> list[int]:
    return [
        int(pid)
        for pid in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    ]


def _subreaper(value: int | None = None) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    state = ctypes.c_int()
    result = (
        libc.prctl(37, ctypes.byref(state), 0, 0, 0)
        if value is None
        else libc.prctl(36, value, 0, 0, 0)
    )
    if result != 0:
        raise OSError(ctypes.get_errno(), "cannot access child-subreaper state")
    return state.value


@contextlib.contextmanager
def _parallel_mode() -> Generator[bool]:
    previous = None
    try:
        try:
            if (
                not SERIAL
                and _resources_allow_parallel()
                and threading.current_thread() is threading.main_thread()
                and threading.active_count() == 1
                and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
                and not _direct_children()
            ):
                previous = _subreaper()
                _subreaper(1)
        except (OSError, AttributeError, ValueError):
            previous = None
        yield previous is not None
    finally:
        if previous is not None:
            handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                _subreaper(previous)
            finally:
                signal.signal(signal.SIGINT, handler)


def _adopted_children(
    process: subprocess.Popen[bytes] | None, sig: int | None = None
) -> None:
    for pid in _direct_children():
        if process is not None and process.returncode is None and pid == process.pid:
            continue  # Popen alone owns its unreaped child's status.
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if waited == 0 and sig is not None:
            # An owned, unreaped PID cannot recycle: no other thread/reaper is admitted.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)


def _signal_group(process: subprocess.Popen[bytes] | None, sig: int) -> None:
    if process is not None and process.returncode is None:
        # The unreaped session leader pins this group ID even after its exit.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)


def _wait_parallel(process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        _adopted_children(process)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            pass


def _stop_parallel(process: subprocess.Popen[bytes] | None) -> bool:
    """Bound cleanup of owned children, including descendants in nested sessions."""
    interrupted = False

    def interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    handler = signal.signal(signal.SIGINT, interrupt)
    failure = None
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            while True:
                if interrupted and sig == signal.SIGTERM:
                    break
                try:
                    _signal_group(process, sig)
                    if process is not None:
                        process.poll()
                    _adopted_children(process, sig)
                    empty = not _direct_children()
                except (OSError, ValueError) as error:
                    failure = OSError(
                        f"cannot inspect or signal owned pytest children: {error}"
                    )
                else:
                    if (process is None or process.returncode is not None) and empty:
                        if failure is not None:
                            raise failure
                        return interrupted
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        raise failure or OSError("owned pytest descendants survived bounded cleanup")
    finally:
        signal.signal(signal.SIGINT, handler)


def _replay(output: BinaryIO) -> None:
    output.seek(0)
    shutil.copyfileobj(output, sys.stderr.buffer, length=CHUNK_SIZE)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop a child that will not exit on its own, escalating to SIGKILL.

    A `KeyboardInterrupt` here is a further Ctrl-C asking to stop now, so it
    escalates exactly as an expired wait does.
    """
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        try:
            process.wait()
        except KeyboardInterrupt:
            pass


def _settle_interrupted(process: subprocess.Popen[bytes]) -> None:
    """Give an interrupted pytest time to emit diagnostics, then stop it.

    The window is a ceiling, not a latency budget: the wait returns the moment
    the child exits, so only a child that has not exited pays it. ADR 0130
    sizes it against the suite this script wraps, and records why a second
    Ctrl-C, not the number, is what bounds an interactive run.
    """
    try:
        process.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _stop(process)


def _exit_status(returncode: int) -> int:
    return 128 + abs(returncode) if returncode < 0 else returncode


def _run_suite(parallel: bool) -> int:
    """Run the configured pytest suite and report its result compactly."""
    timings = os.environ.get("HMCPCTL_TEST_TIMINGS") == "1"
    command = [sys.executable, "-m", "pytest"]
    if parallel:
        command.extend(["-n", "2", "--dist=loadfile", "--max-worker-restart=0"])
    if timings:
        command.extend(["--durations=30", "--durations-min=0"])
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _PYTEST_ENVIRONMENT_OVERRIDES
    }
    with tempfile.TemporaryFile() as output:
        process = None
        interrupted = False
        timed_out = False
        try:
            try:
                process = subprocess.Popen(
                    command,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=parallel,
                )
                if parallel:
                    _wait_parallel(process, TEST_TIMEOUT_SECONDS)
                else:
                    process.wait(timeout=TEST_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                if not parallel and process is not None:
                    _stop(process)
            except KeyboardInterrupt:
                interrupted = True
                if parallel and process is not None:
                    _signal_group(process, signal.SIGINT)
                    try:
                        _wait_parallel(process, INTERRUPT_GRACE_SECONDS)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt):
                        pass
                elif process is not None:
                    _settle_interrupted(process)
            finally:
                if parallel:
                    interrupted = _stop_parallel(process) or interrupted
        except OSError:
            _replay(output)
            raise

        if (
            process is not None
            and process.returncode == 0
            and not interrupted
            and not timed_out
        ):
            if timings:
                try:
                    _replay(output)
                except KeyboardInterrupt:
                    return 130
            return 0
        try:
            _replay(output)
        except KeyboardInterrupt:
            return 130
        if interrupted:
            return 130
        if timed_out:
            print(f"test: timed out after {TEST_TIMEOUT_SECONDS}s", file=sys.stderr)
            return 124
        assert process is not None and process.returncode is not None
        return _exit_status(process.returncode)


def main() -> int:
    """Select a bounded execution mode and restore its process-level ownership."""
    try:
        with _parallel_mode() as parallel:
            status = _run_suite(parallel)
        if status == 0:
            print(
                "test: passed; configured coverage gate passed"
                + ("; workers=2" if parallel else "")
            )
        return status
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        print(f"test: cannot run or clean up pytest: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--timings", action="store_true", help="retain pytest phase durations"
    )
    parser.add_argument(
        "--serial", action="store_true", help="disable parallel execution"
    )
    arguments = parser.parse_args()
    SERIAL = arguments.serial
    if arguments.timings:
        os.environ["HMCPCTL_TEST_TIMINGS"] = "1"
    raise SystemExit(main())
