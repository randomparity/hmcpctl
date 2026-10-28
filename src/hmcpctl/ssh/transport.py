"""SSH transport and session mechanics for HMC CLI execution."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import asyncssh

from ..config import HMCConfig
from ..errors import HMCError


class HMCCLIError(HMCError):
    """An HMC CLI operation failed or was refused before execution."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
        *,
        remote_refusal_code: str | None = None,
        remote_nonzero_completed: bool = False,
    ) -> None:
        super().__init__(message, status_code, body)
        self.remote_refusal_code = remote_refusal_code
        self.remote_nonzero_completed = remote_nonzero_completed is True


#: What a declared secret value reads as in any diagnostic (ADR 0246).
REDACTED: Final = "<redacted>"


def _connect_kwargs(config: HMCConfig) -> dict[str, Any]:
    """Build the ``asyncssh.connect`` keyword arguments for *config*.

    When authenticating with a password (no ``ssh_key_file`` set) we suppress
    all local key attempts and request password-only auth.  HMC appliances
    enforce a low ``MaxAuthTries`` limit; exhausting it with every agent key
    before the password attempt triggers a lockout (docs/HMC_HINTS.md).
    """
    config.validate_credentials(require_password=not config.ssh_key_file)
    connect_kwargs: dict[str, Any] = {
        "host": config.host,
        "username": config.user,
        "known_hosts": (
            str(Path.home() / ".ssh" / "known_hosts")
            if config.ssh_verify_host_key
            else None
        ),
    }
    if not config.ssh_verify_host_key:
        logging.getLogger(__name__).warning(
            "SSH host-key verification disabled for %s (ssh_verify_host_key=false)",
            config.host,
        )
    if config.ssh_key_file:
        connect_kwargs["client_keys"] = [config.ssh_key_file]
        connect_kwargs["password"] = None
    else:
        connect_kwargs["password"] = config.password
        connect_kwargs["client_keys"] = []
        connect_kwargs["preferred_auth"] = "password"
    return connect_kwargs


async def run_hmc_command(
    config: HMCConfig,
    cmd: str,
    *,
    include_stderr: bool = False,
    secrets: Collection[str] = (),
    max_output_bytes: int | None = None,
) -> str:
    """Execute one command, optionally retaining labeled successful stderr.

    *secrets* are values *cmd* carries that no diagnostic may show (ADR 0246):
    the command runs unchanged, and every error and ``asyncssh`` log record the
    call produces reads each of them, raw or ``shlex.quote``d, as :data:`REDACTED`.
    A positive *max_output_bytes* bounds combined stdout/stderr payload, including
    discarded stderr; None preserves the existing collecting path.
    """
    if max_output_bytes is not None and (
        type(max_output_bytes) is not int or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer or None")
    declared = _validated_secrets(secrets)
    if not declared:
        return await _run_hmc_command(
            config, cmd, include_stderr, shown=cmd, max_output_bytes=max_output_bytes
        )
    with _asyncssh_log_redaction(declared):
        try:
            return await _run_hmc_command(
                config,
                cmd,
                include_stderr,
                shown=_redact(cmd, declared),
                max_output_bytes=max_output_bytes,
            )
        except HMCCLIError as error:
            message = _redact(str(error), declared)
            completed = error.remote_nonzero_completed
            code = error.remote_refusal_code
            if code is not None and _redact(code, declared) != code:
                code = None
    # Raised outside the handler so the error carries no __cause__/__context__:
    # the asyncssh exception repeats the command and remote output verbatim.
    raise HMCCLIError(
        message, remote_refusal_code=code, remote_nonzero_completed=completed
    )


async def _run_hmc_command(
    config: HMCConfig,
    cmd: str,
    include_stderr: bool,
    *,
    shown: str,
    max_output_bytes: int | None = None,
) -> str:
    """Run *cmd*, naming it as *shown* in every error this builds."""
    connect_kwargs = _connect_kwargs(config)
    try:
        async with asyncio.timeout(config.ssh_timeout):
            async with asyncssh.connect(**connect_kwargs) as connection:
                if max_output_bytes is None:
                    result = await connection.run(
                        cmd, check=True, timeout=config.ssh_timeout
                    )
                else:
                    result = await _run_bounded_command(
                        connection, cmd, max_output_bytes, shown=shown
                    )
                stdout = _command_stdout(result.stdout)
                stderr = _command_stdout(result.stderr) if include_stderr else ""
                if stderr:
                    return (
                        stdout
                        + ("" if not stdout or stdout.endswith("\n") else "\n")
                        + "[stderr]\n"
                        + stderr
                    )
                return stdout
    except TimeoutError as exc:
        error = _command_timeout_error(config, shown)
        if max_output_bytes is not None:
            error = HMCCLIError(
                f"{error} Command completion is unknown; read back before retrying."
            )
        raise error from exc
    except (OSError, ValueError) as exc:
        raise _command_connection_error(shown, exc) from exc
    except asyncssh.ProcessError as exc:
        raise _process_error(shown, exc) from exc
    except asyncssh.Error as exc:
        raise _asyncssh_error(exc) from exc


async def _run_bounded_command(
    connection: asyncssh.SSHClientConnection,
    cmd: str,
    max_output_bytes: int,
    *,
    shown: str,
) -> asyncssh.SSHCompletedProcess:
    """Retain a finite aggregate payload without disabling native backpressure."""
    process = None
    readers: list[asyncio.Task[bytes]] = []
    received = 0
    aborted = False

    async def read(reader: asyncssh.SSHReader[bytes]) -> bytes:
        nonlocal received, aborted
        chunks = bytearray()
        while chunk := await reader.read(4096):
            if received + len(chunk) > max_output_bytes:
                if not aborted:
                    connection.abort()
                    aborted = True
                raise HMCCLIError(
                    f"SSH command {shown!r} exceeded {max_output_bytes} output bytes; "
                    "completion is unknown. Read back before retrying."
                )
            received += len(chunk)
            chunks.extend(chunk)
        return bytes(chunks)

    try:
        process = await connection.create_process(
            cmd, encoding=None, window=4096, max_pktsize=4096
        )
        readers = [
            asyncio.create_task(read(stream))
            for stream in (process.stdout, process.stderr)
        ]
        stdout_bytes, stderr_bytes = await asyncio.gather(*readers)
        await process.wait_closed()
        try:
            stdout, stderr = stdout_bytes.decode("utf-8"), stderr_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise HMCCLIError(
                f"SSH command {shown!r} returned invalid UTF-8; completion is unknown. "
                "Read back before retrying."
            ) from None
        if process.exit_status is None:
            raise HMCCLIError(
                f"SSH command {shown!r} supplied no exit status; completion is unknown. "
                "Read back before retrying."
            )
        result = asyncssh.SSHCompletedProcess(
            process.env,
            process.command,
            process.subsystem,
            process.exit_status,
            process.exit_signal,
            process.returncode,
            stdout,
            stderr,
        )
        if process.exit_status:
            raise asyncssh.ProcessError(
                result.env,
                result.command,
                result.subsystem,
                result.exit_status,
                result.exit_signal,
                result.returncode,
                stdout,
                stderr,
            )
        return result
    except BaseException:
        # Abort before leaving the connection context: its graceful exit can wait
        # for a peer which is still writing or never acknowledges channel close.
        if not aborted:
            connection.abort()
        if process is not None:
            process.close()
        raise
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def _validated_secrets(secrets: Collection[str]) -> tuple[str, ...]:
    """Return *secrets* as a tuple, refusing a value that cannot be redacted."""
    if isinstance(secrets, str):
        raise TypeError("secrets must be a collection of strings, not one string")
    declared = tuple(secrets)
    if not all(declared):
        raise ValueError(
            "secrets must be non-empty strings; an empty secret would redact "
            "every position of the diagnostic"
        )
    return declared


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace each secret in *text*, raw or as spelled inside a single-quoted word.

    A secret's stripped value is matched too: the error builders ``strip()`` the
    remote detail, which cuts a whitespace-bounded secret echoed at its edge.
    """
    values = {
        value for secret in secrets for value in (secret, secret.strip()) if value
    }
    forms = {
        form for value in values for form in (value, value.replace("'", "'\"'\"'"))
    }
    # Every occurrence, overlapping ones included, merged into spans: replacing
    # forms one at a time would cut an overlapping secret and leave a fragment.
    spans = sorted(
        (match.start(), match.start() + len(form))
        for form in forms
        for match in re.finditer(f"(?={re.escape(form)})", text)
    )
    merged: list[list[int]] = []
    for start, stop in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    parts: list[str] = []
    end = 0
    for start, stop in merged:
        parts += [text[end:start], REDACTED]
        end = stop
    return "".join(parts) + text[end:]


class _RedactingFilter(logging.Filter):
    """Rewrite each record's rendered message with the declared secrets redacted."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            # A record whose arguments do not format must not raise inside asyncssh;
            # its arguments are dropped, since repr() would defeat redaction.
            rendered = str(record.msg)
        record.msg = _redact(rendered, self._secrets)
        record.args = ()
        return True


@contextmanager
def _asyncssh_log_redaction(secrets: tuple[str, ...]) -> Generator[None]:
    """Redact *secrets* from the ``asyncssh`` logger for the block's duration."""
    # asyncssh logs every exec'd command at INFO on this logger (channel.py, 2.24.1).
    logger = logging.getLogger("asyncssh")
    redacting = _RedactingFilter(secrets)
    logger.addFilter(redacting)
    try:
        yield
    finally:
        logger.removeFilter(redacting)


def _command_stdout(stdout: str | bytes | None) -> str:
    """Normalize asyncssh command stdout to text."""
    return stdout.decode() if isinstance(stdout, bytes) else stdout or ""


def _command_timeout_error(config: HMCConfig, cmd: str) -> HMCCLIError:
    """Build the actionable timeout error for one bounded CLI command."""
    return HMCCLIError(
        f"SSH command timed out after {config.ssh_timeout:.0f}s: {cmd!r}. "
        "The HMC CLI may be hung or the HMC may be under load."
    )


def _command_connection_error(cmd: str, error: OSError | ValueError) -> HMCCLIError:
    """Build the actionable connection error for one CLI command."""
    return HMCCLIError(f"SSH command connection failed for {cmd!r}: {error}")


def _process_error(cmd: str, error: asyncssh.ProcessError) -> HMCCLIError:
    """Build a CLI error retaining the remote command's termination detail."""
    detail = error.stderr or error.stdout or str(error)
    code = None
    # Only a completed nonzero remote process can establish a coded refusal.
    # Command echoes and transport diagnostics are not remote response evidence.
    if (
        error.exit_status is not None
        and error.exit_status > 0
        and not error.exit_signal
    ):
        for stream in (error.stderr, error.stdout):
            response = (
                stream.decode(errors="replace")
                if isinstance(stream, bytes)
                else stream or ""
            )
            match = re.search(r"(?:^|[\r\n])(HSCL[A-Z0-9]{4})\b", response)
            if match:
                code = match.group(1)
                break
    return HMCCLIError(
        f"SSH command {cmd!r} failed with {_process_termination(error)}: {detail.strip()}",
        remote_refusal_code=code,
        remote_nonzero_completed=(
            type(error.exit_status) is int
            and error.exit_status > 0
            and not error.exit_signal
        ),
    )


def _process_termination(error: asyncssh.ProcessError) -> str:
    """Describe the result status reported by asyncssh for a failed process."""
    if error.exit_status is not None:
        return f"exit status {error.exit_status}"
    if error.exit_signal:
        return f"signal {error.exit_signal}"
    return f"return code {error.returncode}"


def _asyncssh_error(error: asyncssh.Error) -> HMCCLIError:
    """Build an error from an asyncssh failure with optional captured output."""
    detail = (
        getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
    )
    return HMCCLIError(f"SSH command failed: {detail.strip()}")


#: SSH keepalive on every console connection (ADR 0174): asyncssh closes the
#: connection with ``ConnectionLost`` once 3 requests sent 15 s apart go unanswered.
KEEPALIVE_INTERVAL_SECONDS = 15.0
KEEPALIVE_COUNT_MAX = 3


async def open_hmc_connection(config: HMCConfig) -> asyncssh.SSHClientConnection:
    """Open one long-lived SSH connection hosting a streaming HMC process.

    Unlike :func:`run_hmc_command` this connection stays open when the call
    returns and no command timeout bounds it: its caller streams a remote
    process that never exits on its own (the partition console). The caller
    owns the lifetime and must close the connection. The connect itself stays
    bounded by ``config.ssh_timeout`` so an unreachable HMC fails actionably.
    It sends SSH keepalives, so a dead peer closes it (ADR 0174).
    """
    connect_kwargs = _connect_kwargs(config)
    connect_kwargs["keepalive_interval"] = KEEPALIVE_INTERVAL_SECONDS
    connect_kwargs["keepalive_count_max"] = KEEPALIVE_COUNT_MAX
    try:
        async with asyncio.timeout(config.ssh_timeout):
            return await asyncssh.connect(**connect_kwargs)
    except TimeoutError as exc:
        raise HMCCLIError(
            f"SSH connection to {config.host} timed out after "
            f"{config.ssh_timeout:.0f}s."
        ) from exc
    except (OSError, ValueError) as exc:
        raise HMCCLIError(f"SSH connection to {config.host} failed: {exc}") from exc
    except asyncssh.Error as exc:
        raise HMCCLIError(f"SSH connection failed: {str(exc).strip()}") from exc


async def run_hmc_cli(cmd: str, config: HMCConfig | None = None) -> str:
    """Execute a command using an explicit or environment-derived config."""
    return await run_hmc_command(config if config is not None else HMCConfig(), cmd)
