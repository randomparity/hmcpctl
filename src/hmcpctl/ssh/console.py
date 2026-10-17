"""LPAR console sessions and bounded capture over the HMC ``mkvterm`` CLI.

The HMC exposes exactly one virtual terminal (vterm) per partition through the
``mkvterm``/``rmvterm`` CLI pair over SSH. ``mkvterm`` never exits on its own:
it streams the partition console until torn down, so it structurally cannot
run through :func:`hmcpctl.ssh.transport.run_hmc_command` (a one-shot exec that collects
output until the remote command exits). :class:`ConsoleSession` holds the vterm
on top of :func:`hmcpctl.ssh.transport.open_hmc_connection` with no duration or
byte cap and owns its acquisition and proven release (ADR 0170), including
mid-session suspension for a preempting hold (ADR 0173) and an opt-in
reconnect after a dropped connection (ADR 0174);
:func:`capture_lpar_console` is its bounded consumer and enforces issue #385's
design contract. Every invariant below traces to a recorded observation from
the P1-P8 live-hardware prototype on that issue (HMC V10R3 M1060); ADR 0072
records the design decision per prototype fact.

- **Contention** (P1): a held vterm is reported on *stdout* with exit code 0,
  so the exit status proves nothing; the sentinel sentence below is parsed
  instead and :class:`ConsoleHeldError` is raised, quoting what the HMC printed.
  No ``rmvterm`` is issued on that path — it would release the *other* holder's
  session — unless the caller explicitly asked for a forced takeover (ADR 0172).
- **Mandatory release** (P2/P3/P4): the HMC does not auto-release a vterm,
  not after an abrupt disconnect and not after a graceful close. A release
  therefore runs on every exit path, cancellation included, and runs to
  completion before cancellation propagates. It sends stdin EOF, which ends
  only this session's own ``mkvterm`` about 10 s later (#1058), and issues
  ``rmvterm`` only when the stream had already ended or did not end in time.
  ``released`` is ``True`` only after an independent-session ``mkvterm`` probe
  proves the slot is free; ``rmvterm``'s own exit code is not proof (P2). The
  probe releases its own hold the same way, through stdin EOF (#1072). A
  hold another client's ``rmvterm`` already ended gets no release (#1004): the
  HMC reports it in band, and ``rmvterm`` would end the new holder's session.
- **Sealed stdin** (P5/P7): mkvterm's stdin is the write socket to the
  partition console, and EOF on it terminates the vterm. The capture opens a
  pipe, hands mkvterm the read end, and holds the write end open without ever
  writing until the release closes it: no parameter, method, or code path can
  send a byte to the console. :class:`WritableConsoleSession` is the one
  exception (ADR 0176): it keeps a private stdin writer that sends EOF only at
  release, and only its typed, audited write methods reach it.
- **Client-side bounds** (P8): an idle vterm stream stays open forever and
  the HMC sends no keepalives, so duration, max-bytes, and idle bounds are all
  enforced here, never expected from the HMC. hmcpctl's own SSH keepalives
  close a connection whose peer is gone (ADR 0174).

The captured bytes are returned raw: truncation backtracks to a boundary that
cannot split a multi-byte UTF-8 sequence or an incomplete ANSI escape
sequence, but decoding remains the caller's decision (issue #385).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import math
import os
import shlex
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, TypeVar

import asyncssh

from hmcpctl.client.core import HMCClient

from ..audit import records as audit
from ..config import HMCConfig
from ..errors import HMCError
from .transport import HMCCLIError, open_hmc_connection, run_hmc_command

logger = logging.getLogger(__name__)

#: The terminal outcomes of a bounded capture. ``contention`` is not a stop
#: reason: it raises :class:`ConsoleHeldError` instead, per issue #385's
#: requirement that "another session holds the vterm" be a distinct error.
StopReason = Literal[
    "duration", "max_bytes", "idle", "remote-close", "error", "interrupted"
]

_State = Literal[
    "new",
    "opening",
    "held",
    "suspending",
    "suspended",
    "resuming",
    "reconnecting",
    "dropped",
    "unheld",
    "lost",
]

#: The distinctive sentence of the contention message (P1). The full recorded
#: stdout is three CRLF lines, each beginning and ending with a space::
#:
#:     b"\r\n A terminal session is already open for this partition. \r\n"
#:     b" Only one open session is allowed for a partition. \r\n"
#:     b" Exiting.... "
#:
#: Matching the sentence alone keeps detection robust against those cosmetic
#: quirks while staying anchored to the exact recorded bytes.
HELD_SENTINEL = b"A terminal session is already open for this partition."
ACQUIRED_SENTINEL = b"Open in progress"

#: What the HMC sends a holder once another client's ``rmvterm`` ended its hold
#: (#1004, recorded on HMC V10R3 M1060, followed there by one space). The channel
#: then stays open and silent: no EOF and no exit status arrive.
LOST_HOLD_SENTINEL = (
    b"\r\n Connection has closed \r\n\r\n\r\n"
    b" This session is no longer connected. Please close this window.\n\n\n"
)

#: How long the release probe waits for its own ``mkvterm`` to speak before
#: giving up (P1/P5: both the contention text and the HMC banner arrive
#: immediately on a healthy HMC).
_RELEASE_PROBE_SECONDS = 10.0

#: How long a release scans bytes already received but unread for
#: :data:`LOST_HOLD_SENTINEL` before it issues ``rmvterm``: per read, and in total.
_UNREAD_READ_SECONDS = 0.1
_UNREAD_SCAN_SECONDS = 1.0

#: How long a release waits for ``mkvterm`` to exit after its stdin reaches EOF
#: (#1058). V10R3 M1060 answered EOF in 10.1-10.5 s over six recorded runs.
_EOF_RELEASE_SECONDS = 20.0

#: The states in which :meth:`ConsoleSession._release_hold` runs: close() and suspend().
_RELEASABLE: tuple[_State, ...] = ("held", "suspending")

#: Upper caps for the MCP tool surface. The bounds are caller-chosen, but a
#: capture runs inside the MCP server process, so memory (``max_bytes``) and
#: wall clock (``duration_seconds``) get hard ceilings.
MAX_CAPTURE_BYTES = 1_048_576
MAX_CAPTURE_SECONDS = 3600.0

_CHUNK = 65_536
_ERROR_DETAIL_MAX_CHARS = 256


class ConsoleHeldError(HMCError):
    """Another session already holds the partition's vterm (issue #385).

    The HMC allows exactly one open vterm per partition (P1: signalled on
    stdout, always with exit code 0). This error is deliberately distinct
    from :class:`hmcpctl.ssh.transport.HMCCLIError`. No ``rmvterm`` is issued
    against another holder's session on this path; only an explicit
    ``ConsoleSession(..., take_over=True)`` does that (ADR 0172). When the
    capture sees the sentence after it proved acquisition, it raises this after
    releasing its own hold.
    """


class ConsoleHeldAfterDropError(ConsoleHeldError):
    """A reconnect after a dropped connection found the vterm held (ADR 0174).

    Most likely the session's own leftover hold (P3), but hmcpctl cannot prove
    it, so no ``rmvterm`` was issued. Recover with :meth:`ConsoleSession.close`
    and a new session opened with ``take_over=True``.
    """


class ConsoleHoldLostError(HMCError):
    """Another client's ``rmvterm`` ended this session's hold (#1004).

    The HMC reported it with :data:`LOST_HOLD_SENTINEL`. :meth:`ConsoleSession.close`
    then issues no ``rmvterm``: it would end the new holder's session.
    """


@dataclass(frozen=True)
class ConsoleGap:
    """Marks where console output may be missing after a reconnect (ADR 0174).

    ``error`` says why the old connection counted as dropped; ``took_over`` is
    ``True`` when ``rmvterm`` ran before re-acquisition.
    """

    error: str
    took_over: bool


@dataclass(frozen=True)
class ConsoleCapture:
    """Raw captured console bytes plus the outcome facts (issue #385).

    ``released`` is honest, not optimistic: ``True`` only when an independent
    follow-up ``mkvterm`` proved the vterm slot free after the mandatory
    release, stdin EOF or ``rmvterm`` (P2, #1058). ``False`` means the caller
    may have left the partition's console held and should treat further
    console access as broken, unless
    ``error`` names :class:`ConsoleHoldLostError`: then another client ended
    the hold and no ``rmvterm`` was issued (#1004).
    """

    system: str
    lpar: str
    data: bytes
    stop_reason: StopReason
    released: bool
    error: str | None = None


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Mark a background task's outcome retrieved; its reader re-raises it."""
    if not task.cancelled():
        task.exception()


def _validate_bounds(
    duration_seconds: float, max_bytes: int, idle_timeout_seconds: float
) -> None:
    """Reject non-positive or over-ceiling bounds with actionable messages."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be positive, got {duration_seconds}")
    if duration_seconds > MAX_CAPTURE_SECONDS:
        raise ValueError(
            f"duration_seconds must not exceed {MAX_CAPTURE_SECONDS:.0f}, "
            f"got {duration_seconds}"
        )
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if max_bytes > MAX_CAPTURE_BYTES:
        raise ValueError(
            f"max_bytes must not exceed {MAX_CAPTURE_BYTES}, got {max_bytes}"
        )
    if not math.isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0:
        raise ValueError(
            f"idle_timeout_seconds must be positive, got {idle_timeout_seconds}"
        )


class _SealedStdin:
    """The write end of the capture's stdin pipe, sealed by construction (P7).

    mkvterm's stdin is the write socket to the partition console, and EOF on
    it terminates the vterm (P5) — which is why ``DEVNULL`` cannot be used.
    The pipe's read end is handed to the remote process and its write end is
    held open here, never written. The descriptor pair is private, no method
    sends data, and only :meth:`release` and :meth:`close` touch them: there is
    no API surface through which a byte could reach the console. :meth:`release`
    closes the write end so the HMC ends this session's ``mkvterm`` (#1058).
    """

    __slots__ = ("_read_fd", "_write_fd")

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()

    @property
    def source(self) -> int:
        """What ``create_process(stdin=...)`` receives: the pipe's read end."""
        return self._read_fd

    def adopt(self, process: Any) -> None:
        """Record that asyncssh adopted the read end (it closes it now)."""
        self._read_fd = -1

    def release(self) -> None:
        """Send EOF by closing the write end: the HMC then ends this ``mkvterm``."""
        self.close()

    def close(self) -> None:
        """Close both ends. The read end is skipped once asyncssh owns it."""
        if self._read_fd != -1:
            try:
                os.close(self._read_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            self._read_fd = -1
        if self._write_fd != -1:
            try:
                os.close(self._write_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            self._write_fd = -1


class _ConsoleStdin:
    """A writable session's private stdin writer (ADR 0176).

    asyncssh's stdin pipe replaces the sealed OS pipe, and :meth:`adopt` keeps
    the process's writer. EOF ends the vterm (P5), so only :meth:`release`, which
    the session's release calls, sends it (#1058); :meth:`close` only drops the
    reference.
    """

    __slots__ = ("_writer",)

    source = asyncssh.PIPE

    def __init__(self) -> None:
        self._writer: Any = None

    def adopt(self, process: Any) -> None:
        """Keep the process's stdin writer."""
        self._writer = process.stdin

    async def write(self, data: bytes) -> None:
        """Queue *data* on the channel and wait for asyncssh to drain it."""
        self._writer.write(data)
        await self._writer.drain()

    def release(self) -> None:
        """Send EOF: the HMC then ends this session's ``mkvterm`` (#1058)."""
        self._writer.write_eof()

    def close(self) -> None:
        """Drop the writer without sending EOF."""
        self._writer = None


_Stdin = _SealedStdin | _ConsoleStdin


def _utf8_safe_cut(data: bytes, cut: int) -> int:
    """Backtrack *cut* (with ``cut < len(data)``) off any UTF-8 sequence."""
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    if cut > 0 and data[cut - 1] >= 0xC0:
        # A lead byte immediately before the cut starts a sequence the cut
        # would split; move the cut in front of it.
        cut -= 1
    return cut


def _escape_complete(data: bytes, start: int, cut: int) -> bool:
    """True when the ESC sequence at *start* has its final byte before *cut*.

    ECMA-48 shapes, protocol-derived (P6): ESC + introducer + parameter/
    intermediate bytes + final byte (``0x40``-``0x7E``), with the DCS/SOS/PM/
    APC string forms terminated by ``ESC \\``.
    """
    index = start + 1
    if index >= cut:
        return False  # a bare ESC at the cut is incomplete by definition
    introducer = data[index]
    if introducer == 0x5B:  # CSI: parameter/intermediate bytes then final
        index += 1
        while index < cut and 0x20 <= data[index] <= 0x3F:
            index += 1
        return index < cut and 0x40 <= data[index] <= 0x7E
    if introducer in (0x50, 0x58, 0x5E, 0x5F, 0x5D):  # DCS/SOS/PM/APC/OSC strings
        return data.find(b"\x1b\\", index + 1, cut) != -1 or (
            introducer == 0x5D and data.find(b"\x07", index + 1, cut) != -1
        )
    if 0x20 <= introducer <= 0x2F:  # intermediates then a final 0x30-0x7E
        index += 1
        while index < cut and 0x20 <= data[index] <= 0x2F:
            index += 1
        return index < cut and 0x30 <= data[index] <= 0x7E
    return True  # ESC + one final byte (ESC 7, ESC c, ...)


def _ansi_safe_cut(data: bytes, cut: int) -> int:
    """Backtrack *cut* off an incomplete ANSI escape sequence.

    Protocol-derived, not prototype-verified (P6): every live observation was
    7-bit ASCII with no escapes; only a live install stream exercises this.
    """
    start = data.find(b"\x1b", 0, cut)
    while start != -1:
        if not _escape_complete(data, start, cut):
            return start
        start = data.find(b"\x1b", start + 1, cut)
    return cut


def _truncate(data: bytes, limit: int) -> bytes:
    """Cut *data* to at most *limit* bytes without splitting a sequence."""
    if len(data) <= limit:
        return bytes(data)
    cut = _utf8_safe_cut(data, limit)
    cut = _ansi_safe_cut(data, cut)
    return bytes(data[:cut])


async def _collect_output(
    session: ConsoleSession,
    duration_seconds: float,
    max_bytes: int,
    idle_timeout_seconds: float,
    on_chunk: Callable[[bytes], None] | None = None,
    stop_on_cancel: bool = False,
) -> tuple[bytes, StopReason, str | None]:
    """Read the stream until one of the three client-side bounds fires (P8).

    Read errors (transport drops included) end the collection with
    ``stop_reason="error"`` and whatever bytes arrived; the release path
    still runs, since P3 showed the HMC never reclaims the vterm itself.

    When *on_chunk* is provided it is called with each raw chunk as it
    arrives, before the chunk is appended to the internal buffer.  This
    lets the CLI write bytes to its sink in real time rather than waiting
    for the capture to finish (issue #1674).
    """
    loop = asyncio.get_running_loop()
    buf = bytearray()
    deadline = loop.time() + duration_seconds
    idle_deadline = loop.time() + idle_timeout_seconds
    while True:
        if len(buf) >= max_bytes:
            return bytes(buf), "max_bytes", None
        now = loop.time()
        if now >= deadline:
            return bytes(buf), "duration", None
        if now >= idle_deadline:
            return bytes(buf), "idle", None
        try:
            chunk = await asyncio.wait_for(
                session.read(),
                min(deadline, idle_deadline) - now,
            )
        except TimeoutError:
            continue  # loop top decides whether duration or idle fired
        except asyncio.CancelledError:
            if stop_on_cancel:
                return bytes(buf), "interrupted", None
            raise
        except Exception as exc:  # noqa: BLE001 - a mid-capture read failure ends the capture as an error result, not a raise
            logger.error("console stream read failed mid-capture: %s", exc)
            return bytes(buf), "error", _error_detail(exc)
        if isinstance(chunk, ConsoleGap):
            raise TypeError("bounded capture never reconnects")
        if not chunk:
            return bytes(buf), "remote-close", None
        if on_chunk is not None:
            on_chunk(chunk)
        buf += chunk
        idle_deadline = loop.time() + idle_timeout_seconds


def _error_detail(error: Exception) -> str:
    """Return a bounded, single-line diagnostic safe for tool responses."""
    message = " ".join(
        "".join(
            character if character.isprintable() else " " for character in str(error)
        ).split()
    )
    detail = type(error).__name__
    if message:
        detail = f"{detail}: {message}"
    if len(detail) <= _ERROR_DETAIL_MAX_CHARS:
        return detail
    return detail[: _ERROR_DETAIL_MAX_CHARS - 1] + "…"


async def _release_and_verify(
    config: HMCConfig, system_name: str, lpar_name: str
) -> bool:
    """Issue ``rmvterm``, then prove release with a fresh ``mkvterm`` (P2).

    ``rmvterm``'s exit code is not proof; only an independent-session
    ``mkvterm`` starting without the contention sentinel is.
    """
    await _rmvterm(config, system_name, lpar_name)
    return await _probe_released(config, system_name, lpar_name)


async def _rmvterm(config: HMCConfig, system_name: str, lpar_name: str) -> None:
    """Issue ``rmvterm``; a failure is only logged, since its exit code proves nothing (P2)."""
    quoted = f"rmvterm -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)}"
    try:
        await run_hmc_command(config, quoted)
    except HMCCLIError as exc:
        logger.warning(
            "rmvterm for %s/%s failed (%s); a following mkvterm decides the outcome",
            system_name,
            lpar_name,
            exc,
        )


async def _probe_released(config: HMCConfig, system_name: str, lpar_name: str) -> bool:
    """Prove the vterm slot is free by opening a fresh ``mkvterm`` (P2).

    Outcomes:

    - sentinel seen → still held → ``False`` (no ``rmvterm``: it would close
      whoever holds it);
    - the probe's ``mkvterm`` starts and stays alive → slot proven free →
      ``True``; the probe then tears its own session down, since the HMC does
      not auto-release (P3): stdin EOF, then ``rmvterm`` only when its stream
      does not end in time (#1072, :func:`_release_probe_by_eof`);
    - timeout with no output → state unknown → ``False``; no destructive cleanup
      is attempted because ownership of the slot was never established;
    - clean EOF without the sentinel → the remote ``mkvterm`` exited without
      acquiring → ``False`` (unproven, nothing of ours to release).
    """
    mkvterm_command = (
        f"mkvterm -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)}"
    )
    rmvterm_command = (
        f"rmvterm -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)}"
    )
    stdin = _SealedStdin()
    try:
        try:
            connection, process = await _open_capture_stream(
                config, mkvterm_command, stdin
            )
        except Exception as exc:  # noqa: BLE001 - a probe that cannot start mkvterm is unproven, not fatal
            logger.warning(
                "release probe for %s/%s could not start mkvterm: %s",
                system_name,
                lpar_name,
                exc,
            )
            return False
        eof_released = False
        try:
            outcome = await _read_release_probe(process)
            if outcome == "acquired":
                eof_released = await _release_probe_by_eof(stdin, process, connection)
        finally:
            stdin.close()
            connection.close()
        if outcome == "held":
            return False
        if outcome == "remote-exited":
            logger.warning(
                "release probe for %s/%s exited without proof of release",
                system_name,
                lpar_name,
            )
            return False
        if outcome == "acquired":
            if eof_released:
                return True
            try:
                await run_hmc_command(config, rmvterm_command)
            except HMCCLIError as exc:
                logger.error(
                    "release probe for %s/%s could not tear down its own "
                    "mkvterm (%s); the probe's vterm may be leaked",
                    system_name,
                    lpar_name,
                    exc,
                )
                return False
            return True
        # Timeout with no output: ownership is unknown. Another caller may have
        # acquired the slot after this probe disconnected, so rmvterm would race
        # with and terminate a session we do not own.
        logger.warning(
            "release probe for %s/%s produced no output within %ss; "
            "'released' is unproven",
            system_name,
            lpar_name,
            _RELEASE_PROBE_SECONDS,
        )
        return False
    finally:
        stdin.close()


async def _release_probe_by_eof(
    stdin: _SealedStdin, process: Any, connection: Any
) -> bool:
    """Send the probe's stdin EOF and read until its ``mkvterm`` exits (#1072).

    ``True`` once the stream ended on an open connection, or the HMC reported that
    another client's ``rmvterm`` ended the probe's hold (#1004): either way nothing
    of the probe's is left held, and an ``rmvterm`` could end another client's
    session. A stream that had already ended before EOF, the bound, a failure to
    send EOF or read, or a closed connection return ``False``, and the caller
    falls back to ``rmvterm``, as a session's release does (#1058).
    """
    if process.stdout.at_eof():
        return False
    tail = b""
    with contextlib.suppress(Exception):
        stdin.release()
        async with asyncio.timeout(_EOF_RELEASE_SECONDS):
            while True:
                chunk = await process.stdout.read(_CHUNK)
                if not chunk:
                    return not connection.is_closed()
                window = tail + chunk
                if LOST_HOLD_SENTINEL in window:
                    return True
                tail = window[-(len(LOST_HOLD_SENTINEL) - 1) :]
    return False


def _acquisition_outcome(data: bytes | bytearray) -> Literal["acquired", "held"] | None:
    """Classify ``mkvterm`` output by whichever sentinel came first (P1, ADR 0172).

    The banner proves the hold, so a contention sentence after it is console
    content; P1's contention text replaces the banner.
    """
    acquired = data.find(ACQUIRED_SENTINEL)
    held = data.find(HELD_SENTINEL)
    if held != -1 and (acquired == -1 or held < acquired):
        return "held"
    return "acquired" if acquired != -1 else None


async def _read_release_probe(
    process: Any,
) -> Literal["acquired", "held", "remote-exited", "unproven"]:
    """Classify one bounded probe stream without making ownership decisions."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RELEASE_PROBE_SECONDS
    output = bytearray()
    while (remaining := deadline - loop.time()) > 0:
        try:
            chunk = await asyncio.wait_for(process.stdout.read(_CHUNK), remaining)
        except TimeoutError:
            break
        except Exception:  # noqa: BLE001 - transport trouble leaves ownership unproven
            break
        if not chunk:
            return "remote-exited"
        output += chunk
        if outcome := _acquisition_outcome(output):
            return outcome
    return "unproven"


async def _open_capture_stream(
    config: HMCConfig, command: str, stdin: _Stdin
) -> tuple[Any, Any]:
    """Open the connection and the ``mkvterm`` process on *stdin*.

    Returns ``(connection, process)`` with ownership transferred to the
    caller. Any failure closes what was opened and re-raises.
    """
    connection = await open_hmc_connection(config)
    try:
        process = await connection.create_process(
            command, stdin=stdin.source, encoding=None
        )
    except (asyncssh.Error, OSError) as exc:
        connection.close()
        raise HMCCLIError(
            f"Unable to create the HMC console process for {command!r}: {exc}"
        ) from exc
    except BaseException:
        connection.close()
        raise
    stdin.adopt(process)
    return connection, process


async def _acquire_capture_stream(
    config: HMCConfig, command: str, stdin: _Stdin
) -> tuple[Any, Any, bytes]:
    """Open ``mkvterm`` and wait for proof that this caller acquired the slot."""
    connection, process = await _open_capture_stream(config, command, stdin)
    try:
        async with asyncio.timeout(_RELEASE_PROBE_SECONDS):
            data = bytearray()
            while True:
                try:
                    chunk = await process.stdout.read(_CHUNK)
                except (asyncssh.Error, OSError) as exc:
                    connection.close()
                    raise HMCCLIError(
                        "HMC console acquisition read failed before the console "
                        f"was confirmed: {exc}"
                    ) from exc
                if not chunk:
                    raise HMCCLIError(
                        "mkvterm exited before confirming console acquisition"
                    )
                data += chunk
                outcome = _acquisition_outcome(data)
                if outcome == "held":
                    report = " ".join(bytes(data).decode("ascii", "replace").split())
                    raise ConsoleHeldError(
                        f"{command} found the console held by another session; "
                        f"the HMC reported: {report[:_ERROR_DETAIL_MAX_CHARS]!r}"
                    )
                if outcome == "acquired":
                    return connection, process, bytes(data)
    except TimeoutError as exc:
        connection.close()
        raise HMCCLIError(
            "mkvterm did not confirm console acquisition within "
            f"{_RELEASE_PROBE_SECONDS:g} seconds"
        ) from exc
    except BaseException:
        connection.close()
        raise


async def _await_acquisition(
    config: HMCConfig, command: str, stdin: _Stdin
) -> tuple[Any, Any, bytes, bool]:
    """Shield the ownership handshake and report whether cancellation arrived."""
    task = asyncio.create_task(_acquire_capture_stream(config, command, stdin))
    cancelled = False
    while True:
        try:
            connection, process, data = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                task.result()
            cancelled = True
            continue
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        return connection, process, data, cancelled


async def _await_uninterrupted(task: asyncio.Task[bool]) -> bool:
    """Await *task* to completion even if the caller is cancelled, then re-raise that."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                task.result()  # the task itself was cancelled
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return result


class ConsoleHandover:
    """Exclusive read access to a held session's channel (ADR 0173 mode a).

    Yielded by :meth:`ConsoleSession.hand_over`. The session keeps the vterm
    held; like the session, a handover has no write surface.
    """

    __slots__ = ("_session",)

    def __init__(self, session: ConsoleSession) -> None:
        self._session = session

    async def read(self) -> bytes:
        """Return the next raw chunk, or ``b""`` once the remote end has closed.

        Raises:

            ConsoleHoldLostError: Another client's ``rmvterm`` ended the hold.
            RuntimeError: The handover has ended, including a read still
                pending when the ``hand_over()`` block exits.
        """
        chunk = await self._session._read_for(self)
        if chunk is None:
            raise RuntimeError("the console handover has ended")
        return chunk


_Handover = TypeVar("_Handover", bound=ConsoleHandover)


class ConsoleRawChannel(ConsoleHandover):
    """Exclusive raw access to a writable session's channel (ADR 0176).

    Yielded by :meth:`WritableConsoleSession.raw_mode`. It reads like a
    :class:`ConsoleHandover` and adds :meth:`write`. While it is active, the
    session's own writes raise.
    """

    __slots__ = ("_writable",)

    def __init__(self, session: WritableConsoleSession) -> None:
        super().__init__(session)
        self._writable = session

    async def write(self, data: bytes) -> None:
        """Send raw *data*; audited as an ``exclusive`` ``raw`` write.

        Raises:

            RuntimeError: Raw mode has ended, or the session cannot write.
        """
        await self._writable._write_for(self, data, mode="exclusive", input_kind="raw")


class ConsoleSession:
    """A read-only hold on one partition's vterm with no duration or byte cap.

    ADR 0170 is the contract. :meth:`open` returns once ``mkvterm`` proves
    this session acquired the vterm and raises :class:`ConsoleHeldError`,
    issuing no ``rmvterm``, when another session holds it (P1). With
    ``take_over=True`` (never the default) :meth:`open` first issues ``rmvterm``
    to end whatever holds the vterm, then acquires as usual (ADR 0172). :meth:`read`
    returns raw chunks, the acquisition bytes first and ``b""`` after the
    remote end closes; the session enforces no bound, so consumers wrap reads
    in their own timeouts. :meth:`close` and :meth:`suspend` are the only
    releases: stdin EOF (``rmvterm`` if the stream does not end within
    ``_EOF_RELEASE_SECONDS``, #1058), an independent-session probe, then local
    teardown, run to completion even when the caller is cancelled (P2/P3/P4). Use the
    session as an async context manager so every exit that unwinds the owning
    coroutine closes it.

    A preempting hold pauses collection in one of two modes (ADR 0173):
    :meth:`hand_over` moves the channel to an in-process holder while the vterm
    stays held, and :meth:`suspend`/:meth:`resume` release the vterm for an
    external holder and acquire it again. :meth:`read` waits while paused.

    With ``reconnect=True`` (never the default; the bounded capture never sets
    it), a dropped connection seen by the collector's read starts one
    re-acquisition, and :meth:`read` then yields a :class:`ConsoleGap` before
    the new stream (ADR 0174). A vterm still held after the drop raises
    :class:`ConsoleHeldAfterDropError` with no ``rmvterm``; under P3 that is
    the usual outcome unless ``take_over=True`` is also set, which reclaims it.
    A drop inside :meth:`hand_over` reaches the handover's reader and is
    reconnected after the block; a suspended session holds no connection.

    Process exit: the library installs no ``atexit`` hook or signal handler.
    SIGKILL, ``os._exit``, a crash, default-action SIGTERM, a second SIGINT
    under :func:`asyncio.run`, an event loop stopped or shut down before
    :meth:`close` finishes, and a session dropped without :meth:`close` all
    leave the vterm held, because the HMC never releases it (P3). The next
    :meth:`open` then raises :class:`ConsoleHeldError`; ``rmvterm -m <system>
    -p <partition>`` recovers the console.

    stdin is sealed by construction (:class:`_SealedStdin`, P5/P7): no
    attribute or method of a session writes to the partition console. Only
    :class:`WritableConsoleSession` can write (ADR 0176).
    """

    def __init__(
        self,
        hmc: HMCClient,
        system_name: str,
        lpar_name: str,
        *,
        take_over: bool = False,
        reconnect: bool = False,
    ) -> None:
        self._config = hmc.config
        self._take_over = take_over
        self._reconnect = reconnect
        self._system = system_name
        self._lpar = lpar_name
        self._state: _State = "new"
        self._stdin: _Stdin | None = None
        self._connection: Any = None
        self._stdout: Any = None  # only the read side of the process is kept
        self._pending = b""
        self._close_task: asyncio.Task[bool] | None = None
        self._released: bool | None = None
        self._owner: object | None = None  # the session itself, a handover, or None
        self._collecting = (
            asyncio.Event()
        )  # set only while the session owns the channel
        self._inflight: asyncio.Future[bytes] | None = None
        self._release_proof = False
        self._settled = asyncio.Event()  # clear only while suspend() or resume() runs
        self._settled.set()
        self._remote_closed = (
            False  # latched: a remote close never later counts as a drop
        )
        self._tail = b""  # the stream's last bytes, for a sentinel split across reads
        # The unread scan found the stream ended; unlike _remote_closed, it does not
        # depend on the reconnect path.
        self._stream_ended = False
        self._reconnect_task: asyncio.Task[ConsoleGap] | None = None

    @property
    def released(self) -> bool | None:
        """``None`` before :meth:`close`; then ``True`` only on proven release."""
        return self._released

    async def open(self) -> None:
        """Acquire the vterm, or raise without releasing another holder's session.

        With ``take_over=True``, ``rmvterm`` runs first and ends any other
        holder's session; a failed ``rmvterm`` is only logged, and acquisition
        decides. Contention after it raises with no second ``rmvterm``.

        Raises:

            ConsoleHeldError: Another session holds the vterm (P1); the message
                quotes the HMC output.
            HMCCLIError: ``mkvterm`` could not start or never confirmed acquisition.
            RuntimeError: The session was already opened or closed.
        """
        if self._state != "new" or self._close_task is not None:
            raise RuntimeError("a console session opens once and never after close()")
        await self._finish_acquire(
            await self._acquire("unheld", take_over=self._take_over)
        )

    async def _acquire(self, fallback: _State, *, take_over: bool = False) -> bool:
        """Acquire the vterm; return whether cancellation arrived meanwhile."""
        stdin = self._stdin = self._new_stdin()
        transitions: dict[_State, _State] = {
            "unheld": "opening",
            "suspended": "resuming",
            "dropped": "reconnecting",
        }
        self._state = transitions[fallback]
        command = f"mkvterm -m {shlex.quote(self._system)} -p {shlex.quote(self._lpar)}"
        try:
            if take_over:
                logger.warning(
                    "forced takeover of the console of %s/%s", self._system, self._lpar
                )
                await _rmvterm(self._config, self._system, self._lpar)
            connection, process, data, cancelled = await _await_acquisition(
                self._config, command, stdin
            )
        except BaseException:
            self._state = fallback
            stdin.close()
            raise
        self._connection, self._stdout = connection, process.stdout
        self._state = "held"
        self._remote_closed = False  # the latch covers one mkvterm stream
        self._stream_ended = False
        self._tail = b""
        self._pending += data
        return cancelled

    def _new_stdin(self) -> _Stdin:
        return _SealedStdin()

    async def _finish_acquire(self, cancelled: bool) -> None:
        """Hand the channel to the collector, or release a cancelled acquisition."""
        if cancelled:
            released = await self.close()
            logger.warning(
                "console acquisition on %s/%s was cancelled; released=%s",
                self._system,
                self._lpar,
                released,
            )
            raise asyncio.CancelledError
        self._give_channel(self)

    def _give_channel(self, owner: object | None) -> None:
        """Make *owner* the only reader, preempting a read in flight."""
        self._owner = owner
        if self._inflight is not None:
            self._inflight.cancel()
        if owner is self:
            self._collecting.set()
        else:
            self._collecting.clear()

    async def _read_for(self, owner: object) -> bytes | None:
        """Read one chunk for *owner*; ``None`` if it does not own the channel.

        A read that :meth:`_give_channel` cancelled returns ``None`` so the reader
        re-checks ownership. asyncssh blocks only before it consumes data, so the
        cancelled read drops no byte (ADR 0173). A read whose own task is being
        cancelled re-raises.
        """
        if self._owner is not owner:
            return None
        if self._state == "lost":
            raise ConsoleHoldLostError(
                f"another client's rmvterm ended this session's hold on the console of "
                f"{self._lpar!r} on {self._system!r}; close() issued no rmvterm"
            )
        read = asyncio.ensure_future(self._stdout.read(_CHUNK))
        self._inflight = read
        try:
            chunk = await read
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return None
        finally:
            if self._inflight is read:
                self._inflight = None
        self._watch_for_lost_hold(chunk)
        return chunk

    def _watch_for_lost_hold(self, chunk: bytes) -> None:
        """Latch ``lost`` when the HMC reports another client's ``rmvterm`` (#1004)."""
        window = self._tail + chunk
        if self._state in _RELEASABLE and LOST_HOLD_SENTINEL in window:
            self._state = "lost"
        self._tail = window[-(len(LOST_HOLD_SENTINEL) - 1) :]

    async def _scan_unread(self) -> None:
        """Scan bytes received but never read, so an unread loss still skips ``rmvterm``."""
        if self._inflight is not None:
            inflight = self._inflight
            inflight.cancel()
            await asyncio.wait({inflight})  # asyncssh allows one reader per stream
        # A failed scan must never cost a held session its release (ADR 0170).
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_UNREAD_SCAN_SECONDS):
                while self._state in _RELEASABLE:
                    chunk = await asyncio.wait_for(
                        self._stdout.read(_CHUNK), _UNREAD_READ_SECONDS
                    )
                    if not chunk:
                        self._stream_ended = True
                        return
                    self._watch_for_lost_hold(chunk)

    async def read(self) -> bytes | ConsoleGap:
        """Return the next raw chunk, or ``b""`` once the remote end has closed.

        While a handover or suspension is active, the read waits and returns
        the next chunk once collection resumes, or ``b""`` if :meth:`close`
        starts first. Without ``reconnect``, transport errors propagate
        unwrapped; the session still owes its release. With ``reconnect``, a
        dropped connection (ADR 0174) yields a :class:`ConsoleGap` once the
        vterm is acquired again, and a timed-out read leaves the reconnect
        running for the next read.

        Raises:

            ConsoleHeldAfterDropError: A reconnect found the vterm held; the
                session is then dropped and :meth:`close` issues no ``rmvterm``.
            ConsoleHoldLostError: Another client's ``rmvterm`` ended the hold;
                the read before returned the HMC's report, and :meth:`close`
                issues no ``rmvterm``. The session never reconnects after it.
            HMCCLIError: A reconnect could not connect or acquire.
            RuntimeError: The session is not open, or it was dropped.
        """
        if self._reconnect_task is not None:
            return await self._reconnect_outcome(self._reconnect_task)
        if (
            self._state in ("new", "opening", "unheld", "dropped")
            or self._close_task is not None
        ):
            raise RuntimeError("the console session is not open")
        while True:
            await self._collecting.wait()
            if self._close_task is not None:
                return b""
            if self._pending:
                chunk, self._pending = self._pending, b""
                return chunk
            chunk = await self._read_or_reconnect()
            if chunk is not None:
                return chunk

    def _may_reconnect(self) -> bool:
        return self._reconnect and not self._remote_closed and self._close_task is None

    def _paused(self) -> bool:
        """True when a pause began while the collector's failed read was completing."""
        return self._owner is not self or self._state != "held"

    async def _read_or_reconnect(self) -> bytes | ConsoleGap | None:
        """Read for the collector; with reconnect, a drop becomes a gap (ADR 0174)."""
        try:
            chunk = await self._read_for(self)
        except (asyncssh.Error, OSError) as exc:
            if not self._may_reconnect():
                raise
            if self._paused():
                return None  # the pause's holder or resume() meets the dead connection
            return await self._start_reconnect(_error_detail(exc))
        if chunk == b"" and self._may_reconnect():
            if self._paused():
                return None
            if self._connection.is_closed():
                return await self._start_reconnect("the SSH connection closed")
            self._remote_closed = True
        return chunk

    async def _start_reconnect(self, error: str) -> bytes | ConsoleGap:
        logger.warning(
            "console connection of %s/%s dropped (%s); reconnecting",
            self._system,
            self._lpar,
            error,
        )
        self._state = (
            "dropped"  # _acquire moves it on; a failure before that stays dropped
        )
        self._drop_channel()
        task = self._reconnect_task = asyncio.create_task(
            self._reconnect_after_drop(error)
        )
        task.add_done_callback(_retrieve)
        return await self._reconnect_outcome(task)

    async def _reconnect_outcome(
        self, task: asyncio.Task[ConsoleGap]
    ) -> bytes | ConsoleGap:
        """Await the reconnect; a consumer's timeout leaves it and its outcome in place."""
        gap: ConsoleGap | None = None
        try:
            gap = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not task.cancelled() or (current is not None and current.cancelling()):
                raise  # the reader's own cancellation: the next read gets the outcome
        except BaseException:
            self._reconnect_task = None
            raise
        self._reconnect_task = None
        return b"" if gap is None or self._close_task is not None else gap

    async def _reconnect_after_drop(self, error: str) -> ConsoleGap:
        """Acquire once more after a drop (ADR 0174 rules 4 and 5)."""
        try:
            cancelled = await self._acquire("dropped", take_over=self._take_over)
        except ConsoleHeldError as exc:
            raise ConsoleHeldAfterDropError(
                f"reconnect of {self._lpar!r} on {self._system!r} after a dropped connection "
                f"found the console held; {self._held_after_drop_advice()} {exc}"
            ) from exc
        except HMCCLIError as exc:
            raise HMCCLIError(
                f"console reconnect of {self._lpar!r} on {self._system!r} after a dropped "
                f"connection failed: {exc}"
            ) from exc
        if cancelled:
            raise asyncio.CancelledError  # close() arrived; its teardown releases the new hold
        return ConsoleGap(error=error, took_over=self._take_over)

    def _held_after_drop_advice(self) -> str:
        if self._take_over:
            return (
                "rmvterm ran first, so another holder acquired the console after it. "
                "Whether to take it again is the caller's decision."
            )
        return (
            "most likely this session's leftover hold (P3). No rmvterm was issued; "
            "close() this session and open one with take_over=True to reclaim it."
        )

    @contextlib.asynccontextmanager
    async def hand_over(self) -> AsyncGenerator[ConsoleHandover]:
        """Mode (a): move the channel to an in-process holder; the vterm stays held.

        No ``rmvterm`` or ``mkvterm`` runs on entry or exit. :meth:`read` waits
        inside the block, and a pending collector read is preempted without
        losing bytes. Leaving the block returns the channel to the collector.
        Cancelling the block's task leaves the release to :meth:`close`.

        Raises:

            RuntimeError: The session is not held, or a handover or suspension
                is already active.
        """
        async with self._handed_over(ConsoleHandover(self), "hand_over()") as handover:
            yield handover

    @contextlib.asynccontextmanager
    async def _handed_over(
        self, handover: _Handover, name: str
    ) -> AsyncGenerator[_Handover]:
        self._require_collecting(name)
        self._give_channel(handover)
        try:
            yield handover
        finally:
            if self._owner is handover:
                self._give_channel(self)

    async def suspend(self) -> bool:
        """Mode (b): release the vterm for an external holder; return the proof.

        Sends stdin EOF (or ``rmvterm``) and runs the independent probe exactly
        as :meth:`close` does, and closes the connection; like :meth:`close`, it returns
        ``False`` with no ``rmvterm`` for a hold another client ended (#1004).
        That loss is reported only by ``False`` and a logged warning; a later
        :meth:`resume` meets any new holder as :class:`ConsoleHeldError`.
        Cancelling the caller never interrupts the release; the cancellation is
        re-raised after it completes.
        :meth:`read` waits until :meth:`resume`. The external holder should
        acquire only after this returns: the probe holds the slot briefly, and
        a holder that acquires before the probe makes this return ``False``
        although the slot was handed over.

        Raises:

            RuntimeError: The session is not held, or a handover or suspension
                is already active.
        """
        self._require_collecting("suspend()")
        self._state = "suspending"
        self._settled.clear()
        self._give_channel(None)
        try:
            return await _await_uninterrupted(asyncio.create_task(self._release_hold()))
        finally:
            self._state = "suspended"
            self._settled.set()

    async def resume(self) -> None:
        """Mode (b): acquire the vterm again after :meth:`suspend`.

        Never issues ``rmvterm``, even with ``take_over=True``: that would end
        the holder the session made way for. Cancellation after acquisition
        releases the new hold before ``CancelledError`` propagates.

        Raises:

            ConsoleHeldError: The slot was taken meanwhile; the session stays
                suspended and :meth:`resume` may be retried.
            HMCCLIError: ``mkvterm`` could not start or never confirmed acquisition.
            RuntimeError: The session is not suspended, or :meth:`close` began
                while this call ran (the new hold is then released).
        """
        if self._state != "suspended" or self._close_task is not None:
            raise RuntimeError("resume() needs a suspended console session")
        self._settled.clear()
        try:
            cancelled = await self._acquire("suspended")
        finally:
            self._settled.set()
        if not cancelled and self._close_task is not None:
            await self.close()  # the teardown releases the hold this call acquired
            raise RuntimeError("the console session was closed during resume()")
        await self._finish_acquire(cancelled)

    def _require_collecting(self, name: str) -> None:
        if (
            self._state != "held"
            or self._close_task is not None
            or self._owner is not self
            or self._reconnect_task is not None  # the consumer reads the gap first
        ):
            raise RuntimeError(
                f"{name} needs an open console session with no pause active"
            )

    async def close(self) -> bool:
        """Release the vterm once and report whether the release was proven.

        Later and concurrent calls await the same release. Cancelling the
        caller never interrupts the release; the cancellation is re-raised
        after it completes. The release sends stdin EOF and waits, about 10 s on
        the recorded HMC, for ``mkvterm`` to exit; ``rmvterm`` runs instead when
        the stream had ended or does not end in time (#1058). The probe that
        proves the release waits the same way for its own ``mkvterm`` (#1072),
        so a release takes about 22 s there. During
        :meth:`suspend` or :meth:`resume` it waits
        for that call, then releases whatever it left held. A suspended
        session issues no ``rmvterm``, since the slot may now be the external
        holder's, and reports the proof :meth:`suspend` obtained. A reconnect in
        flight is cancelled first, and a hold it already acquired is released;
        a dropped session reports ``False`` with no ``rmvterm``. So does a
        session whose hold another client's ``rmvterm`` ended (#1004), found
        by a read or by scanning bytes received but not yet read.
        """
        if self._state == "opening":
            raise RuntimeError(
                "close() cannot run while open() is in flight; cancel the opening task"
            )
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._teardown())
        return await _await_uninterrupted(self._close_task)

    async def _teardown(self) -> bool:
        self._owner = None
        self._collecting.set()  # a waiting collector returns b""
        self._released = False
        try:
            if self._reconnect_task is not None:
                self._reconnect_task.cancel()
                await asyncio.wait(
                    {self._reconnect_task}
                )  # completes even if never started
            await (
                self._settled.wait()
            )  # a suspend() or resume() in flight finishes first
            if self._state == "held":
                self._released = await self._release_hold()
            elif self._state == "suspended":
                self._released = self._release_proof
            return self._released
        finally:
            self._drop_channel()

    async def _release_hold(self) -> bool:
        """Release with proof (ADR 0170 rule 4), then drop the channel.

        A hold another client's ``rmvterm`` ended gets no ``rmvterm`` (#1004). An
        open stream is released through stdin EOF, which ends only this session's
        own ``mkvterm`` (#1058); ``rmvterm`` runs only when that does not finish.
        """
        self._release_proof = False
        await self._scan_unread()
        eof_released = (
            self._state != "lost"
            and not self._stream_ended
            and await self._release_by_eof()
        )
        if self._state == "lost":
            logger.warning(
                "another client ended the console hold on %s/%s; no rmvterm issued",
                self._system,
                self._lpar,
            )
            self._drop_channel()
            return False
        try:
            if eof_released:
                self._release_proof = await _probe_released(
                    self._config, self._system, self._lpar
                )
            else:
                self._release_proof = await _release_and_verify(
                    self._config, self._system, self._lpar
                )
        finally:
            self._drop_channel()
        return self._release_proof

    async def _release_by_eof(self) -> bool:
        """Send stdin EOF and read until ``mkvterm`` exits; ``True`` once it did (#1058).

        A lost-hold report read meanwhile still latches ``lost``. A failure to send
        EOF or read, the bound, or a closed connection returns ``False``, and the
        caller falls back to ``rmvterm``.
        """
        if self._stdin is None:
            return False
        with contextlib.suppress(Exception):
            self._stdin.release()
            async with asyncio.timeout(_EOF_RELEASE_SECONDS):
                while self._state in _RELEASABLE:
                    chunk = await self._stdout.read(_CHUNK)
                    if not chunk:
                        return not self._connection.is_closed()
                    self._watch_for_lost_hold(chunk)
        return False

    def _drop_channel(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._stdin is not None:
            self._stdin.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        released = await self.close()
        if exc_type is asyncio.CancelledError:
            logger.warning(
                "console session on %s/%s was cancelled; released=%s",
                self._system,
                self._lpar,
                released,
            )

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes | ConsoleGap:
        chunk = await self.read()
        if not chunk:
            raise StopAsyncIteration
        return chunk


#: Ctrl-O, the Linux hvc console's SysRq trigger (``drivers/tty/hvc/hvc_console.c``),
#: observed passing through the HMC vterm on V10R3 M1060 / FW950 (#1149).
SYSRQ_PREFIX = b"\x0f"


class WritableConsoleSession(ConsoleSession):
    """A console session that can write to the partition console (ADR 0176).

    Constructing one is the authorization gate: hmcpctl never builds one on a
    caller's behalf, and the MCP tool, the CLI and :func:`capture_lpar_console`
    use the sealed :class:`ConsoleSession`. The stdin writer stays private, sends EOF
    only at release (#1058), and is replaced on every acquisition (open, resume, reconnect).
    Every write emits a ``console-write`` audit record carrying no written bytes
    before the bytes are queued.

    A write needs a held session whose channel the writer owns: :meth:`write`
    and :meth:`send_sysrq` while the collector owns it, the
    :class:`ConsoleRawChannel` inside :meth:`raw_mode`. Otherwise, including
    while suspended, dropped, reconnecting, or closing, it raises
    ``RuntimeError``. Writes never start or wait for a reconnect; a transport
    error on a write propagates, and the collector's read detects the drop.
    asyncssh queues a write's whole buffer before draining, so a returned write
    proves only that it was queued, and cancelling one does not withdraw it:
    never retry a cancelled write. A writable session does not consult the
    ADR 0011 ownership guard.

    Observed live on HMC V10R3 M1060 with partition firmware FW950 (#1149):
    written ``~.`` did not end the vterm, whether mid-line or after a CR, at
    the SMS menu, at GRUB or in a Linux installer, although the ``mkvterm``
    manual page documents it as the end sequence. The held session needed no
    stdin EOF to stay held for 85-230 s. Written keystrokes reach the
    partition firmware as well as a booted guest: SMS answers them.
    """

    def _new_stdin(self) -> _Stdin:
        return _ConsoleStdin()

    async def write(self, data: bytes) -> None:
        """Send raw *data* while collection keeps running.

        Raises:

            TypeError: *data* is not ``bytes``.
            ValueError: *data* is empty.
            RuntimeError: The session cannot write now (see the class docstring).
        """
        await self._write_for(self, data, mode="shared", input_kind="raw")

    async def send_sysrq(self, key: str, *, prefix: bytes = SYSRQ_PREFIX) -> None:
        """Send ``prefix`` followed by the SysRq *key* as one write.

        The default *prefix* is ``b"\\x0f"`` (Ctrl-O), which Linux's hvc
        console reads as the SysRq trigger (ADR 0176). The HMC documents no
        SysRq sequence; the vterm was observed passing it through on HMC V10R3
        M1060 with partition firmware FW950, where ``h`` drew the guest's
        SysRq help within about a second (#1149). Pass *prefix* for any other
        console.

        Raises:

            TypeError: *prefix* is not ``bytes``.
            ValueError: *key* is not one printable ASCII character, or
                *prefix* is empty.
            RuntimeError: The session cannot write now (see the class docstring).
        """
        if not (isinstance(key, str) and len(key) == 1 and "!" <= key <= "~"):
            raise ValueError(
                f"SysRq key must be one printable ASCII character, got {key!r}"
            )
        if not isinstance(prefix, bytes):
            raise TypeError(f"SysRq prefix must be bytes, got {type(prefix).__name__}")
        if not prefix:
            raise ValueError("SysRq prefix must not be empty")
        await self._write_for(
            self, prefix + key.encode("ascii"), mode="shared", input_kind="sysrq"
        )

    @contextlib.asynccontextmanager
    async def raw_mode(self) -> AsyncGenerator[ConsoleRawChannel]:
        """Exclusive raw mode for a preempting holder: ADR 0173 mode (a) plus writes.

        The vterm stays held and no ``rmvterm`` or ``mkvterm`` runs. The
        collector's :meth:`read` waits, the session's own writes raise, and
        leaving the block returns the channel to the collector. A drop inside
        the block reaches the channel's reader; the session reconnects after it.

        Raises:

            RuntimeError: The session is not held, or a pause is already active.
        """
        async with self._handed_over(ConsoleRawChannel(self), "raw_mode()") as channel:
            yield channel

    async def _write_for(
        self,
        owner: object,
        data: bytes,
        *,
        mode: audit.ConsoleWriteMode,
        input_kind: audit.ConsoleInputKind,
    ) -> None:
        """The one write path: validate, check ownership, audit, then write."""
        if not isinstance(data, bytes):
            raise TypeError(f"console input must be bytes, got {type(data).__name__}")
        if not data:
            raise ValueError("console input must not be empty")
        stdin = self._stdin
        if (
            self._state != "held"
            or self._close_task is not None
            or self._reconnect_task is not None
            or self._owner is not owner
            or not isinstance(stdin, _ConsoleStdin)
        ):
            raise RuntimeError(
                "console write needs a held session whose channel this writer owns"
            )
        audit.record_console_write(
            system=self._system,
            lpar=self._lpar,
            host=self._config.host,
            mode=mode,
            input_kind=input_kind,
            length=len(data),
            agent_id=self._config.agent_id or "hmcpctl",
        )
        await stdin.write(data)


async def capture_lpar_console(
    hmc: HMCClient,
    system_name: str,
    lpar_name: str,
    *,
    duration_seconds: float,
    max_bytes: int,
    idle_timeout_seconds: float,
    on_chunk: Callable[[bytes], None] | None = None,
    stop_on_cancel: bool = False,
) -> ConsoleCapture:
    """Capture up to the given bounds from *lpar_name*'s console.

    Runs ``mkvterm`` over a dedicated SSH process session with stdin sealed
    (no byte can reach the partition console, P7), enforces the three
    client-side bounds (P8), then releases the vterm on every exit path, with
    stdin EOF or, when that does not finish, ``rmvterm`` (#1058), and reports
    honestly whether the release was *proven* (P2/P3/P4). The exception is a
    hold another client's ``rmvterm`` already ended (#1004): no ``rmvterm``,
    ``stop_reason="error"`` naming
    :class:`ConsoleHoldLostError`, and ``released=False``.

    Raises:

        ConsoleHeldError: Another session holds the vterm (P1). Nothing was
            captured and no ``rmvterm`` was issued — releasing would close
            the other holder's session. Also raised, after the capture's own
            proven hold is released, when the contention sentence appears in
            the captured output (ADR 0172).

    Args:

        hmc: HMC client whose configuration supplies the SSH connection.
        system_name: Managed-system CLI name (UUIDs resolve upstream).
        lpar_name: Partition CLI name (UUIDs resolve upstream).
        duration_seconds: Wall-clock cap on the whole capture.
        max_bytes: Cap on collected bytes; the returned data backtracks to a
            boundary that cannot split a multi-byte UTF-8 sequence or an
            incomplete ANSI escape sequence.
        idle_timeout_seconds: Client-side cap on silence (time since the last
            received byte); the HMC never times an idle stream out itself.
        on_chunk: Optional callback called with each raw chunk as it arrives,
            before it is appended to the internal buffer.  The CLI uses this
            to write bytes to its output sink in real time (issue #1674).
        stop_on_cancel: Return an interrupted outcome after cleanup instead of
            propagating cancellation; the CLI opts in for its SIGINT contract.
    """
    _validate_bounds(duration_seconds, max_bytes, idle_timeout_seconds)
    data = b""
    session = ConsoleSession(hmc, system_name, lpar_name)
    try:
        async with session:
            data, stop_reason, error = await _collect_output(
                session,
                duration_seconds,
                max_bytes,
                idle_timeout_seconds,
                on_chunk,
                stop_on_cancel,
            )
    except asyncio.CancelledError:
        if not stop_on_cancel:
            raise
        stop_reason, error = "interrupted", None
    if session._state == "lost" and stop_reason != "error":
        # A bound fired on the loss report, or the release scan found it unread.
        if stop_reason != "interrupted":
            stop_reason = "error"
        error = _error_detail(
            ConsoleHoldLostError("another client's rmvterm ended the capture's hold")
        )
    if HELD_SENTINEL in data and stop_reason != "interrupted":
        raise ConsoleHeldError(
            f"The console of {lpar_name!r} on {system_name!r} printed the HMC "
            f"contention sentence {HELD_SENTINEL.decode()!r} after acquisition; "
            f"a release of the capture's own hold ran "
            f"(released={session.released is True})."
        )
    if HELD_SENTINEL in data and stop_reason == "interrupted" and error is None:
        error = "ConsoleHeldError: contention sentence appeared after acquisition"
    return ConsoleCapture(
        system=system_name,
        lpar=lpar_name,
        data=_truncate(data, max_bytes),
        stop_reason=stop_reason,
        released=session.released is True,
        error=error,
    )
