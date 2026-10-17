"""Bounded, input-free LPAR console capture command (issue #959, ADR 0175)."""

from __future__ import annotations

import contextlib
import math
import os
import shlex
from pathlib import Path
from typing import BinaryIO

import typer

from hmcpctl.operations.lpar.console import capture_lpar_console_by_selector
from hmcpctl.ssh.console import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SECONDS,
    ConsoleCapture,
    ConsoleHoldLostError,
)

from ..output import err_console, fail, terminal_field, usage_error
from ..runtime import with_client

#: ADR 0175: ``released`` false means the vterm may still be held; it outranks 1.
RELEASE_UNPROVEN = 3


def _stdout_is_terminal() -> bool:
    return typer.get_text_stream("stdout").isatty()


def _check_bounds(duration: float, max_bytes: int, idle_timeout: float) -> None:
    if not 0 < duration <= MAX_CAPTURE_SECONDS:
        usage_error(
            f"--duration must be above 0 and at most {MAX_CAPTURE_SECONDS:.0f}, got {duration}"
        )
    if not 0 < max_bytes <= MAX_CAPTURE_BYTES:
        usage_error(
            f"--max-bytes must be above 0 and at most {MAX_CAPTURE_BYTES}, got {max_bytes}"
        )
    if not 0 < idle_timeout < math.inf:
        usage_error(
            f"--idle-timeout must be a finite number above 0, got {idle_timeout}"
        )


def _open_sink(output: Path | None) -> BinaryIO:
    if output is None:
        if _stdout_is_terminal():
            usage_error(
                "stdout is a terminal and console bytes carry escape sequences; "
                "redirect stdout or pass --output FILE"
            )
        return typer.get_binary_stream("stdout")
    try:
        return _create_exclusive(output)
    except FileExistsError:
        usage_error(f"--output {output} already exists; choose a new path")
    except OSError as exc:
        fail(exc)


def _create_exclusive(path: Path) -> BinaryIO:
    """Create *path* owner-only, refusing an existing file, as the snapshot writer does."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _report(capture: ConsoleCapture) -> None:
    line = (
        f"stop reason: {capture.stop_reason}; bytes: {len(capture.data)}; "
        f"released: {str(capture.released).lower()}"
    )
    if capture.error:
        line += f"; error: {capture.error}"
    if capture.error and capture.error.startswith(ConsoleHoldLostError.__name__):
        line += (
            "; the HMC reported that another client ended this hold, so no rmvterm "
            "was issued: leave that client's session alone"
        )
    elif not capture.released and capture.stop_reason == "interrupted":
        line += (
            "; check console ownership before another capture; "
            "do not end another client's session"
        )
    elif not capture.released:
        line += (
            "; the console may still be held: run 'rmvterm -m "
            f"{shlex.quote(capture.system)} -p {shlex.quote(capture.lpar)}' "
            "on the HMC before another capture"
        )
    err_console.print(
        terminal_field(line, err=True), markup=False, highlight=False, soft_wrap=True
    )


def _exit_code(capture: ConsoleCapture, *, write_failed: bool) -> int:
    if not capture.released:
        return RELEASE_UNPROVEN
    if capture.stop_reason == "interrupted":
        return 130
    return 1 if write_failed or capture.stop_reason == "error" else 0


def lpars_capture_console(
    lpar_name_or_uuid: str = typer.Argument(
        ..., metavar="LPAR", help="LPAR name or UUID"
    ),
    system_name_or_uuid: str = typer.Option(
        ...,
        "--system",
        "-s",
        help="Managed system name or UUID; required to select the LPAR console host",
    ),
    duration_seconds: float = typer.Option(
        30.0, "--duration", help="Maximum capture duration in seconds (at most 3600)"
    ),
    max_bytes: int = typer.Option(
        65_536, "--max-bytes", help="Maximum bytes to capture (at most 1048576)"
    ),
    idle_timeout_seconds: float = typer.Option(
        10.0, "--idle-timeout", help="Stop after this many seconds without output"
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Write the bytes to this new file; never overwrites"
    ),
) -> None:
    """Capture a bounded snapshot of an LPAR's virtual console (mkvterm).

    Writes the raw console bytes to stdout, or to --output, and never sends input to
    the partition. One stderr line reports the stop reason, byte count and whether
    the console release was proven. Press Ctrl-C (SIGINT) to stop early, then
    wait for release cleanup. The flushed partial --output file is retained.

    Exit codes (ADR 0175): 0 the capture finished and the console was released;
    1 lookup, SSH or HMC failure, the console held by another session, a capture
    stopped by an error, or bytes that could not be written; 2 a usage error,
    including an out-of-range bound, an existing --output file, or a terminal
    stdout without --output; 3 the release was not proven, so the console may
    still be held; 130 interrupted by Ctrl-C with release proven. Exit 3 takes
    precedence over 130 and write errors. SIGTERM and SIGHUP are not covered.
    """
    _check_bounds(duration_seconds, max_bytes, idle_timeout_seconds)
    sink = _open_sink(output)
    write_error: list[OSError] = []

    def _stream_chunk(chunk: bytes) -> None:
        """Write *chunk* to the sink as it arrives.

        Write errors are recorded and suppressed so the capture completes and
        the vterm is properly released before the caller handles the failure
        (issue #1674).  Once a write has failed, later chunks are skipped.
        """
        if write_error:
            return
        try:
            sink.write(chunk)
            sink.flush()
        except OSError as exc:
            write_error.append(exc)

    try:
        capture = with_client(
            lambda hmc: capture_lpar_console_by_selector(
                hmc,
                lpar_name_or_uuid,
                system_name_or_uuid,
                duration_seconds=duration_seconds,
                max_bytes=max_bytes,
                idle_timeout_seconds=idle_timeout_seconds,
                on_chunk=_stream_chunk,
                stop_on_cancel=True,
            )
        )
    except KeyboardInterrupt:
        if write_error:
            err_console.print(
                f"Error: could not write console bytes: {terminal_field(write_error[0], err=True)}",
                markup=False,
            )
        if output is not None:
            try:
                sink.close()
            except OSError as exc:
                err_console.print(
                    f"Error: could not close partial console file: {terminal_field(exc, err=True)}",
                    markup=False,
                )
        err_console.print(
            "Capture interrupted; partial output retained; release was not proven. "
            "Check console ownership before another capture.",
            markup=False,
        )
        raise typer.Exit(RELEASE_UNPROVEN) from None
    except BaseException:
        _discard(sink, output)
        raise
    _report(capture)
    if write_error:
        if capture.stop_reason != "interrupted":
            _discard(sink, output)
        elif output is not None:
            with contextlib.suppress(OSError):
                sink.close()
        err_console.print(
            f"Error: could not write console bytes: {terminal_field(write_error[0], err=True)}",
            markup=False,
        )
        raise typer.Exit(_exit_code(capture, write_failed=True)) from write_error[0]
    if output is not None:
        try:
            sink.close()
        except OSError as exc:
            if capture.stop_reason != "interrupted":
                _discard(sink, output)
            err_console.print(
                f"Error: could not write console bytes: {terminal_field(exc, err=True)}",
                markup=False,
            )
            raise typer.Exit(_exit_code(capture, write_failed=True)) from exc
    code = _exit_code(capture, write_failed=False)
    if code:
        raise typer.Exit(code)


def _discard(sink: BinaryIO, output: Path | None) -> None:
    if output is None:
        return
    try:
        with contextlib.suppress(OSError):
            sink.close()
    finally:
        output.unlink(missing_ok=True)


def register_commands(group: typer.Typer) -> None:
    """Register this module's commands on *group*."""
    group.command("capture-console")(lpars_capture_console)
