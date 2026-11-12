"""Run the operator-configured build entry for built installer media (ADR 0324).

The command comes from deployment configuration alone; only the path of the
per-call request file varies, and no shell runs. ``run_build_entry`` returns or
raises only once no child was started or the child has been reaped, so its caller
knows the outcome of every error it sees. No message quotes the entry's output.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import tempfile
from collections.abc import Generator

from hmcpctl.config import BUILD_TARGET
from hmcpctl.operations.lpar.provisioning.media import MAX_PRODUCER_RESULT_BYTES

BUILD_TIMEOUT_SECONDS = 3600.0
_CHUNK = 8192
_DRAIN_SECONDS = 5.0


class BuildFailed(Exception):
    """The build entry produced no usable output; names the cause, never the output."""


def child_environment() -> dict[str, str]:
    """This process's environment without the server's ``HMC_*`` settings."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("HMC_")}


@contextlib.contextmanager
def _request_file(request: bytes) -> Generator[str]:
    """A mode-0600 file holding *request*, in a new mode-0700 directory."""
    try:
        directory = tempfile.TemporaryDirectory(prefix="hmcpctl-build-")
    except OSError as exc:
        raise _unwritten(exc) from None
    with directory:
        path = os.path.join(directory.name, "target.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(request)
        except OSError as exc:
            raise _unwritten(exc) from None
        yield path


def _unwritten(exc: OSError) -> BuildFailed:
    return BuildFailed(
        f"the build request file could not be written ({type(exc).__name__})"
    )


async def _collect(process: asyncio.subprocess.Process) -> bytes:
    assert process.stdout is not None
    output = bytearray()
    while chunk := await process.stdout.read(_CHUNK):
        output += chunk
        if len(output) > MAX_PRODUCER_RESULT_BYTES:
            raise BuildFailed(
                f"the build entry printed more than {MAX_PRODUCER_RESULT_BYTES // 1024} "
                "KiB on stdout"
            )
    status = await process.wait()
    if status != 0:
        raise BuildFailed(f"the build entry exited with status {status}")
    return bytes(output)


async def _reap(process: asyncio.subprocess.Process) -> None:
    # The whole session's group, even after the leader exited: a descendant can hold
    # stdout open. A group id is not reused while any member lives.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.stdout is not None:
        # Reaching EOF lets the pipe close. A descendant that left the group can
        # still hold it, so the drain is bounded rather than awaited outright.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.stdout.read(), _DRAIN_SECONDS)
    await process.wait()


async def run_build_entry(
    command: tuple[str, ...],
    request: bytes,
    *,
    timeout: float = BUILD_TIMEOUT_SECONDS,
) -> bytes:
    """Run *command* with ``{target}`` replaced by a file holding *request*.

    Returns the entry's stdout, at most 64 KiB. Raises ``BuildFailed`` when the
    request file cannot be written, the entry cannot start, exits non-zero, runs
    past *timeout* seconds, or prints more than the bound.
    """
    with _request_file(request) as path:
        argv = [command[0], *(path if a == BUILD_TARGET else a for a in command[1:])]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                env=child_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise BuildFailed(
                f"the build entry could not start ({type(exc).__name__})"
            ) from None
        try:
            return await asyncio.wait_for(_collect(process), timeout)
        except TimeoutError:
            raise BuildFailed(f"the build entry ran past {timeout:g} seconds") from None
        finally:
            await _reap(process)
