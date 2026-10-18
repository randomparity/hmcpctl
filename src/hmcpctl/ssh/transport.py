"""SSH transport and session mechanics for HMC CLI execution."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import asyncssh

from ..config import HMCConfig
from ..errors import HMCError


class HMCCLIError(HMCError):
    """An HMC CLI operation failed or was refused before execution."""


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
    config: HMCConfig, cmd: str, *, include_stderr: bool = False
) -> str:
    """Execute one command, optionally retaining labeled successful stderr."""
    connect_kwargs = _connect_kwargs(config)
    try:
        async with asyncio.timeout(config.ssh_timeout):
            async with asyncssh.connect(**connect_kwargs) as connection:
                result = await connection.run(
                    cmd, check=True, timeout=config.ssh_timeout
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
        raise _command_timeout_error(config, cmd) from exc
    except (OSError, ValueError) as exc:
        raise _command_connection_error(cmd, exc) from exc
    except asyncssh.ProcessError as exc:
        raise _process_error(cmd, exc) from exc
    except asyncssh.Error as exc:
        raise _asyncssh_error(exc) from exc


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
    return HMCCLIError(
        f"SSH command {cmd!r} failed with {_process_termination(error)}: {detail.strip()}"
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
