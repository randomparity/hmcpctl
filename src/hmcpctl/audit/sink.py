"""Non-blocking transport for audit records and stderr diagnostics.

This module owns the reserved audit logger and all process-global sink state. It
imports no other :mod:`hmcpctl` module, keeping the transport usable by
:mod:`hmcpctl.audit` without creating a dependency cycle.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import re
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

#: The reserved logger. Only this module resolves it, which makes "the message is
#: the record" a property of the logger rather than only of the emitter.
AUDIT_LOGGER_NAME: Final = "hmcpctl.audit"


def emit(level: int, build: Callable[[], dict[str, Any]]) -> None:
    """Render and log one record, or drop it. Never raises.

    Taking the builder keeps both record construction and transport failures from
    changing the operation whose diagnostic is being recorded.
    """
    try:
        message = json.dumps(build(), ensure_ascii=True)
        logging.getLogger(AUDIT_LOGGER_NAME).log(level, message)
    except Exception:  # noqa: BLE001, S110 - totality is the audit contract
        pass


#: How many items the sink will hold for a destination that is not reading. About
#: 0.5 MiB at the measured record sizes, and roughly six times the 64 KiB pipe
#: buffer it stands in for. Since ADR 0051 an item can also be a rendered
#: traceback, which is larger and has no fixed size, so the bound is on items and
#: the byte figure is a typical case rather than a ceiling. Not configurable;
#: #270 owns that gap.
_QUEUE_CAPACITY: Final = 1024

#: The whole of what shutdown waits on a destination nobody is draining. A
#: destination that *is* being read finishes in microseconds.
_DRAIN_TIMEOUT: Final = 2.0

_SENTINEL: Final = object()


class _StderrSink:
    """Every stderr write the served process makes, off the thread that asked for it.

    Three producers share it, and the third is what ADR 0051 added: this module's
    audit records, ``server._warn``'s startup prose, and — once
    ``server.install_third_party_stderr_sinks`` has run — the records of the bound
    third-party loggers (``fastmcp``, ``uvicorn``, ``uvicorn.access``, ``mcp``),
    tracebacks included. One bound, one drop counter, and one writer covering all
    three is the point rather than an accident; a second mechanism on this
    descriptor would be a second failure mode on it.

    A synchronous ``write()`` to a pipe nobody is draining blocks. It raises
    nothing, so no guard fires, and under ADR 0040 that write sits inside
    ``dispatch_scope.authorize`` — ahead of the denial and ahead of the handler — so
    the server stops answering. One bounded queue and one daemon writer move the
    only blocking call in the chain onto a thread whose progress nothing waits on.

    A full queue **drops** the line and counts it, which is the trade
    ADR 0043 takes deliberately: a droppable audit trail that keeps serving, over a
    complete one that stops. Nothing is lost silently — the count is written into
    the stream as a ``records-dropped`` record ahead of the next line that lands.

    ``sys.stderr`` is resolved per write rather than bound at construction, an
    absent stream is skipped, and ``OSError`` and ``ValueError`` are caught: the
    same three guards ADR 0040 gave the handler, for the same three reasons —
    CPython sets ``sys.stderr`` to ``None`` when fd 2 is closed at interpreter
    start, a broken stream raises ``OSError``, and a closed one raises
    ``ValueError``. What changes is that each of them now counts a drop.

    Each item is written with its newline in **one** call. ``logging``'s lock
    serialises audit records against each other but against nothing else on this
    stream — the interpreter's own exit traceback and any handler an operator
    attached elsewhere still write to it directly — so a second call could let
    another writer land between a record and its terminator. Since ADR 0051 that
    one call is also what makes a FastMCP traceback atomic: it is one item, so it
    cannot be interleaved and cannot be half-dropped.
    """

    def __init__(self, capacity: int, drain_timeout: float) -> None:
        # One slot past the capacity, reserved for the stop sentinel and refused to
        # `submit`. Without it, stopping a full queue would have to evict a queued
        # line to make room — losing, at shutdown, records a destination that *is*
        # being read would otherwise have received.
        self._capacity = capacity
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=capacity + 1)
        self._drain_timeout = drain_timeout
        self._state = threading.Condition()
        self._pending = 0
        self._dropped = 0
        self._closed = False
        self._writer: threading.Thread | None = None

    def submit(self, line: str) -> None:
        """Queue one already-terminated line. Never blocks, never raises."""
        with self._state:
            if self._closed:
                self._dropped += 1
                return
            if self._queue.qsize() >= self._capacity:
                self._dropped += 1
                return
            self._start_writer()
            self._queue.put_nowait(line)
            self._pending += 1

    def drain(self, timeout: float) -> bool:
        """Wait until every submitted line has been written or dropped.

        Bounded by *timeout* and by nothing else, which is what makes it safe to
        call from ``logging.shutdown``'s ``flush()`` on a destination that has
        stopped reading. Returns whether the queue emptied.
        """
        with self._state:
            if self._closed:
                return self._pending == 0
            return self._state.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        """Stop the writer, giving queued lines ``drain_timeout`` to land.

        The sentinel goes *behind* whatever is already queued and into a slot
        reserved for it, so nothing is evicted to make room and a destination being
        read drains completely. One that is not cannot be drained by any mechanism,
        so the join times out, the daemon thread is abandoned, and the process exits
        rather than hanging. Idempotent.

        Anything submitted after this is counted and can never be reported: the
        writer is gone, so no later write can carry the marker.

        Calling it again re-joins a writer the first call had to abandon, rather
        than returning while that thread is still parked on the descriptor — an
        abandoned writer wakes up eventually and writes wherever ``sys.stderr``
        points by then.
        """
        with self._state:
            writer, already_closing = self._writer, self._closed
            self._closed = True
            if writer is None:
                return
            if not already_closing:
                self._queue.put_nowait(_SENTINEL)
        writer.join(timeout=self._drain_timeout)
        with self._state:
            if not writer.is_alive():
                self._writer = None

    def _start_writer(self) -> None:
        """Begin draining. Caller holds ``_state``; importing must start nothing."""
        if self._writer is None:
            self._writer = threading.Thread(
                target=self._run, name="hmcpctl-audit-stderr", daemon=True
            )
            self._writer.start()

    def _run(self) -> None:
        """Drain the queue onto stderr until the sentinel arrives."""
        while True:
            item = self._queue.get()
            self._report_drops()
            if item is _SENTINEL:
                return
            landed = self._write(item)
            with self._state:
                if not landed:
                    self._dropped += 1
                self._pending -= 1
                self._state.notify_all()

    def _report_drops(self) -> None:
        """Write the drop marker, if anything is owed, ahead of the next line."""
        with self._state:
            owed, self._dropped = self._dropped, 0
        if not owed:
            return
        if not self._write(_drop_marker(owed)):
            with self._state:
                self._dropped += owed

    def _write(self, line: str) -> bool:
        """Put one line on stderr. Returns whether it landed."""
        stream = sys.stderr
        if stream is None:
            return False
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):
            return False
        except Exception:  # noqa: BLE001 - a dead writer stops the trail for good
            # Anything else the destination raises is still a drop rather than the
            # end of the audit trail: this thread is the only one draining, so
            # letting it die would silently strand every later record.
            return False
        return True


def _drop_marker(count: int) -> str:
    """The record that says lines are missing above this point in the stream."""
    event = "records-dropped"
    return (
        json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "event": event,
                "count": count,
            },
            ensure_ascii=True,
        )
        + "\n"
    )


_SINK: _StderrSink | None = None
_SINK_LOCK: Final = threading.Lock()


def _sink() -> _StderrSink:
    """Return the process sink, creating and registering it on first use."""
    global _SINK
    with _SINK_LOCK:
        if _SINK is None:
            _SINK = _StderrSink(_QUEUE_CAPACITY, _DRAIN_TIMEOUT)
            atexit.register(_SINK.close)
        return _SINK


def write_diagnostic(line: str) -> None:
    """Queue one non-record diagnostic line for stderr. Never blocks, never raises.

    ``server._warn``'s entry point. Its lines are prose rather than records, and
    they share this sink rather than a second one because a second one would be a
    second mechanism with its own failure mode on the same descriptor — the
    condition #269 names first is precisely a start that hangs on such a write.
    """
    _sink().submit(line + "\n")


class _AuditHandler(logging.Handler):
    """Hand one record per line to the sink, which writes it or counts it lost.

    Rendering is the installed ``Formatter``'s when there is one and
    ``record.getMessage()`` when there is not — deliberately *not*
    ``Handler.format``'s fallback to a shared default ``Formatter``, because the
    two callers want opposite things and the fallback would silently give both
    the same one.

    The audit logger installs none. ADR 0040's grammar is that the message *is*
    the record — one physical line of ASCII JSON — and a formatter is the one
    thing that could put something else on that line.

    The ``fastmcp`` logger installs one (ADR 0051), because its records carry
    ``exc_info`` and ``logging.Formatter.format`` is what appends the traceback.
    A handler that dropped it would undo ADR 0046's guarantee that a genuine
    handler bug keeps its traceback.

    The newline is explicit either way: a custom handler inherits no
    ``StreamHandler.terminator``, so the rendering plus its terminator is what
    reaches the stream, in one write, as ADR 0040 requires. A multi-line
    traceback is therefore one queue item and one write — it lands whole or is
    dropped whole, and never half-written.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            rendered = self.format(record) if self.formatter else record.getMessage()
            _sink().submit(rendered + "\n")
        except Exception:  # noqa: BLE001 - the stdlib handler contract
            # Every stdlib handler wraps its body this way, and that is what makes
            # a logging call safe to place anywhere in a program. This module's own
            # records cannot reach here — `_emit` already catches around both the
            # build and the log call — but the operator documentation names
            # `hmcpctl.audit` as an attachment point, so a foreign writer's odd
            # record must not raise back into whatever called it.
            self.handleError(record)

    def flush(self) -> None:
        """Wait, boundedly, for what has been submitted to reach the stream.

        ``logging.shutdown`` calls this on every handler at interpreter exit, and
        the bound is what keeps that from becoming the hang this sink removes.
        """
        _sink().drain(_DRAIN_TIMEOUT)


#: What a rendered line may not carry if this stream is to keep its shape. The C0
#: controls — which carry ESC, and which ``str.splitlines`` treats as breaks even
#: though ``split("\n")`` does not — plus DEL, the Unicode line and paragraph
#: separators, and the bidirectional overrides. The same hazards
#: ``json.dumps(..., ensure_ascii=True)`` neutralises for the record grammar,
#: named directly rather than by escaping every non-ASCII character: a UTF-8 path
#: in a traceback is worth keeping readable, an ESC is not worth keeping at all.
_UNSAFE_IN_LINE: Final = re.compile(
    r"[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def _as_escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


class StreamSafeFormatter(logging.Formatter):
    """Render a foreign record so that no line of it can pass for a record.

    ADR 0051 put a second kind of text on the stream ADR 0040 defined, and that
    stream's grammar is one physical line of ASCII JSON per record. A rendered
    exception carries whatever the exception's ``str()`` carries — under ADR 0042's
    threat model, HMC-returned text that this package does not trust — so a
    ``logging.Formatter`` alone would let a newline followed by ``{"event": …}``
    land at column 0 and parse as an authorization record. Verified as a real
    delta rather than assumed: through the ``RichHandler`` this replaces the same
    text was indented into the message column and wrapped, so column 0 was
    unreachable.

    Two rules, both on **every physical line** of the rendering:

    - a fixed *prefix* the caller supplies, which must not begin a JSON object, so
      a reader splitting the stream into lines never sees a forged record;
    - :data:`_UNSAFE_IN_LINE` replaced by its ``\\uXXXX`` escape, so a terminal
      never sees a raw ESC and no separator this formatter did not write can
      introduce a line.

    The rendering stays one string, and therefore one queue item and one write:
    the prefix bounds what a line can *say*, not how many writes it takes.
    """

    def __init__(self, fmt: str, prefix: str) -> None:
        super().__init__(fmt)
        self._prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return "\n".join(
            self._prefix + _UNSAFE_IN_LINE.sub(_as_escape, line)
            # `or [""]` so an empty rendering is still a marked line rather than a
            # bare newline, which is the one shape the prefix would otherwise miss.
            for line in (rendered.splitlines() or [""])
        )


def sink_handler() -> logging.Handler:
    """A handler putting one line per record onto the shared bounded sink.

    Shared rather than a second sink, for the reason ADR 0043 gave ``_warn``: a
    second mechanism on the same descriptor is a second failure mode on it. The
    caller installs a ``Formatter`` when the records it will carry need more than
    their own message, and leaves it unset when the message is the record.
    """
    return _AuditHandler()


class _CLIAuditFormatter(logging.Formatter):
    """Compact terminal view without changing the emitted record or collector JSON."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if sys.stderr is None or not sys.stderr.isatty():
            return message
        try:
            event = json.loads(message)
        except (ValueError, TypeError):
            return message
        if (
            not isinstance(event, dict)
            or set(event) != {"time", "event", "host", "source"}
            or event["event"] != "tls-verification-disabled"
            or not all(isinstance(value, str) for value in event.values())
        ):
            return message
        fields = " ".join(
            f"{key}={json.dumps(event[key], ensure_ascii=True)}"
            for key in ("host", "source", "time")
        )
        return f"Audit tls-verification-disabled: {fields}"


@contextmanager
def cli_audit_sink() -> Generator[None]:
    """Scope CLI fallback presentation; configured handlers remain authoritative."""
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    if (
        logger.hasHandlers()
        or logging.lastResort is None
        or logging.lastResort is not getattr(logging, "_defaultLastResort", None)
    ):
        yield
        return
    # Keep lastResort's synchronous delivery: a display change must not add drops.
    handler = type(logging.lastResort)()
    handler.setLevel(logging.lastResort.level)
    handler.setFormatter(_CLIAuditFormatter())
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()


def set_audit_level(level: int) -> None:
    """Put the operator's chosen level on the reserved audit logger.

    Called from ``server._serve_application`` ahead of ``install_audit_sink``
    (#270), so the NOTSET-default rule there sees an explicit choice and keeps
    it. Permits are recorded at ``INFO`` and denials at ``WARNING``, so
    ``WARNING`` keeps denials only and ``ERROR`` silences the stream; an
    explicit ``INFO`` is indistinguishable from the default. The level must be
    resolved to its integer before it reaches here -- the CLI validates the
    name, and a Python-API caller has ``logging`` already.
    """
    logging.getLogger(AUDIT_LOGGER_NAME).setLevel(level)


def install_audit_sink() -> None:
    """Attach the stderr sink the serve paths use. Idempotent.

    Called from ``server._serve_application``, so both transports get it and neither
    ``create_mcp`` nor any library or CLI caller mutates global logging state by
    composing an application.

    One rule for the last two clauses — what the operator configured wins, what they
    left unconfigured gets a default. ``propagate`` is the exception and is set
    unconditionally: propagation to an unknown ancestor handler is the stdio hazard
    itself, since one ``StreamHandler(sys.stdout)`` on the root logger would put a
    record into the JSON-RPC stream on every authorized call. That closes the
    in-process route only; a launcher merging fd 2 into fd 1 is outside any choice
    available here, and a handler the operator attaches *here* is theirs to keep off
    stdout — this defers to it without inspecting it.
    """
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    logger.propagate = False
    _sink()
    if not logger.handlers:
        logger.addHandler(sink_handler())
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
