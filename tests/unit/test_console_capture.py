"""Tests for the bounded LPAR console capture (issue #385, ADR 0072).

asyncssh is mocked at the two seams the capture uses: a long-lived connection
whose ``create_process`` hosts the ``mkvterm`` stream, and the one-shot
``run_hmc_command`` that carries ``rmvterm``. Every exit path must end in a
release attempt, and every assertion about ``released`` mirrors the P2 rule:
only a clean independent-session mkvterm probe proves release.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.server_tools import console as server_console
from hmcpctl.ssh.console import (
    HELD_SENTINEL,
    LOST_HOLD_SENTINEL,
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SECONDS,
    ConsoleCapture,
    ConsoleGap,
    ConsoleHandover,
    ConsoleHeldAfterDropError,
    ConsoleHeldError,
    ConsoleHoldLostError,
    ConsoleRawChannel,
    ConsoleSession,
    WritableConsoleSession,
    _acquire_capture_stream,
    _open_capture_stream,
    _probe_released,
    _SealedStdin,
    _truncate,
    capture_lpar_console,
)
from hmcpctl.ssh.transport import HMCCLIError

BANNER = b"\r\n Open in progress  \r\n "


def _client() -> HMCClient:
    return HMCClient(make_config())


# The exact recorded P1 contention output, quirks included.
CONTENTION = (
    b"\r\n A terminal session is already open for this partition. \r\n"
    b" Only one open session is allowed for a partition. \r\n Exiting.... "
)


class FakeStdout:
    """Replays scripted reads; ``None`` blocks until cancelled, an exception is raised.

    A trailing ``None`` blocks every later read too, like a silent live stream.
    """

    def __init__(
        self,
        *chunks: bytes | Exception | None,
        blocked_read_started: asyncio.Event | None = None,
    ):
        self._chunks = list(chunks)
        self._blocked_read_started = blocked_read_started

    async def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if chunk is None:
            if self._blocked_read_started is not None:
                self._blocked_read_started.set()
            try:
                await asyncio.Event().wait()  # cancelled by the caller's timeout
            except asyncio.CancelledError:
                if not self._chunks:
                    self._chunks.append(
                        None
                    )  # a trailing None: the stream stays silent
                raise
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def at_eof(self) -> bool:
        return not self._chunks


class FakeStdin:
    """Records what a writable session sends through asyncssh's stdin writer."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.drains = 0
        self.eof = False

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    async def drain(self) -> None:
        self.drains += 1

    def write_eof(self) -> None:
        self.eof = True


class FakeProcess:
    def __init__(
        self,
        *chunks: bytes | Exception | None,
        blocked_read_started: asyncio.Event | None = None,
    ):
        self.stdout = FakeStdout(
            *chunks,
            blocked_read_started=blocked_read_started,
        )
        self.stdin = FakeStdin()


#: What ``mkvterm`` sends about 10 s after its stdin reaches EOF (#1058).
EXITED = b" The write socket has closed. Exiting.\n"


class EofStdout(FakeStdout):
    """Replays its chunks, then waits for stdin EOF and exits like the HMC (#1058)."""

    def __init__(
        self,
        *chunks: bytes,
        eof: asyncio.Event,
        after_eof: tuple[bytes | Exception, ...],
    ):
        super().__init__(*chunks)
        self._eof = eof
        self._exit = [*after_eof, EXITED]

    async def read(self, size: int) -> bytes:
        if self._chunks:
            return await super().read(size)
        await self._eof.wait()
        chunk = self._exit.pop(0) if self._exit else b""
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def at_eof(self) -> bool:
        return self._eof.is_set() and not self._exit and not self._chunks


class EofProcess(FakeProcess):
    """A ``mkvterm`` whose stdin EOF ends it, as recorded live (#1058)."""

    def __init__(self, *chunks: bytes, after_eof: tuple[bytes | Exception, ...] = ()):
        super().__init__()
        self.eof = asyncio.Event()
        self.stdout = EofStdout(*chunks, eof=self.eof, after_eof=after_eof)
        self.stdin.write_eof = self.eof.set

    def watch(self, source: object) -> None:
        """Set :attr:`eof` once a sealed pipe's write end closes."""
        if source == asyncssh.PIPE:  # a writable session: write_eof sets it
            return
        loop = asyncio.get_running_loop()

        def readable() -> None:
            if not os.read(source, 1):
                loop.remove_reader(source)
                os.close(source)
                self.eof.set()

        loop.add_reader(source, readable)


class FakeConnection:
    """Hands out scripted processes and records what was asked of it."""

    def __init__(self, processes: list[FakeProcess]):
        self._processes = list(processes)
        self.create_process_calls: list[dict] = []
        self.closed = False
        self.close_calls = 0

    async def create_process(self, command: str, **kwargs):
        self.create_process_calls.append({"command": command, **kwargs})
        if not self._processes:
            raise AssertionError("unexpected extra create_process call")
        process = self._processes.pop(0)
        if isinstance(process, EofProcess):
            process.watch(kwargs.get("stdin"))
        return process

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    def is_closed(self) -> bool:
        return self.closed


class FailingProcessConnection(FakeConnection):
    async def create_process(self, command: str, **kwargs):
        raise OSError("channel unavailable")


class FailingStdout:
    async def read(self, size: int) -> bytes:
        raise OSError("channel lost")


class FailingReadProcess:
    stdout = FailingStdout()


@pytest.fixture(autouse=True)
def _short_eof_release():
    """Streams scripted to end in ``None`` ignore EOF, so their release falls back fast."""
    with patch("hmcpctl.ssh.console._EOF_RELEASE_SECONDS", 0.2):
        yield


def _capture_kwargs(**overrides):
    kwargs = {
        "duration_seconds": 5.0,
        "max_bytes": 65_536,
        "idle_timeout_seconds": 0.05,
    }
    kwargs.update(overrides)
    return kwargs


async def _run_capture(connection: FakeConnection, **overrides) -> ConsoleCapture:
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch(
            "hmcpctl.ssh.console.run_hmc_command",
            AsyncMock(return_value="Close command sent"),
        ) as release_mock,
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    ):
        capture = await capture_lpar_console(
            _client(),
            "sys1",
            "lp1",
            **_capture_kwargs(**overrides),
        )
    # ConsoleCapture is frozen; the test seam rides on the side.
    object.__setattr__(capture, "release_calls", release_mock.await_args_list)
    return capture


@pytest.mark.asyncio
async def test_console_process_creation_translates_transport_errors() -> None:
    connection = FailingProcessConnection([])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        pytest.raises(HMCCLIError, match="Unable to create the HMC console process"),
    ):
        await _open_capture_stream(make_config(), "mkvterm", _SealedStdin())
    assert connection.closed


@pytest.mark.asyncio
async def test_console_acquisition_read_translates_transport_errors() -> None:
    connection = FakeConnection([])
    connection.create_process = AsyncMock(return_value=FailingReadProcess())
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        pytest.raises(HMCCLIError, match="acquisition read failed"),
    ):
        await _acquire_capture_stream(make_config(), "mkvterm", _SealedStdin())
    assert connection.closed


@pytest.mark.asyncio
async def test_successful_capture_closes_its_connection_once() -> None:
    connection = FakeConnection([FakeProcess(BANNER, b"console output")])
    release_probe_connection = FakeConnection([FakeProcess(BANNER)])

    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(side_effect=[connection, release_probe_connection]),
        ),
        patch(
            "hmcpctl.ssh.console.run_hmc_command",
            AsyncMock(return_value="Close command sent"),
        ),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    ):
        await capture_lpar_console(_client(), "sys1", "lp1", **_capture_kwargs())

    assert connection.close_calls == 1


def test_truncate_backtracks_split_string_terminator() -> None:
    data = b"prefix\x1bPpayload\x1b\\suffix"

    assert _truncate(data, data.index(b"\\")) == b"prefix"


def test_truncate_backtracks_unterminated_osc_payload() -> None:
    data = b"prefix\x1b]0;window title\x07suffix"

    assert _truncate(data, data.index(b"window") + 3) == b"prefix"


# ---------------------------------------------------------------------------
# Bounds validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", 0),
        ("duration_seconds", -1.0),
        ("duration_seconds", MAX_CAPTURE_SECONDS + 1),
        ("max_bytes", 0),
        ("max_bytes", MAX_CAPTURE_BYTES + 1),
        ("idle_timeout_seconds", 0),
    ],
)
async def test_out_of_range_bounds_are_rejected_before_any_ssh(field, value):
    kwargs = _capture_kwargs()
    kwargs[field] = value
    with (
        patch("hmcpctl.ssh.console.open_hmc_connection", AsyncMock()) as connect_mock,
        pytest.raises(ValueError, match=field),
    ):
        await capture_lpar_console(_client(), "sys1", "lp1", **kwargs)
    connect_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["duration_seconds", "idle_timeout_seconds"])
async def test_nan_time_bounds_are_rejected_before_any_ssh(field):
    kwargs = _capture_kwargs()
    kwargs[field] = float("nan")
    with (
        patch("hmcpctl.ssh.console.open_hmc_connection", AsyncMock()) as connect_mock,
        pytest.raises(ValueError, match=field),
    ):
        await capture_lpar_console(_client(), "sys1", "lp1", **kwargs)
    connect_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Contention (P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contention_sentinel_raises_distinct_error_and_never_releases():
    connection = FakeConnection([FakeProcess(CONTENTION)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console.run_hmc_command", AsyncMock()) as release_mock,
        pytest.raises(ConsoleHeldError) as excinfo,
    ):
        await capture_lpar_console(
            _client(), "sys1", "lp1", **_capture_kwargs(idle_timeout_seconds=0.05)
        )
    assert HELD_SENTINEL.decode() in str(excinfo.value)
    # Exit code was 0 on the real HMC; only the sentinel detects this. And
    # since we never held the vterm, releasing would close the other holder.
    release_mock.assert_not_awaited()
    assert connection.closed


def test_sentinel_matches_the_recorded_p1_bytes():
    assert HELD_SENTINEL in CONTENTION


@pytest.mark.asyncio
async def test_late_contention_sentence_releases_own_hold_then_raises():
    # ADR 0172 rule 4: after proven acquisition the hold is the capture's own,
    # so the sentence in console output still raises, but only after release.
    stream = FakeConnection([FakeProcess(BANNER, CONTENTION)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with (
        connect,
        run_command as release,
        probe_seconds,
        pytest.raises(ConsoleHeldError) as excinfo,
    ):
        await capture_lpar_console(
            _client(), "sys1", "lp1", **_capture_kwargs(idle_timeout_seconds=0.2)
        )
    assert "after acquisition" in str(excinfo.value)
    assert "released=True" in str(excinfo.value)
    assert release.await_count == 2  # ours plus the probe's teardown
    assert stream.closed


# ---------------------------------------------------------------------------
# The three client-side bounds and the remaining stop reasons (P8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duration_bound_fires_on_a_silent_stream():
    connection = FakeConnection([FakeProcess(BANNER, None)])
    capture = await _run_capture(
        connection, duration_seconds=0.15, idle_timeout_seconds=5.0
    )
    assert capture.stop_reason == "duration"
    assert capture.data == BANNER


@pytest.mark.asyncio
async def test_idle_bound_fires_after_silence_since_last_byte():
    connection = FakeConnection([FakeProcess(BANNER, None)])
    capture = await _run_capture(connection, duration_seconds=10.0)
    assert capture.stop_reason == "idle"


@pytest.mark.asyncio
async def test_remote_close_stops_the_capture():
    connection = FakeConnection([FakeProcess(BANNER, b"tail")])
    capture = await _run_capture(connection, duration_seconds=10.0)
    assert capture.stop_reason == "remote-close"
    assert capture.data == BANNER + b"tail"
    assert capture.error is None


@pytest.mark.asyncio
async def test_transport_failure_yields_error_stop_reason_and_still_releases():
    class ExplodingStdout(FakeStdout):
        def __init__(self):
            super().__init__(BANNER)

        async def read(self, size: int) -> bytes:
            if self._chunks:
                return await super().read(size)
            raise ConnectionResetError("TCP dropped")

    connection = FakeConnection([FakeProcess()])
    connection._processes[0].stdout = ExplodingStdout()
    capture = await _run_capture(connection)
    assert capture.stop_reason == "error"
    assert capture.data == BANNER
    assert capture.error == "ConnectionResetError: TCP dropped"
    # P3: the HMC does not auto-release after a transport failure.
    assert len(capture.release_calls) >= 1


@pytest.mark.asyncio
async def test_transport_error_detail_is_single_line_and_bounded():
    class ExplodingStdout(FakeStdout):
        def __init__(self):
            super().__init__(BANNER)

        async def read(self, size: int) -> bytes:
            if self._chunks:
                return await super().read(size)
            raise ConnectionResetError("TCP dropped\n\x1b[31m" + "x" * 400)

    connection = FakeConnection([FakeProcess()])
    connection._processes[0].stdout = ExplodingStdout()

    capture = await _run_capture(connection)

    assert capture.error is not None
    assert capture.error.startswith("ConnectionResetError: TCP dropped")
    assert len(capture.error) == 256
    assert "\n" not in capture.error
    assert "\x1b" not in capture.error


# ---------------------------------------------------------------------------
# Release honesty (P2/P3/P4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_proven_by_clean_probe_mkterm():
    # Capture ends by remote close; the probe's mkvterm gets the banner (no
    # sentinel) -> slot free -> released True. Both rmvterm calls happen.
    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(BANNER)])
    capture = await _run_capture(connection)
    assert capture.stop_reason == "remote-close"
    assert capture.released is True
    commands = [call.args[1] for call in capture.release_calls]
    assert any(cmd.startswith("rmvterm -m sys1") for cmd in commands)
    assert len(commands) == 2  # our release + the probe session's teardown


@pytest.mark.asyncio
async def test_release_probe_teardown_failure_is_not_reported_as_released():
    from hmcpctl.ssh.transport import HMCCLIError

    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(BANNER)])
    release = AsyncMock(
        side_effect=["Close command sent", HMCCLIError("probe teardown failed")]
    )
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console.run_hmc_command", release),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    ):
        capture = await capture_lpar_console(
            _client(), "sys1", "lp1", **_capture_kwargs()
        )

    assert release.await_count == 2
    assert capture.released is False


@pytest.mark.asyncio
async def test_rmvterm_exit_zero_alone_is_not_proof():
    # P2: rmvterm says "Close command sent" (exit 0) but the slot is still
    # held — the probe sees the sentinel, so released stays False.
    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(CONTENTION)])
    capture = await _run_capture(connection)
    assert capture.released is False


@pytest.mark.asyncio
async def test_fragmented_probe_contention_sentinel_is_not_acquisition():
    split = len(HELD_SENTINEL) // 2
    contention_chunks = (
        b"\r\n " + HELD_SENTINEL[:split],
        HELD_SENTINEL[split:] + b" \r\n Exiting.... ",
    )
    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(*contention_chunks)])

    capture = await _run_capture(connection)

    assert capture.released is False


@pytest.mark.asyncio
async def test_failed_rmvterm_still_probes_and_reports_honestly():
    # rmvterm fails (HMCCLIError, as run_hmc_command raises); the probe then
    # finds the sentinel — released stays False instead of being asserted.
    from hmcpctl.ssh.transport import HMCCLIError

    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(CONTENTION)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch(
            "hmcpctl.ssh.console.run_hmc_command",
            AsyncMock(side_effect=HMCCLIError("rmvterm failed")),
        ),
    ):
        capture = await capture_lpar_console(
            _client(), "sys1", "lp1", **_capture_kwargs()
        )
    assert capture.released is False


@pytest.mark.asyncio
async def test_release_probe_closes_connection_when_process_start_fails():
    connection = FakeConnection([])
    with patch(
        "hmcpctl.ssh.console.open_hmc_connection",
        AsyncMock(return_value=connection),
    ):
        released = await _probe_released(make_config(), "sys1", "lp1")

    assert released is False
    assert connection.closed is True


@pytest.mark.asyncio
async def test_release_probe_timeout_without_output_does_not_issue_rmvterm():
    connection = FakeConnection([FakeProcess(None)])
    release = AsyncMock()
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console.run_hmc_command", release),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.01),
    ):
        released = await _probe_released(make_config(), "sys1", "lp1")

    assert released is False
    assert connection.closed is True
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_still_runs_release_to_completion():
    # P4: cancelling the task mid-stream must not leak the vterm; the shielded
    # release runs to completion before the cancellation propagates.
    capture_started = asyncio.Event()
    connection = FakeConnection(
        [FakeProcess(BANNER, None, blocked_read_started=capture_started)]
    )
    seen_commands: list[str] = []

    async def fake_run_command(config, cmd):
        seen_commands.append(cmd)
        return "Close command sent"

    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch(
            "hmcpctl.ssh.console.run_hmc_command",
            AsyncMock(side_effect=fake_run_command),
        ),
    ):
        task = asyncio.ensure_future(
            capture_lpar_console(
                _client(),
                "sys1",
                "lp1",
                **_capture_kwargs(duration_seconds=30.0, idle_timeout_seconds=30.0),
            )
        )
        await asyncio.wait_for(capture_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any(cmd.startswith("rmvterm ") for cmd in seen_commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_cancel", [False, True])
async def test_cancellation_during_acquisition_releases_proven_session(stop_on_cancel):
    entered = asyncio.Event()
    finish = asyncio.Event()
    process = FakeProcess(BANNER, None)
    connection = FakeConnection([])

    async def blocked_create_process(command: str, **kwargs):
        connection.create_process_calls.append({"command": command, **kwargs})
        entered.set()
        await finish.wait()
        return process

    connection.create_process = blocked_create_process
    release = AsyncMock(return_value=True)
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console._release_and_verify", release),
    ):
        task = asyncio.create_task(
            capture_lpar_console(
                _client(),
                "sys1",
                "lp1",
                **_capture_kwargs(duration_seconds=30.0, idle_timeout_seconds=30.0),
                stop_on_cancel=stop_on_cancel,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        if stop_on_cancel:
            result = await task
            assert result.stop_reason == "interrupted"
            assert result.released is True
        else:
            with pytest.raises(asyncio.CancelledError):
                await task

    release.assert_awaited_once_with(make_config(), "sys1", "lp1")
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_cancel", [False, True])
async def test_cancellation_during_contended_acquisition_never_releases_holder(
    stop_on_cancel,
):
    entered = asyncio.Event()
    finish = asyncio.Event()
    connection = FakeConnection([])

    async def blocked_create_process(command: str, **kwargs):
        connection.create_process_calls.append({"command": command, **kwargs})
        entered.set()
        await finish.wait()
        return FakeProcess(CONTENTION)

    connection.create_process = blocked_create_process
    release = AsyncMock(return_value=True)
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console._release_and_verify", release),
    ):
        task = asyncio.create_task(
            capture_lpar_console(
                _client(),
                "sys1",
                "lp1",
                **_capture_kwargs(duration_seconds=30.0, idle_timeout_seconds=30.0),
                stop_on_cancel=stop_on_cancel,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        if stop_on_cancel:
            result = await task
            assert result.stop_reason == "interrupted"
            assert result.released is False
        else:
            with pytest.raises(asyncio.CancelledError):
                await task

    release.assert_not_awaited()
    assert connection.closed is True


# ---------------------------------------------------------------------------
# Sealed stdin (P7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdin_is_a_pipe_write_end_that_nothing_can_write():
    connection = FakeConnection([FakeProcess(BANNER), FakeProcess(BANNER)])
    capture = await _run_capture(connection)
    assert capture.released is True
    stream_call = connection.create_process_calls[0]
    read_fd = stream_call["stdin"]
    assert isinstance(read_fd, int) and read_fd >= 0  # the pipe's read end
    assert stream_call["command"].startswith("mkvterm -m ")
    assert not hasattr(capture, "stdin")
    writer = _SealedStdin()
    assert not any(
        name.startswith(("write", "send"))
        for name in dir(writer)
        if not name.startswith("_")
    )
    writer.close()


# ---------------------------------------------------------------------------
# Byte integrity at max_bytes (P6)
# ---------------------------------------------------------------------------


def test_truncation_never_splits_multibyte_utf8():
    data = "abcé".encode()  # é = 0xC3 0xA9 straddling a 4-byte cut
    cut = _truncate(data, 4)
    assert cut == b"abc"


def test_truncation_never_splits_an_incomplete_csi_sequence():
    data = b"ok\x1b[31mred\x1b[0m"
    # Cut inside "\x1b[31m": backtracks to before the ESC.
    assert _truncate(data, 6) == b"ok"
    # The completed sequence plus following text cuts normally.
    assert _truncate(data, 11) == b"ok\x1b[31mred"


def test_truncation_backtracks_over_partial_utf8_then_esc():
    data = "aé".encode() + b"\x1b[1;2Hx"
    # The limit lands inside é and inside the CSI sequence: both backtrack.
    assert _truncate(data, 2) == b"a"
    # The CSI sequence completes at 'H' (index 8), but the cut must fall
    # before the whole incomplete sequence, i.e. before the ESC at index 3.
    assert _truncate(data, 8) == "aé".encode()
    assert _truncate(data, len(data)) == data


def test_truncation_of_pure_ascii_cuts_at_the_limit():
    data = b"x" * 100
    assert _truncate(data, 40) == b"x" * 40


def test_truncation_handles_bare_esc_and_string_sequences():
    assert _truncate(b"ab\x1bcd", 3) == b"ab"  # bare ESC at the cut
    dcs = b"ab\x1bP1;2qdata\x1b\\tail"
    assert _truncate(dcs, 8) == b"ab"  # DCS string not yet terminated
    assert _truncate(dcs, len(dcs)) == dcs  # terminated: no backtrack


@pytest.mark.parametrize("introducer", [b"P", b"X", b"^", b"_"])
def test_truncation_keeps_string_sequence_terminated_at_limit(
    introducer: bytes,
) -> None:
    complete = b"ab\x1b" + introducer + b"payload\x1b\\"
    data = complete + b"tail"

    assert _truncate(data, len(complete)) == complete


@pytest.mark.asyncio
async def test_max_bytes_bound_truncates_to_a_safe_boundary():
    payload = BANNER * 2000  # ~48k of pure ASCII
    connection = FakeConnection([FakeProcess(payload)])
    capture = await _run_capture(connection, max_bytes=1024)
    assert capture.stop_reason == "max_bytes"
    assert len(capture.data) <= 1024


# ---------------------------------------------------------------------------
# MCP surface wiring
# ---------------------------------------------------------------------------


def test_capture_tool_result_carries_contract_fields():
    from hmcpctl.server import TOOL_SECURITY

    security = TOOL_SECURITY["hmc_capture_lpar_console"]
    assert security.operation == "lpar.capture_console"
    assert security.effect == "mutate"
    assert security.exhaustive_targets is True
    arguments = {(t.kind, t.argument) for t in security.targets}
    assert ("lpar", "lpar_name_or_uuid") in arguments
    assert ("managed_system", "system_name_or_uuid") in arguments


def test_base64_round_trip_preserves_raw_bytes():
    raw = bytes(range(256))
    encoded = base64.b64encode(raw).decode("ascii")
    assert base64.b64decode(encoded) == raw


@pytest.mark.parametrize(
    ("system_selector", "lpar_selector", "system_name", "lpar_name"),
    [
        ("system-a", "aix-db", "system-a", "aix-db"),
        (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "resolved-system",
            "resolved-lpar",
        ),
    ],
)
def test_capture_tool_resolves_identity_and_forwards_bounds(
    system_selector: str,
    lpar_selector: str,
    system_name: str,
    lpar_name: str,
):
    from hmcpctl.operations.lpar import console as lpar_console

    client = MagicMock()
    client.config = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    resolve_system_uuid = AsyncMock(return_value="system-uuid")
    resolve_lpar_uuid = AsyncMock(return_value="lpar-uuid")
    resolve_system_name = AsyncMock(return_value="resolved-system")
    resolve_lpar_name = AsyncMock(return_value="resolved-lpar")
    capture = AsyncMock(
        return_value=ConsoleCapture(
            system=system_name,
            lpar=lpar_name,
            data=b"\x00console\xff",
            stop_reason="idle",
            released=True,
        )
    )

    with (
        patch("hmcpctl._app.client_from_env", return_value=context) as factory,
        patch.object(lpar_console, "resolve_system_uuid", resolve_system_uuid),
        patch.object(lpar_console, "resolve_lpar_uuid", resolve_lpar_uuid),
        patch.object(lpar_console, "resolve_system_name", resolve_system_name),
        patch.object(lpar_console, "resolve_lpar_cli_name", resolve_lpar_name),
        patch.object(lpar_console, "capture_lpar_console", capture),
    ):
        result = server_console.hmc_capture_lpar_console(
            lpar_selector,
            system_selector,
            duration_seconds=12.5,
            max_bytes=4096,
            idle_timeout_seconds=3.5,
            profile="lab",
        )

    factory.assert_called_once_with("lab")
    resolve_system_uuid.assert_awaited_once_with(client, system_selector)
    resolve_lpar_uuid.assert_awaited_once_with(
        client, lpar_selector, system_name_or_uuid="system-uuid"
    )
    capture.assert_awaited_once_with(
        client,
        system_name,
        lpar_name,
        duration_seconds=12.5,
        max_bytes=4096,
        idle_timeout_seconds=3.5,
        on_chunk=None,
        stop_on_cancel=False,
    )
    context.__aexit__.assert_awaited_once()
    assert resolve_system_name.await_count == int(system_selector != system_name)
    assert resolve_lpar_name.await_count == int(lpar_selector != lpar_name)
    assert result == {
        "system": system_name,
        "partition": lpar_name,
        "stop_reason": "idle",
        "released": True,
        "error": None,
        "bytes_captured": 9,
        "data_base64": base64.b64encode(b"\x00console\xff").decode("ascii"),
    }


# ---------------------------------------------------------------------------
# Continuous console session (issue #974, ADR 0170)
# ---------------------------------------------------------------------------


def _session_patches(*connections: FakeConnection, release=None):
    """Patch the SSH seams: connections in open order, then rmvterm."""
    run_command = release or AsyncMock(return_value="Close command sent")
    return (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(side_effect=list(connections)),
        ),
        patch("hmcpctl.ssh.console.run_hmc_command", run_command),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    )


@pytest.mark.asyncio
async def test_session_streams_past_capture_ceiling_and_releases():
    chunk = b"x" * 65_536
    count = MAX_CAPTURE_BYTES // len(chunk) + 1
    stream = FakeConnection([FakeProcess(BANNER, None, *([chunk] * count))])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        # A consumer-side timeout leaves the session open and readable.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(session.read(), 0.05)
        received = b"".join([part async for part in session])
        assert session.released is None
        assert await session.close() is True
        assert await session.close() is True

    assert len(received) == count * len(chunk) > MAX_CAPTURE_BYTES
    assert session.released is True
    commands = [call.args[1] for call in release.await_args_list]
    assert commands == ["rmvterm -m sys1 -p lp1"] * 2  # ours + the probe's teardown
    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_session_close_reports_unproven_release():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER

    assert session.released is False


@pytest.mark.asyncio
async def test_session_contention_at_open_never_releases():
    stream = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(stream)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        with pytest.raises(ConsoleHeldError) as excinfo:
            await session.open()
        assert await session.close() is False
        never_opened = ConsoleSession(_client(), "sys1", "lp1")
        assert await never_opened.close() is False

    release.assert_not_awaited()
    assert stream.closed
    assert "mkvterm -m sys1 -p lp1" in str(excinfo.value)
    assert "Only one open session is allowed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_sentence_after_banner_in_one_read_is_acquisition():
    # ADR 0172 rule 3: the banner proves the hold, so a sentence after it in
    # the same read is console content and the capture releases its own hold.
    stream = FakeConnection([FakeProcess(BANNER + CONTENTION)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with (
        connect,
        run_command as release,
        probe_seconds,
        pytest.raises(ConsoleHeldError, match="after acquisition"),
    ):
        await capture_lpar_console(
            _client(), "sys1", "lp1", **_capture_kwargs(idle_timeout_seconds=0.2)
        )
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_sentence_before_banner_in_one_read_is_contention():
    stream = FakeConnection([FakeProcess(CONTENTION + BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream)
    with (
        connect,
        run_command as release,
        probe_seconds,
        pytest.raises(ConsoleHeldError),
    ):
        await ConsoleSession(_client(), "sys1", "lp1").open()

    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_sentence_after_banner_is_acquisition_and_torn_down():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER + CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER

    assert session.released is True
    assert release.await_count == 2  # ours plus the probe's teardown


@pytest.mark.asyncio
async def test_session_take_over_rmvterms_then_acquires():
    events: list[str] = []
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])

    async def connect(config):
        events.append("connect")
        return [stream, probe][events.count("connect") - 1]

    async def rmvterm(config, command):
        events.append(command)
        return "Close command sent"

    with (
        patch("hmcpctl.ssh.console.open_hmc_connection", connect),
        patch("hmcpctl.ssh.console.run_hmc_command", rmvterm),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    ):
        async with ConsoleSession(_client(), "sys1", "lp1", take_over=True) as session:
            assert await session.read() == BANNER

    assert events[:2] == ["rmvterm -m sys1 -p lp1", "connect"]
    assert session.released is True


@pytest.mark.asyncio
async def test_session_take_over_contended_raises_after_one_rmvterm():
    stream = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(stream)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1", take_over=True)
        with pytest.raises(ConsoleHeldError):
            await session.open()
        assert await session.close() is False

    assert release.await_count == 1


@pytest.mark.asyncio
async def test_session_take_over_survives_failed_rmvterm():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    release = AsyncMock(side_effect=[HMCCLIError("rc 1"), "Close command sent", "ok"])
    connect, run_command, probe_seconds = _session_patches(
        stream, probe, release=release
    )
    with connect, run_command, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1", take_over=True) as session:
            assert await session.read() == BANNER

    assert session.released is True


@pytest.mark.asyncio
async def test_session_yields_late_sentinel_as_data():
    stream = FakeConnection([FakeProcess(BANNER, CONTENTION)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            received = [part async for part in session]

    assert received == [BANNER, CONTENTION]
    assert session.released is True
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_session_read_error_propagates_and_close_still_releases():
    class BannerThenFailingStdout(FakeStdout):
        async def read(self, size: int) -> bytes:
            if self._chunks:
                return await super().read(size)
            raise OSError("channel lost")

    process = FakeProcess()
    process.stdout = BannerThenFailingStdout(BANNER)
    stream = FakeConnection([process])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(OSError, match="channel lost"):
            await session.read()
        assert await session.close() is True

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_session_rejects_reopen_and_read_when_not_open():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect as connect_mock, run_command, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        with pytest.raises(RuntimeError):
            await session.read()
        await session.open()
        with pytest.raises(RuntimeError):
            await session.open()
        await session.close()
        with pytest.raises(RuntimeError):
            await session.read()

        closed_first = ConsoleSession(_client(), "sys1", "lp1")
        await closed_first.close()
        with pytest.raises(RuntimeError):
            await closed_first.open()

    assert connect_mock.await_count == 2  # the session and its probe only


@pytest.mark.asyncio
async def test_session_failed_stdin_pipe_leaves_session_closable():
    session = ConsoleSession(_client(), "sys1", "lp1")
    with (
        patch("hmcpctl.ssh.console.os.pipe", side_effect=OSError(24, "EMFILE")),
        patch("hmcpctl.ssh.console.open_hmc_connection", AsyncMock()) as connect_mock,
        pytest.raises(OSError, match="EMFILE"),
    ):
        await session.open()

    assert await session.close() is False
    connect_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_close_during_open_is_refused():
    entered = asyncio.Event()
    finish = asyncio.Event()
    stream = FakeConnection([])

    async def blocked_create_process(command: str, **kwargs):
        entered.set()
        await finish.wait()
        return FakeProcess(BANNER, None)

    stream.create_process = blocked_create_process
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        opening = asyncio.create_task(session.open())
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="in flight"):
            await session.close()
        finish.set()
        await opening
        assert await session.close() is True

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_session_cancellation_releases_before_propagating():
    reading = asyncio.Event()
    stream = FakeConnection([FakeProcess(BANNER, None, blocked_read_started=reading)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    session = ConsoleSession(_client(), "sys1", "lp1")

    async def collect() -> None:
        async with session:
            async for _ in session:
                pass

    with connect, run_command as release, probe_seconds:
        task = asyncio.create_task(collect())
        await asyncio.wait_for(reading.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert session.released is True
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_session_close_survives_cancellation_and_is_idempotent():
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[tuple] = []

    async def slow_release(*args) -> bool:
        calls.append(args)
        started.set()
        await finish.wait()
        return True

    stream = FakeConnection([FakeProcess(BANNER, None)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection", AsyncMock(return_value=stream)
        ),
        patch("hmcpctl.ssh.console._release_and_verify", slow_release),
    ):
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        closing = asyncio.create_task(session.close())
        await asyncio.wait_for(started.wait(), timeout=5)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await session.close() is True

    assert session.released is True
    assert calls == [(make_config(), "sys1", "lp1")]
    assert stream.closed


@pytest.mark.asyncio
async def test_session_close_propagates_cancelled_release():
    async def cancelled_release(*args) -> bool:
        raise asyncio.CancelledError

    stream = FakeConnection([FakeProcess(BANNER, None)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection", AsyncMock(return_value=stream)
        ),
        patch("hmcpctl.ssh.console._release_and_verify", cancelled_release),
    ):
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        with pytest.raises(asyncio.CancelledError):
            await session.close()

    assert session.released is False
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_cancel", [False, True])
async def test_capture_cancelled_during_release_raises_after_release(stop_on_cancel):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[tuple] = []

    async def slow_release(*args) -> bool:
        calls.append(args)
        started.set()
        await finish.wait()
        return True

    stream = FakeConnection([FakeProcess(BANNER)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection", AsyncMock(return_value=stream)
        ),
        patch("hmcpctl.ssh.console._release_and_verify", slow_release),
    ):
        task = asyncio.create_task(
            capture_lpar_console(
                _client(),
                "sys1",
                "lp1",
                **_capture_kwargs(),
                stop_on_cancel=stop_on_cancel,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        if stop_on_cancel:
            result = await task
            assert result.stop_reason == "interrupted"
            assert result.released is True
        else:
            with pytest.raises(asyncio.CancelledError):
                await task

    assert calls == [(make_config(), "sys1", "lp1")]
    assert stream.closed


def test_session_has_no_write_surface():
    session = ConsoleSession(_client(), "sys1", "lp1")
    assert not any(
        name.startswith(("write", "send"))
        for name in dir(session)
        if not name.startswith("_")
    )


# ---------------------------------------------------------------------------
# Mid-session suspension (issue #976, ADR 0173)
# ---------------------------------------------------------------------------


async def _started(task: asyncio.Task, event: asyncio.Event) -> None:
    """Wait until *task* is blocked on the fake stream behind *event*."""
    await asyncio.wait_for(event.wait(), timeout=5)
    assert not task.done()


@pytest.mark.asyncio
async def test_hand_over_keeps_the_hold_and_moves_the_channel():
    blocked = asyncio.Event()
    stream = FakeConnection(
        [
            FakeProcess(
                BANNER, None, b"kgdb", b"after", None, blocked_read_started=blocked
            )
        ]
    )
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER
            collector = asyncio.create_task(session.read())
            await _started(collector, blocked)
            async with session.hand_over() as handover:
                assert await handover.read() == b"kgdb"
                await asyncio.sleep(0)
                assert not collector.done()
            assert await asyncio.wait_for(collector, timeout=5) == b"after"
            assert release.await_count == 0
            with pytest.raises(RuntimeError, match="handover has ended"):
                await handover.read()

    assert release.await_count == 2  # close() only: rmvterm plus the probe's teardown
    assert len(stream.create_process_calls) == 1
    assert session.released is True


@pytest.mark.asyncio
async def test_empty_hand_over_does_not_cancel_collector():
    blocked = asyncio.Event()
    stream = FakeConnection(
        [FakeProcess(BANNER, None, b"next", None, blocked_read_started=blocked)]
    )
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER
            collector = asyncio.create_task(session.read())
            await _started(collector, blocked)
            async with session.hand_over():
                pass
            assert await asyncio.wait_for(collector, timeout=5) == b"next"


@pytest.mark.asyncio
async def test_suspension_state_errors():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        with pytest.raises(RuntimeError):
            await session.suspend()
        with pytest.raises(RuntimeError):
            await session.resume()
        with pytest.raises(RuntimeError):
            async with session.hand_over():
                pass
        async with session:
            with pytest.raises(RuntimeError):
                await session.resume()
            async with session.hand_over():
                with pytest.raises(RuntimeError):
                    await session.suspend()
                with pytest.raises(RuntimeError):
                    async with session.hand_over():
                        pass


@pytest.mark.asyncio
async def test_suspend_releases_and_resume_reacquires():
    blocked = asyncio.Event()
    first = FakeConnection([FakeProcess(BANNER, None, blocked_read_started=blocked)])
    second = FakeConnection([FakeProcess(BANNER, b"more", None)])
    probes = [FakeConnection([FakeProcess(BANNER)]) for _ in range(2)]
    connect, run_command, probe_seconds = _session_patches(
        first, probes[0], second, probes[1]
    )
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER
            collector = asyncio.create_task(session.read())
            await _started(collector, blocked)
            assert await session.suspend() is True
            assert release.await_count == 2
            assert first.closed
            await asyncio.sleep(0)
            assert not collector.done()
            await session.resume()
            assert await asyncio.wait_for(collector, timeout=5) == BANNER
            assert await session.read() == b"more"

    assert session.released is True
    assert release.await_count == 4


@pytest.mark.asyncio
async def test_resume_contention_stays_suspended_and_close_skips_rmvterm():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    taken = [FakeConnection([FakeProcess(CONTENTION)]) for _ in range(2)]
    connect, run_command, probe_seconds = _session_patches(stream, probe, *taken)
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1", take_over=True)
        await session.open()
        assert await session.read() == BANNER
        assert await session.suspend() is True
        after_takeover_rmvterm = release.await_count  # 3: takeover, ours, probe's
        waiting = asyncio.create_task(session.read())
        with pytest.raises(ConsoleHeldError):
            await session.resume()
        assert release.await_count == after_takeover_rmvterm  # resume never rmvterms
        with pytest.raises(ConsoleHeldError):
            await session.resume()
        assert await session.close() is True
        assert await asyncio.wait_for(waiting, timeout=5) == b""

    assert release.await_count == after_takeover_rmvterm == 3
    assert all(connection.closed for connection in taken)


@pytest.mark.asyncio
async def test_cancel_inside_hand_over_releases():
    blocked = asyncio.Event()
    stream = FakeConnection([FakeProcess(BANNER, None, blocked_read_started=blocked)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    session = ConsoleSession(_client(), "sys1", "lp1")

    async def hold() -> None:
        async with session, session.hand_over() as handover:
            await handover.read()

    with connect, run_command as release, probe_seconds:
        task = asyncio.create_task(hold())
        await _started(task, blocked)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert session.released is True
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_suspend_completes_release():
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[tuple] = []

    async def slow_release(*args) -> bool:
        calls.append(args)
        started.set()
        await finish.wait()
        return True

    stream = FakeConnection([FakeProcess(BANNER, None)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection", AsyncMock(return_value=stream)
        ),
        patch("hmcpctl.ssh.console._release_and_verify", slow_release),
    ):
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        suspending = asyncio.create_task(session.suspend())
        await asyncio.wait_for(started.wait(), timeout=5)
        suspending.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await suspending
        assert await session.close() is True

    assert calls == [(make_config(), "sys1", "lp1")]
    assert stream.closed


def _blocked_resume_stream() -> tuple[FakeConnection, asyncio.Event, asyncio.Event]:
    entered = asyncio.Event()
    finish = asyncio.Event()
    stream = FakeConnection([])

    async def blocked_create_process(command: str, **kwargs):
        entered.set()
        await finish.wait()
        return FakeProcess(BANNER, None)

    stream.create_process = blocked_create_process
    return stream, entered, finish


@pytest.mark.asyncio
async def test_cancelled_resume_releases_new_hold():
    first = FakeConnection([FakeProcess(BANNER, None)])
    second, entered, finish = _blocked_resume_stream()
    probes = [FakeConnection([FakeProcess(BANNER)]) for _ in range(2)]
    connect, run_command, probe_seconds = _session_patches(
        first, probes[0], second, probes[1]
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.suspend() is True
        resuming = asyncio.create_task(session.resume())
        await asyncio.wait_for(entered.wait(), timeout=5)
        resuming.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await resuming

    assert session.released is True
    assert release.await_count == 4
    assert second.closed


@pytest.mark.asyncio
async def test_close_during_resume_releases_new_hold():
    first = FakeConnection([FakeProcess(BANNER, None)])
    second, entered, finish = _blocked_resume_stream()
    probes = [FakeConnection([FakeProcess(BANNER)]) for _ in range(2)]
    connect, run_command, probe_seconds = _session_patches(
        first, probes[0], second, probes[1]
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.suspend() is True
        resuming = asyncio.create_task(session.resume())
        await asyncio.wait_for(entered.wait(), timeout=5)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        with pytest.raises(RuntimeError, match="closed during resume"):
            await resuming
        assert await closing is True

    assert release.await_count == 4
    assert second.closed


@pytest.mark.asyncio
async def test_close_during_suspend_waits_and_reports_its_proof():
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[tuple] = []

    async def slow_release(*args) -> bool:
        calls.append(args)
        started.set()
        await finish.wait()
        return True

    stream = FakeConnection([FakeProcess(BANNER, None)])
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection", AsyncMock(return_value=stream)
        ),
        patch("hmcpctl.ssh.console._release_and_verify", slow_release),
    ):
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        waiting = asyncio.create_task(session.read())
        suspending = asyncio.create_task(session.suspend())
        await asyncio.wait_for(started.wait(), timeout=5)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        assert await suspending is True
        assert await closing is True
        assert await asyncio.wait_for(waiting, timeout=5) == b""

    assert calls == [(make_config(), "sys1", "lp1")]
    assert stream.closed


def test_handover_has_no_write_surface():
    assert not any(
        name.startswith(("write", "send"))
        for name in dir(ConsoleHandover)
        if not name.startswith("_")
    )


# ---------------------------------------------------------------------------
# Drop detection and opt-in reconnect (issue #977, ADR 0174)
# ---------------------------------------------------------------------------

DROP = asyncssh.ConnectionLost("Server not responding to keepalive")
KEEPALIVE_GAP = ConsoleGap("ConnectionLost: Server not responding to keepalive", False)


def _reconnecting(take_over: bool = False) -> ConsoleSession:
    return ConsoleSession(_client(), "sys1", "lp1", take_over=take_over, reconnect=True)


@pytest.mark.asyncio
async def test_reconnect_after_transport_error_yields_gap_then_new_stream():
    first = FakeConnection([FakeProcess(BANNER, b"before", DROP)])
    second = FakeConnection([FakeProcess(BANNER, b"after", None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command as release, probe_seconds:
        async with _reconnecting() as session:
            items = [await session.read() for _ in range(5)]
            assert release.await_count == 0
            assert first.closed

    assert items == [BANNER, b"before", KEEPALIVE_GAP, BANNER, b"after"]
    assert session.released is True


@pytest.mark.asyncio
async def test_reconnect_after_eof_on_closed_connection():
    first = FakeConnection([FakeProcess(BANNER)])
    second = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command as release, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            first.closed = (
                True  # asyncssh closed the connection before the reader resumed
            )
            assert await session.read() == ConsoleGap(
                "the SSH connection closed", False
            )
            assert await session.read() == BANNER

    assert release.await_count == 2  # close() and its probe only


@pytest.mark.asyncio
async def test_eof_on_open_connection_is_latched_remote_close():
    stream = FakeConnection([FakeProcess(BANNER)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect as opener, run_command, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            assert await session.read() == b""
            stream.closed = (
                True  # a connection closing after a remote close is not a drop
            )
            assert await session.read() == b""
        assert opener.await_count == 2  # the stream and the release probe


@pytest.mark.asyncio
async def test_reconnect_into_held_vterm_raises_typed_error_without_rmvterm():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    held = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(first, held)
    with connect, run_command as release, probe_seconds:
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(ConsoleHeldAfterDropError, match="take_over=True") as caught:
            await session.read()
        assert isinstance(caught.value, ConsoleHeldError)
        with pytest.raises(RuntimeError):
            await session.read()
        with pytest.raises(RuntimeError):
            await session.suspend()
        assert await session.close() is False

    assert release.await_count == 0
    assert held.closed


@pytest.mark.asyncio
async def test_reconnect_with_take_over_reclaims_and_reports_it():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    second = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command as release, probe_seconds:
        async with _reconnecting(take_over=True) as session:
            assert await session.read() == BANNER
            gap = await session.read()
            assert release.await_count == 2  # the takeover at open, then at reconnect
            assert await session.read() == BANNER

    assert gap == ConsoleGap(KEEPALIVE_GAP.error, took_over=True)
    assert release.await_count == 4


@pytest.mark.asyncio
async def test_failed_reconnect_raises_hmccli_error():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    connect, run_command, probe_seconds = _session_patches(first)
    with connect as opener, run_command as release, probe_seconds:
        opener.side_effect = [first, HMCCLIError("SSH connection timed out")]
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(HMCCLIError, match="reconnect .* timed out"):
            await session.read()
        assert await session.close() is False

    assert release.await_count == 0


@pytest.mark.asyncio
async def test_read_timeout_during_reconnect_keeps_the_gap():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    second, entered, finish = _blocked_resume_stream()
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(session.read(), 0.05)
            assert entered.is_set()
            with pytest.raises(RuntimeError):
                async with session.hand_over():
                    pass
            finish.set()
            reconnect = session._reconnect_task
            assert reconnect is not None
            while not reconnect.done():
                await asyncio.sleep(0)
            with pytest.raises(RuntimeError):
                await session.suspend()  # the unread gap comes first
            assert await asyncio.wait_for(session.read(), 5) == KEEPALIVE_GAP
            assert await session.read() == BANNER

    assert session.released is True


@pytest.mark.asyncio
async def test_close_during_reconnect_releases_the_new_hold():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    second, entered, finish = _blocked_resume_stream()
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command as release, probe_seconds:
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        reading = asyncio.create_task(session.read())
        await asyncio.wait_for(entered.wait(), timeout=5)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        assert await closing is True
        assert await asyncio.wait_for(reading, timeout=5) == b""

    assert release.await_count == 2
    assert second.closed


class _GatedDropStdout:
    """Yields the banner, then raises DROP once *gate* is set."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
        self._banner_sent = False

    async def read(self, size: int) -> bytes:
        if not self._banner_sent:
            self._banner_sent = True
            return BANNER
        await self._gate.wait()
        raise DROP


@pytest.mark.asyncio
async def test_drop_during_close_starts_no_reconnect():
    gate, started, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    process = FakeProcess()
    process.stdout = _GatedDropStdout(gate)  # type: ignore[assignment]
    stream = FakeConnection([process])

    async def slow_release(*args) -> bool:
        started.set()
        await finish.wait()
        return True

    opener = AsyncMock(side_effect=[stream])
    with (
        patch("hmcpctl.ssh.console.open_hmc_connection", opener),
        patch("hmcpctl.ssh.console._release_and_verify", slow_release),
    ):
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        reading = asyncio.create_task(session.read())
        await asyncio.sleep(0)
        closing = asyncio.create_task(session.close())
        assert (
            await asyncio.wait_for(reading, timeout=5) == b""
        )  # close() preempts it (#1004)
        gate.set()  # the drop meets close()'s scan of unread bytes
        await asyncio.wait_for(started.wait(), timeout=5)
        finish.set()
        assert await closing is True

    assert opener.await_count == 1


@pytest.mark.asyncio
async def test_drop_inside_hand_over_reaches_holder_then_collector_reconnects():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    second = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command as release, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            async with session.hand_over() as handover:
                with pytest.raises(asyncssh.ConnectionLost):
                    await handover.read()
            assert not second.create_process_calls  # no reconnect under the holder
            first.closed = True
            assert isinstance(await session.read(), ConsoleGap)
            assert await session.read() == BANNER

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_resume_on_reconnect_session_raises_plain_contention():
    stream = FakeConnection([FakeProcess(BANNER, None)])
    probe = FakeConnection([FakeProcess(BANNER)])
    taken = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(stream, probe, taken)
    with connect, run_command, probe_seconds:
        session = _reconnecting()
        await session.open()
        assert await session.suspend() is True
        with pytest.raises(ConsoleHeldError) as caught:
            await session.resume()
        assert not isinstance(caught.value, ConsoleHeldAfterDropError)
        assert await session.close() is True


@pytest.mark.asyncio
async def test_reader_cancelled_as_reconnect_completes_keeps_the_gap():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    second, entered, finish = _blocked_resume_stream()
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(first, second, probe)
    with connect, run_command, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            reading = asyncio.create_task(session.read())
            await asyncio.wait_for(entered.wait(), timeout=5)
            reconnect = session._reconnect_task
            assert reconnect is not None
            finish.set()
            while not reconnect.done():
                await asyncio.sleep(0)
            reading.cancel()  # the consumer's timeout lands in the same tick
            with pytest.raises(asyncio.CancelledError):
                await reading
            assert await session.read() == KEEPALIVE_GAP
            assert await session.read() == BANNER


@pytest.mark.asyncio
async def test_take_over_reconnect_into_held_vterm_says_rmvterm_ran():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    held = FakeConnection([FakeProcess(CONTENTION)])
    connect, run_command, probe_seconds = _session_patches(first, held)
    with connect, run_command as release, probe_seconds:
        session = _reconnecting(take_over=True)
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(ConsoleHeldAfterDropError, match="rmvterm ran first"):
            await session.read()
        assert await session.close() is False

    assert release.await_count == 2  # the takeovers at open and at reconnect


@pytest.mark.asyncio
async def test_drop_as_suspend_starts_reconnects_nothing():
    gate = asyncio.Event()
    process = FakeProcess()
    process.stdout = _GatedDropStdout(gate)  # type: ignore[assignment]
    stream = FakeConnection([process])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect as opener, run_command, probe_seconds:
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        reading = asyncio.create_task(session.read())
        await asyncio.sleep(0)  # the collector blocks on the stream
        gate.set()
        await asyncio.sleep(0)  # the stream read fails; the collector has not resumed
        assert await session.suspend() is True
        await asyncio.sleep(0)
        assert not reading.done()  # the collector waits while suspended
        assert opener.await_count == 2  # the stream and the release probe, no reconnect
        assert await session.close() is True
        assert await asyncio.wait_for(reading, timeout=5) == b""


@pytest.mark.asyncio
async def test_reconnect_pipe_failure_leaves_session_dropped():
    first = FakeConnection([FakeProcess(BANNER, DROP)])
    connect, run_command, probe_seconds = _session_patches(first)
    pipes = [_SealedStdin, OSError(24, "Too many open files")]

    def next_pipe():
        made = pipes.pop(0)
        if isinstance(made, OSError):
            raise made
        return made()

    with (
        connect,
        run_command as release,
        probe_seconds,
        patch("hmcpctl.ssh.console._SealedStdin", side_effect=next_pipe),
    ):
        session = _reconnecting()
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(OSError, match="Too many open files"):
            await session.read()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(session.read(), timeout=5)
        assert await session.close() is False

    assert release.await_count == 0


@pytest.mark.asyncio
async def test_remote_close_latch_ends_with_its_stream():
    stream = FakeConnection([FakeProcess(BANNER)])
    probe = FakeConnection([FakeProcess(BANNER)])
    resumed = FakeConnection([FakeProcess(BANNER, DROP)])
    after = FakeConnection([FakeProcess(BANNER, None)])
    probe2 = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(
        stream, probe, resumed, after, probe2
    )
    with connect, run_command, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            assert await session.read() == b""  # latched remote close
            assert await session.suspend() is True
            await session.resume()
            assert await session.read() == BANNER
            assert await session.read() == KEEPALIVE_GAP
            assert await session.read() == BANNER


# ---------------------------------------------------------------------------
# Console input: writable session, SysRq, exclusive raw mode (issue #958, ADR 0176)
# ---------------------------------------------------------------------------


def _writable(**kwargs) -> WritableConsoleSession:
    return WritableConsoleSession(_client(), "sys1", "lp1", **kwargs)


def _audited():
    """Patch the console-write emitter; its calls are the audit trail under test."""
    return patch("hmcpctl.ssh.console.audit.record_console_write")


@pytest.mark.parametrize("surface", [ConsoleSession, ConsoleHandover, _SealedStdin])
def test_read_only_surfaces_have_no_write_surface(surface):
    assert not any(
        name.startswith(("write", "send"))
        for name in dir(surface)
        if not name.startswith("_")
    )


@pytest.mark.asyncio
async def test_writable_session_writes_through_a_private_pipe_and_sends_eof_only_at_release():
    process = FakeProcess(BANNER, None)
    stream = FakeConnection([process])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect, run_command, probe_seconds, _audited():
        async with _writable() as session:
            await session.write(b"x")
            assert not hasattr(session, "stdin")
            assert process.stdin.eof is False
        assert session.released is True

    assert stream.create_process_calls[0]["stdin"] == asyncssh.PIPE
    assert process.stdin.written == [b"x"]
    assert process.stdin.drains == 1
    assert process.stdin.eof is True


@pytest.mark.asyncio
async def test_every_write_is_audited_first_and_carries_no_content():
    process = FakeProcess(BANNER, None)
    order: list[str] = []
    process.stdin.write = lambda data: order.append("write")
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command, probe_seconds, _audited() as record:
        record.side_effect = lambda **_: order.append("audit")
        async with _writable() as session:
            await session.write(b"secret-password\r")
            await session.send_sysrq("c", prefix=b"\x0f")

    assert order == ["audit", "write", "audit", "write"]
    first, second = (call.kwargs for call in record.call_args_list)
    assert first == {
        "system": "sys1",
        "lpar": "lp1",
        "host": make_config().host,
        "mode": "shared",
        "input_kind": "raw",
        "length": 16,
        "agent_id": "hmcpctl",
    }
    assert (second["input_kind"], second["length"]) == ("sysrq", 2)
    assert "secret" not in repr(record.call_args_list)


@pytest.mark.parametrize(
    ("kwargs", "written"),
    [
        # The default: Ctrl-O, verified live on V10R3 M1060 / FW950 (#1149).
        ({}, b"\x0fh"),
        ({"prefix": b"\x1b\x0f"}, b"\x1b\x0fh"),
    ],
    ids=["default-ctrl-o", "override"],
)
@pytest.mark.asyncio
async def test_send_sysrq_is_one_write_of_prefix_and_key(kwargs, written):
    process = FakeProcess(BANNER, None)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command, probe_seconds, _audited():
        async with _writable() as session:
            await session.send_sysrq("h", **kwargs)

    assert process.stdin.written == [written]


@pytest.mark.parametrize(
    ("key", "prefix", "error"),
    [
        ("", b"\x0f", ValueError),
        ("cc", b"\x0f", ValueError),
        (" ", b"\x0f", ValueError),
        ("\u00e9", b"\x0f", ValueError),
        ("c", b"", ValueError),
        ("c", "\x0f", TypeError),
    ],
)
@pytest.mark.asyncio
async def test_send_sysrq_rejects_a_bad_key_or_prefix_before_auditing(
    key, prefix, error
):
    process = FakeProcess(BANNER, None)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command, probe_seconds, _audited() as record:
        async with _writable() as session:
            with pytest.raises(error):
                await session.send_sysrq(key, prefix=prefix)

    record.assert_not_called()
    assert process.stdin.written == []


@pytest.mark.parametrize(("data", "error"), [(b"", ValueError), ("x", TypeError)])
@pytest.mark.asyncio
async def test_write_rejects_empty_or_non_bytes_data(data, error):
    process = FakeProcess(BANNER, None)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command, probe_seconds, _audited() as record:
        async with _writable() as session:
            with pytest.raises(error):
                await session.write(data)

    record.assert_not_called()


@pytest.mark.asyncio
async def test_raw_mode_is_exclusive_and_returns_to_collection():
    process = FakeProcess(BANNER, b"$OK#9a", b"after", None)
    stream = FakeConnection([process])
    probe = FakeConnection([FakeProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(stream, probe)
    with connect as opener, run_command as release, probe_seconds, _audited() as record:
        async with _writable() as session:
            assert await session.read() == BANNER
            async with session.raw_mode() as channel:
                assert isinstance(channel, ConsoleRawChannel)
                collector = asyncio.create_task(session.read())
                assert await channel.read() == b"$OK#9a"
                await channel.write(b"$g#67")
                with pytest.raises(RuntimeError):
                    await session.write(b"x")
                with pytest.raises(RuntimeError):
                    await session.send_sysrq("c", prefix=b"\x0f")
                await asyncio.sleep(0)
                assert not collector.done()
                assert release.await_count == 0
            assert await asyncio.wait_for(collector, 5) == b"after"
            with pytest.raises(RuntimeError):
                await channel.write(b"late")
            await session.write(b"y")
        assert opener.await_count == 2  # the stream and the release probe only

    assert process.stdin.written == [b"$g#67", b"y"]
    modes = [call.kwargs["mode"] for call in record.call_args_list]
    assert modes == ["exclusive", "shared"]


@pytest.mark.asyncio
async def test_raw_mode_refuses_while_another_pause_is_active():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, None)]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command, probe_seconds, _audited():
        async with _writable() as session, session.hand_over():
            with pytest.raises(RuntimeError):
                async with session.raw_mode():
                    pass


@pytest.mark.asyncio
async def test_writes_are_refused_inside_hand_over_while_suspended_and_after_close():
    first = FakeProcess(BANNER, None)
    second = FakeProcess(BANNER, None)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([first]),
        FakeConnection([FakeProcess(BANNER)]),  # suspend()'s release probe
        FakeConnection([second]),
        FakeConnection([FakeProcess(BANNER)]),  # close()'s release probe
    )
    with connect, run_command, probe_seconds, _audited() as record:
        session = _writable()
        await session.open()
        async with session.hand_over():
            with pytest.raises(RuntimeError):
                await session.write(b"x")
        assert await session.suspend() is True
        with pytest.raises(RuntimeError):
            await session.write(b"x")
        await session.resume()
        await session.write(b"z")  # a resumed session writes to its new stream
        assert await session.close() is True
        with pytest.raises(RuntimeError):
            await session.write(b"x")

    assert record.call_count == 1
    assert first.stdin.written == []
    assert second.stdin.written == [b"z"]


@pytest.mark.asyncio
async def test_writes_are_refused_while_dropped_and_follow_a_reconnect():
    first = FakeProcess(BANNER, DROP)
    second = FakeProcess(BANNER, None)
    gate = asyncio.Event()

    class GatedConnection(FakeConnection):
        async def create_process(self, command: str, **kwargs):
            process = await super().create_process(command, **kwargs)
            await gate.wait()
            return process

    reconnecting = GatedConnection([second])
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([first]), reconnecting, FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command, probe_seconds, _audited():
        async with _writable(reconnect=True) as session:
            assert await session.read() == BANNER
            with pytest.raises(
                TimeoutError
            ):  # the reconnect keeps running for the next read
                await asyncio.wait_for(session.read(), 0.05)
            with pytest.raises(RuntimeError):
                await session.write(b"x")  # the reconnect is in flight
            gate.set()
            async with asyncio.timeout(5):
                while session._state != "held":
                    await asyncio.sleep(0)
            with pytest.raises(RuntimeError):
                await session.write(b"x")  # held again, but the gap is still unread
            assert await session.read() == KEEPALIVE_GAP
            await session.write(b"y")

    assert first.stdin.written == []
    assert second.stdin.written == [b"y"]


@pytest.mark.asyncio
async def test_writes_are_refused_after_a_reconnect_meets_a_held_console():
    first = FakeProcess(BANNER, DROP)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([first]), FakeConnection([FakeProcess(CONTENTION)])
    )
    with connect, run_command, probe_seconds, _audited() as record:
        session = _writable(reconnect=True)
        await session.open()
        assert await session.read() == BANNER
        with pytest.raises(ConsoleHeldAfterDropError):
            await session.read()
        with pytest.raises(RuntimeError):
            await session.write(b"x")
        assert await session.close() is False

    record.assert_not_called()


@pytest.mark.asyncio
async def test_a_drop_in_raw_mode_reaches_the_raw_holder_and_reconnects_after_the_block():
    first = FakeProcess(BANNER, DROP)
    second = FakeProcess(BANNER, None)

    def broken(data: bytes) -> None:
        raise BrokenPipeError("channel closed")

    first.stdin.write = broken
    dead = FakeConnection([first])
    connect, run_command, probe_seconds = _session_patches(
        dead, FakeConnection([second]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect as opener, run_command as release, probe_seconds, _audited():
        async with _writable(reconnect=True) as session:
            assert await session.read() == BANNER
            async with session.raw_mode() as channel:
                with pytest.raises(asyncssh.ConnectionLost):
                    await channel.read()
                with pytest.raises(BrokenPipeError):
                    await channel.write(b"$g#67")  # unwrapped, and no reconnect starts
                assert opener.await_count == 1
                dead.closed = True
            assert await session.read() == ConsoleGap(
                "the SSH connection closed", False
            )
            await session.write(b"y")
            with pytest.raises(RuntimeError):
                await channel.write(b"late")
            assert release.await_count == 0

    assert second.stdin.written == [b"y"]


# ---------------------------------------------------------------------------
# Lost hold: another client's rmvterm (issue #1004)
# ---------------------------------------------------------------------------

_TRANSCRIPT = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "console" / "lost-hold-transcript.json"
    ).read_text()
)


def _holder_chunks(run: dict) -> list[bytes]:
    return [
        event["data"].encode("latin-1")
        for event in run["events"]
        if event["event"] == "A-stdout-chunk"
    ]


LOST = _holder_chunks(_TRANSCRIPT["runs"][0])[-1]


def test_sentinel_matches_recorded_transcript():
    assert [_holder_chunks(run)[-1] for run in _TRANSCRIPT["runs"]] == [
        LOST_HOLD_SENTINEL + b" "
    ] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks", [(LOST,), (LOST[:40], LOST[40:])], ids=["whole", "split"]
)
async def test_lost_hold_raises_and_close_skips_rmvterm(chunks):
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, *chunks, None)])
    )
    with connect as opener, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        assert b"".join([await session.read() for _ in chunks]) == LOST
        with pytest.raises(ConsoleHoldLostError, match="close\\(\\) issued no rmvterm"):
            await asyncio.wait_for(session.read(), 1)
        assert await session.close() is False

    assert release.await_count == 0
    assert opener.await_count == 1


@pytest.mark.asyncio
async def test_unread_lost_hold_still_skips_rmvterm():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, LOST, None)])
    )
    with connect as opener, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER

    assert session.released is False
    assert release.await_count == 0
    assert opener.await_count == 1


@pytest.mark.asyncio
async def test_relayed_lost_hold_text_keeps_rmvterm():
    relayed = LOST.replace(b"\n", b"\r\n")
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, relayed, None)]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER
            assert await session.read() == relayed

    assert session.released is True
    assert release.await_count == 2  # close() and its probe


@pytest.mark.asyncio
async def test_release_survives_a_failing_scan():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, RuntimeError("in-band signal"), None)]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command as release, probe_seconds:
        async with ConsoleSession(_client(), "sys1", "lp1") as session:
            assert await session.read() == BANNER

    assert session.released is True
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_lost_hold_never_reconnects():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, LOST, None)])
    )
    with connect as opener, run_command as release, probe_seconds:
        async with _reconnecting() as session:
            assert await session.read() == BANNER
            assert await session.read() == LOST
            with pytest.raises(ConsoleHoldLostError):
                await asyncio.wait_for(session.read(), 1)
        assert opener.await_count == 1

    assert release.await_count == 0


@pytest.mark.asyncio
async def test_capture_reports_lost_hold_as_error_without_rmvterm():
    capture = await _run_capture(FakeConnection([FakeProcess(BANNER, LOST, None)]))

    assert capture.stop_reason == "error"
    assert capture.error is not None and "ConsoleHoldLostError" in capture.error
    assert capture.released is False
    assert capture.release_calls == []


@pytest.mark.asyncio
async def test_suspend_after_unread_lost_hold_skips_rmvterm():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, LOST, None)])
    )
    with connect as opener, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        assert await session.suspend() is False
        assert await session.close() is False

    assert release.await_count == 0
    assert opener.await_count == 1


@pytest.mark.asyncio
async def test_capture_reports_lost_hold_as_error_when_a_bound_fires_on_the_report():
    capture = await _run_capture(
        FakeConnection([FakeProcess(BANNER, LOST, None)]),
        max_bytes=len(BANNER) + len(LOST),
    )

    assert capture.stop_reason == "error"
    assert capture.error is not None and capture.error.startswith(
        "ConsoleHoldLostError"
    )
    assert capture.released is False
    assert capture.release_calls == []


# ---------------------------------------------------------------------------
# Release through stdin EOF (issue #1058)
# ---------------------------------------------------------------------------

_EOF_TRANSCRIPT = json.loads(
    (
        Path(__file__).parents[1]
        / "fixtures"
        / "console"
        / "eof-release-transcript.json"
    ).read_text()
)


def test_eof_release_transcript_records_eof_exit_and_survival():
    runs = _EOF_TRANSCRIPT["runs"]
    arms = {run["arm"] for run in runs}
    assert arms == {"single", "single-fast", "takeover", "race", "eof-first"}
    for run in runs:
        assert _holder_chunks(run)[-1] == EXITED
        events = {event["event"]: event for event in run["events"]}
        if run["arm"].startswith("single"):
            assert events["C-probe"]["released"] is True
        else:
            assert events["C-probe"]["released"] is False
            assert events["B-reads-after-A-eof"]["chunks"][-1] == (
                "<timeout: B stream open and silent>"
            )
            assert events["B-closed"]["released"] is True


def _session_kind(kind: str) -> ConsoleSession:
    if kind == "writable":
        return _writable()
    return ConsoleSession(_client(), "sys1", "lp1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sealed", "writable"])
async def test_close_releases_through_eof_without_rmvterm(kind):
    process, probe = EofProcess(BANNER), EofProcess(BANNER)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([probe])
    )
    with connect, run_command as release, probe_seconds:
        session = _session_kind(kind)
        await session.open()
        assert await session.read() == BANNER
        assert not process.eof.is_set()
        assert await session.close() is True

    assert process.eof.is_set() and probe.eof.is_set()
    release.assert_not_awaited()  # the session and its probe both released through EOF (#1072)


@pytest.mark.asyncio
async def test_suspend_releases_through_eof():
    process, probe = EofProcess(BANNER), EofProcess(BANNER)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([process]), FakeConnection([probe])
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.suspend() is True
        assert await session.close() is True

    assert process.eof.is_set() and probe.eof.is_set()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_hold_during_eof_drain_skips_rmvterm():
    process = EofProcess(BANNER, after_eof=(LOST,))
    connect, run_command, probe_seconds = _session_patches(FakeConnection([process]))
    with connect as opener, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.close() is False

    assert process.eof.is_set()
    assert release.await_count == 0
    assert opener.await_count == 1  # no probe after a loss


@pytest.mark.asyncio
async def test_eof_timeout_falls_back_to_rmvterm():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER, None)]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.close() is True

    assert release.await_count == 2  # ours after the EOF wait, then the probe's


@pytest.mark.asyncio
async def test_eof_read_error_falls_back_to_rmvterm():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([EofProcess(BANNER, after_eof=(OSError("channel lost"),))]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.close() is True

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_stream_ended_before_release_uses_rmvterm():
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER)]), FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert await session.read() == BANNER
        assert await session.read() == b""
        assert await session.close() is True

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_connection_closed_during_eof_drain_falls_back_to_rmvterm():
    stream = FakeConnection([EofProcess(BANNER)])
    connect, run_command, probe_seconds = _session_patches(
        stream, FakeConnection([FakeProcess(BANNER)])
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        stream.closed = True  # the stream ends because the connection dropped
        assert await session.close() is True

    assert release.await_count == 2


@pytest.mark.asyncio
async def test_resume_after_an_ended_stream_releases_through_eof_again():
    resumed = EofProcess(BANNER)
    connect, run_command, probe_seconds = _session_patches(
        FakeConnection([FakeProcess(BANNER)]),
        FakeConnection([FakeProcess(BANNER)]),
        FakeConnection([resumed]),
        FakeConnection([FakeProcess(BANNER)]),
    )
    with connect, run_command as release, probe_seconds:
        session = ConsoleSession(_client(), "sys1", "lp1")
        await session.open()
        assert (
            await session.suspend() is True
        )  # ended stream: rmvterm, then the probe's
        await session.resume()
        assert await session.close() is True

    assert resumed.eof.is_set()
    assert release.await_count == 3


# ---------------------------------------------------------------------------
# The release probe's own teardown through stdin EOF (issue #1072)
# ---------------------------------------------------------------------------


_PROBE_EOF_TRANSCRIPT = json.loads(
    (
        Path(__file__).parents[1]
        / "fixtures"
        / "console"
        / "probe-eof-release-transcript.json"
    ).read_text()
)


def test_probe_eof_transcript_records_teardown_without_rmvterm():
    runs = {run["arm"]: run["events"] for run in _PROBE_EOF_TRANSCRIPT["runs"]}
    assert set(runs) == {"cost", "race"}
    for events in runs.values():
        probes = [event for event in events if event["event"] == "probe"]
        assert all(
            p["commands"] == ([] if p["mode"] == "eof" else ["rmvterm"]) for p in probes
        )
        exits = [e["data"] for e in events if e["event"] == "P-stdout-chunk"]
        assert exits.count(EXITED.decode()) == sum(p["mode"] == "eof" for p in probes)
    race = {event["event"]: event for event in reversed(runs["race"])}  # first of each
    assert race["B-acquired"]["t"] > race["P-stdout-eof"]["t"]
    assert (
        race["B-reads-after-probe"]["chunks"][-1]
        == "<timeout: B stream open and silent>"
    )
    assert race["C-probe"] == {**race["C-probe"], "released": False, "commands": []}
    assert race["B-closed"] == {**race["B-closed"], "released": True, "commands": []}


async def _probe(
    *processes: FakeProcess, closed: bool = False
) -> tuple[bool, AsyncMock, FakeConnection]:
    connection = FakeConnection(list(processes))
    connection.closed = closed
    release = AsyncMock(return_value="Close command sent")
    with (
        patch(
            "hmcpctl.ssh.console.open_hmc_connection",
            AsyncMock(return_value=connection),
        ),
        patch("hmcpctl.ssh.console.run_hmc_command", release),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.2),
    ):
        released = await _probe_released(make_config(), "sys1", "lp1")
    return released, release, connection


@pytest.mark.asyncio
async def test_release_probe_tears_down_through_eof_without_rmvterm():
    process = EofProcess(BANNER)
    released, release, connection = await _probe(process)

    assert released is True
    assert process.eof.is_set()
    release.assert_not_awaited()
    assert connection.closed is True


@pytest.mark.asyncio
async def test_release_probe_eof_timeout_falls_back_to_rmvterm():
    released, release, _ = await _probe(FakeProcess(BANNER, None))

    assert released is True
    assert [call.args[1] for call in release.await_args_list] == [
        "rmvterm -m sys1 -p lp1"
    ]


@pytest.mark.asyncio
async def test_release_probe_eof_read_error_falls_back_to_rmvterm():
    released, release, _ = await _probe(
        EofProcess(BANNER, after_eof=(OSError("channel lost"),))
    )

    assert released is True
    assert release.await_count == 1


@pytest.mark.asyncio
async def test_release_probe_closed_connection_falls_back_to_rmvterm():
    released, release, _ = await _probe(EofProcess(BANNER), closed=True)

    assert released is True
    assert release.await_count == 1


@pytest.mark.asyncio
async def test_release_probe_stream_ended_before_eof_uses_rmvterm():
    # An end the probe did not ask for proves nothing about its hold (P3), as for the session.
    released, release, _ = await _probe(FakeProcess(BANNER))

    assert released is True
    assert release.await_count == 1


@pytest.mark.asyncio
async def test_release_probe_lost_hold_during_eof_skips_rmvterm():
    # Another client's rmvterm ended the probe's hold; the stream then stays silent (#1004).
    split = len(LOST_HOLD_SENTINEL) // 2
    process = FakeProcess(BANNER, LOST[:split], LOST[split:], None)
    released, release, _ = await _probe(process)

    assert released is True
    release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [True, False])
@pytest.mark.parametrize("data", [b"", b"partial\x00\xff", HELD_SENTINEL])
async def test_opt_in_cancel_returns_partial_data_and_release_proof(released, data):
    started = asyncio.Event()
    stream = FakeConnection(
        [
            FakeProcess(
                BANNER, *([data] if data else []), None, blocked_read_started=started
            )
        ]
    )
    probe = FakeConnection([FakeProcess(BANNER if released else CONTENTION)])
    with (
        _session_patches(stream, probe)[0],
        patch(
            "hmcpctl.ssh.console.run_hmc_command",
            AsyncMock(return_value="Close command sent"),
        ),
        patch("hmcpctl.ssh.console._RELEASE_PROBE_SECONDS", 0.01),
    ):
        task = asyncio.create_task(
            capture_lpar_console(
                _client(),
                "sys1",
                "lp1",
                **_capture_kwargs(duration_seconds=30.0, idle_timeout_seconds=30.0),
                stop_on_cancel=True,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        result = await asyncio.wait_for(task, 2)
    assert result.stop_reason == "interrupted"
    assert result.released is released
    assert result.data == _truncate(BANNER + data, MAX_CAPTURE_BYTES)
    assert stream.closed and probe.closed
