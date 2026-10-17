"""CLI contract for ``hmcpctl lpars capture-console`` (issue #959, ADR 0175)."""

from __future__ import annotations

import errno
import inspect
import io
from collections.abc import Callable
from typing import Self
from unittest.mock import ANY, AsyncMock

import pytest
from click import unstyle
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands import runtime as cli_runtime
from hmcpctl.cli_commands.lpar import console as cli_console
from hmcpctl.server_tools.console import hmc_capture_lpar_console
from hmcpctl.ssh.console import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SECONDS,
    ConsoleCapture,
    ConsoleHeldError,
)

RUNNER = CliRunner()
DATA = b"boot\n\x1b[31m\xff\x00"
COMMAND = ["lpars", "capture-console", "aix-db", "--system", "system-a"]


class _Client:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _capture(
    *, stop_reason: str = "idle", released: bool = True, error: str | None = None
) -> ConsoleCapture:
    return ConsoleCapture(
        system="system-a",
        lpar="aix-db",
        data=DATA,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        released=released,
        error=error,
    )


def _capture_side_effect(
    result: ConsoleCapture,
    *,
    data: bytes = DATA,
) -> Callable:
    """Return an async side-effect for ``capture_lpar_console_by_selector``.

    The side-effect calls the *on_chunk* keyword argument with *data* before
    returning *result*, exercising the CLI's streaming write path (issue #1674).
    """

    async def _inner(
        _hmc,
        _lpar,
        _system,
        *,
        duration_seconds: float,
        max_bytes: int,
        idle_timeout_seconds: float,
        on_chunk: Callable[[bytes], None] | None = None,
        stop_on_cancel: bool = False,
    ) -> ConsoleCapture:
        if on_chunk is not None:
            on_chunk(data)
        return result

    return _inner


@pytest.fixture
def client(monkeypatch) -> _Client:
    hmc = _Client()
    monkeypatch.setattr(cli_runtime, "client", lambda: hmc)
    return hmc


@pytest.fixture
def capture(monkeypatch, client) -> AsyncMock:
    mock = AsyncMock(side_effect=_capture_side_effect(_capture()))
    monkeypatch.setattr(cli_console, "capture_lpar_console_by_selector", mock)
    return mock


def test_options_reach_the_capture_and_bytes_reach_stdout(client, capture):
    result = RUNNER.invoke(
        cli.app,
        [
            *COMMAND[:3],
            "-s",
            "system-a",
            "--duration",
            "12.5",
            "--max-bytes",
            "4096",
            "--idle-timeout",
            "3.5",
        ],
    )

    assert result.exit_code == 0, result.stderr
    capture.assert_awaited_once_with(
        client,
        "aix-db",
        "system-a",
        duration_seconds=12.5,
        max_bytes=4096,
        idle_timeout_seconds=3.5,
        on_chunk=ANY,
        stop_on_cancel=True,
    )
    assert result.stdout_bytes == DATA
    assert f"stop reason: idle; bytes: {len(DATA)}; released: true" in result.stderr


def test_defaults_match_the_mcp_tool(client, capture):
    result = RUNNER.invoke(cli.app, COMMAND)

    assert result.exit_code == 0, result.stderr
    tool = inspect.signature(hmc_capture_lpar_console).parameters
    # Streaming and interrupt handling are CLI-only; compare shared bounds.
    actual = {
        k: v
        for k, v in capture.await_args.kwargs.items()
        if k not in {"on_chunk", "stop_on_cancel"}
    }
    assert actual == {
        name: tool[name].default
        for name in ("duration_seconds", "max_bytes", "idle_timeout_seconds")
    }


def test_output_file_receives_the_bytes(capture, tmp_path):
    target = tmp_path / "console.log"

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 0, result.stderr
    assert target.read_bytes() == DATA
    assert result.stdout_bytes == b""


def test_output_file_is_allowed_when_stdout_is_a_terminal(
    capture, tmp_path, monkeypatch
):
    monkeypatch.setattr(cli_console, "_stdout_is_terminal", lambda: True)
    target = tmp_path / "console.log"

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 0, result.stderr
    assert target.read_bytes() == DATA


@pytest.mark.parametrize(
    "extra",
    [
        ["--duration", "0"],
        ["--duration", "nan"],
        ["--duration", str(MAX_CAPTURE_SECONDS + 1)],
        ["--max-bytes", "0"],
        ["--max-bytes", str(MAX_CAPTURE_BYTES + 1)],
        ["--idle-timeout", "0"],
        ["--idle-timeout", "inf"],
    ],
)
def test_out_of_range_bound_is_a_usage_error_before_any_capture(capture, extra):
    result = RUNNER.invoke(cli.app, [*COMMAND, *extra])

    assert result.exit_code == 2
    assert extra[0] in result.stderr
    capture.assert_not_awaited()


def test_existing_output_file_is_refused_before_any_capture(capture, tmp_path):
    target = tmp_path / "console.log"
    target.write_bytes(b"keep me")

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert target.read_bytes() == b"keep me"
    capture.assert_not_awaited()


def test_terminal_stdout_without_output_is_refused_before_any_capture(
    capture, monkeypatch
):
    monkeypatch.setattr(cli_console, "_stdout_is_terminal", lambda: True)

    result = RUNNER.invoke(cli.app, COMMAND)

    assert result.exit_code == 2
    assert "--output" in result.stderr
    capture.assert_not_awaited()


def test_contention_exits_1_and_leaves_no_output_file(capture, tmp_path):
    capture.side_effect = ConsoleHeldError("mkvterm found the console held")
    target = tmp_path / "console.log"

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 1
    assert "mkvterm found the console held" in result.stderr
    assert "stop reason" not in result.stderr
    assert not target.exists()


def test_interrupt_without_capture_proof_retains_empty_output(capture, tmp_path):
    capture.side_effect = KeyboardInterrupt
    target = tmp_path / "console.log"

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 3
    assert target.read_bytes() == b""
    assert "release was not proven" in result.stderr


@pytest.mark.parametrize(
    ("stop_reason", "released", "error", "code"),
    [
        ("duration", True, None, 0),
        ("max_bytes", True, None, 0),
        ("remote-close", True, None, 0),
        ("error", True, "channel reset", 1),
        ("idle", False, None, 3),
        ("error", False, "channel reset", 3),
    ],
)
def test_exit_code_follows_the_capture_outcome_and_bytes_are_written(
    capture, stop_reason, released, error, code
):
    capture.side_effect = _capture_side_effect(
        _capture(stop_reason=stop_reason, released=released, error=error)
    )

    result = RUNNER.invoke(cli.app, COMMAND)

    assert result.exit_code == code
    assert result.stdout_bytes == DATA
    assert (
        f"stop reason: {stop_reason}; bytes: {len(DATA)}; released: {str(released).lower()}"
        in (result.stderr)
    )
    if error:
        assert f"error: {error}" in result.stderr
    if not released:
        assert "rmvterm" in result.stderr


class _FullDisk(io.RawIOBase):
    def writable(self) -> bool:
        return True

    def write(self, _data) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize(("released", "code"), [(True, 1), (False, 3)])
def test_failed_write_removes_the_output_file(
    capture, tmp_path, monkeypatch, released, code
):
    capture.side_effect = _capture_side_effect(_capture(released=released))
    target = tmp_path / "console.log"
    create = cli_console._create_exclusive

    def full_disk(path):
        create(path).close()
        return io.BufferedWriter(_FullDisk())

    monkeypatch.setattr(cli_console, "_create_exclusive", full_disk)

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == code
    assert "No space left on device" in result.stderr
    assert not target.exists()


def test_bytes_are_written_as_chunks_arrive_not_after_capture(
    monkeypatch, client, tmp_path
):
    """Each chunk reaches the output file as it arrives, not in one post-capture write.

    The mock delivers DATA in two chunks.  We record when each sink.write()
    call happens relative to the mock's return.  A pre-1674 implementation
    wrote nothing during the capture and flushed everything afterward; the
    streaming implementation flushes each chunk before returning.
    """
    CHUNK_A = DATA[:3]
    CHUNK_B = DATA[3:]
    write_calls: list[bytes] = []
    done = False

    async def _streaming_mock(
        _hmc,
        _lpar,
        _system,
        *,
        duration_seconds: float,
        max_bytes: int,
        idle_timeout_seconds: float,
        on_chunk: Callable[[bytes], None] | None = None,
        stop_on_cancel: bool = False,
    ) -> ConsoleCapture:
        nonlocal done
        if on_chunk is not None:
            on_chunk(CHUNK_A)
            on_chunk(CHUNK_B)
        done = True
        return _capture()

    mock = AsyncMock(side_effect=_streaming_mock)
    monkeypatch.setattr(cli_console, "capture_lpar_console_by_selector", mock)

    target = tmp_path / "console.log"
    # Wrap _create_exclusive so we can intercept write calls.
    orig_create = cli_console._create_exclusive

    def recording_sink(path: object):
        f = orig_create(path)

        class _Recording:
            def write(self, data: bytes) -> int:
                write_calls.append(data)
                return f.write(data)

            def flush(self) -> None:
                f.flush()

            def close(self) -> None:
                f.close()

        return _Recording()

    monkeypatch.setattr(cli_console, "_create_exclusive", recording_sink)

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 0, result.stderr
    assert target.read_bytes() == DATA
    # Both chunks were written during the capture (before done=True in mock),
    # not in a single post-capture write.
    assert write_calls == [CHUNK_A, CHUNK_B]


def test_output_file_is_owner_only(capture, tmp_path):
    target = tmp_path / "console.log"

    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])

    assert result.exit_code == 0, result.stderr
    assert target.stat().st_mode & 0o777 == 0o600


def test_rmvterm_hint_quotes_names(capture):
    capture.side_effect = _capture_side_effect(
        ConsoleCapture(
            system="system a", lpar="db;x", data=b"", stop_reason="idle", released=False
        ),
        data=b"",
    )

    result = RUNNER.invoke(cli.app, COMMAND)

    assert result.exit_code == 3
    assert "rmvterm -m 'system a' -p 'db;x'" in result.stderr


def test_help_lists_every_option():
    result = RUNNER.invoke(cli.app, ["lpars", "capture-console", "--help"])

    assert result.exit_code == 0
    for option in (
        "--system",
        "--duration",
        "--max-bytes",
        "--idle-timeout",
        "--output",
    ):
        assert option in unstyle(result.stdout)


def test_lost_hold_reports_the_new_holder_without_rmvterm_advice(capture):
    capture.side_effect = _capture_side_effect(
        _capture(
            stop_reason="error",
            released=False,
            error="ConsoleHoldLostError: another client's rmvterm ended the hold",
        )
    )

    result = RUNNER.invoke(cli.app, COMMAND)

    assert result.exit_code == 3
    assert "another client ended this hold" in result.stderr
    assert "rmvterm -m" not in result.stderr


@pytest.mark.parametrize("released,code", [(True, 130), (False, 3)])
def test_interrupted_outcome_retains_bytes(capture, tmp_path, released, code):
    capture.side_effect = _capture_side_effect(
        _capture(stop_reason="interrupted", released=released)
    )
    target = tmp_path / "partial.bin"
    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])
    assert result.exit_code == code
    assert target.read_bytes() == DATA
    if not released:
        assert "rmvterm" not in result.stderr
        assert "check console ownership" in result.stderr


@pytest.mark.parametrize("released,code", [(True, 130), (False, 3)])
@pytest.mark.parametrize("failure", ["write", "close"])
def test_interrupted_write_failure_keeps_flushed_bytes(
    capture, monkeypatch, tmp_path, released, code, failure
):
    target = tmp_path / "partial.bin"
    create = cli_console._create_exclusive

    class Sink:
        def __init__(self, path):
            self.file = create(path)
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if failure == "write" and self.writes == 2:
                raise OSError("output unavailable")
            return self.file.write(data)

        def flush(self):
            self.file.flush()

        def close(self):
            self.file.close()
            if failure == "close":
                raise OSError("output unavailable")

    monkeypatch.setattr(cli_console, "_create_exclusive", Sink)

    async def interrupted(*args, on_chunk, **kwargs):
        on_chunk(DATA)
        on_chunk(b"tail")
        return _capture(stop_reason="interrupted", released=released)

    capture.side_effect = interrupted
    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])
    assert result.exit_code == code
    assert target.read_bytes() == DATA + (b"tail" if failure == "close" else b"")
    assert "output unavailable" in result.stderr


@pytest.mark.parametrize("buffered", [False, True])
def test_interrupt_without_capture_proof_reports_write_failure(
    capture, monkeypatch, tmp_path, buffered
):
    target = tmp_path / "partial.bin"
    create = cli_console._create_exclusive

    def full_disk(path):
        create(path).close()
        return io.BufferedWriter(_FullDisk()) if buffered else _FullDisk()

    monkeypatch.setattr(cli_console, "_create_exclusive", full_disk)

    async def interrupted(*args, on_chunk, **kwargs):
        on_chunk(DATA)
        raise KeyboardInterrupt

    capture.side_effect = interrupted
    result = RUNNER.invoke(cli.app, [*COMMAND, "--output", str(target)])
    assert result.exit_code == 3
    assert target.exists()
    assert "No space left on device" in result.stderr
