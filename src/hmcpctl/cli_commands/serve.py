"""MCP server command and access-policy startup diagnostics."""

from __future__ import annotations

import logging
from typing import Final

import typer

from .output import fail, terminal_field, usage_error
from .runtime import current_options

_AUDIT_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _policy_file() -> tuple[str, bool] | None:
    """Return the policy path and its presence when the path is resolvable."""
    from ..authorization.access_policy import resolve_access_policy_path

    try:
        path = resolve_access_policy_path()
        return str(path), path.exists() or path.is_symlink()
    except (RuntimeError, OSError, ValueError):
        return None


def _policy_guidance() -> str:
    """Build the migration guidance shared by policy startup refusals."""
    resolved = _policy_file()
    where = (
        f" ({terminal_field(resolved[0], err=True)})" if resolved is not None else ""
    )
    return (
        "\n\n"
        "hmcpctl will not serve without an access policy. To keep what an unpolicied "
        "server exposed, generate one and review it:\n"
        "    hmcpctl config init-access-policy\n"
        f"then start the server with --access-policy legacy-equivalent{where}.\n"
        "For a new deployment, prefer the read-only example in the README or the "
        "limited-mutation example in docs/mcp-server.md over the generated "
        "legacy-equivalent policy."
    )


def _audit_level(value: str | None) -> int | None:
    """Resolve a named audit level or raise a CLI usage error."""
    if value is None:
        return None
    name = value.upper()
    if name not in _AUDIT_LEVELS:
        raise typer.BadParameter(
            f"unknown --audit-level {terminal_field(repr(value), err=True)}; use one of "
            + terminal_field(", ".join(_AUDIT_LEVELS), err=True)
        )
    return logging.getLevelNamesMapping()[name]


def serve(
    http: bool = typer.Option(
        False, "--http", help="Serve over streamable HTTP instead of stdio"
    ),
    listen_host: str = typer.Option(
        "127.0.0.1", "--listen-host", help="HTTP listen host (with --http)"
    ),
    port: int = typer.Option(8000, "--port", help="HTTP listen port (with --http)"),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Bind beyond loopback (with --http). UNSAFE: the HTTP server has no "
        "authentication; you must gate it with an authenticated reverse proxy.",
    ),
    enable_arbitrary_command: bool = typer.Option(
        False,
        "--enable-arbitrary-command",
        help="Expose hmc_run_command, which can execute any HMC CLI command.",
    ),
    audit_level: str | None = typer.Option(
        None,
        "--audit-level",
        metavar="LEVEL",
        help="Minimum authorization-audit level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    ),
    access_policy: str | None = typer.Option(
        None,
        "--access-policy",
        metavar="NAME",
        help="REQUIRED. Enforce the named policy from access-policy.toml.",
    ),
) -> None:
    """Run the MCP server over stdio, or unauthenticated HTTP when requested.

    ``--access-policy NAME`` is required. HTTP binds to loopback unless the
    operator explicitly acknowledges the unauthenticated remote-listener risk.
    """
    from .. import server
    from ..authorization.access_policy import AccessPolicyError, load_access_policy

    command_line_options = current_options().command_line_options
    if command_line_options:
        options = ", ".join(
            f"--{name.replace('_', '-')}" for name in sorted(command_line_options)
        )
        raise typer.BadParameter(
            f"serve does not accept HMC connection options ("
            f"{terminal_field(options, err=True)}"
            f"); configure the server with HMC_* environment "
            f"variables or a configured HMC_PROFILE"
        )

    level = _audit_level(audit_level)
    if access_policy is None:
        usage_error("serve requires --access-policy NAME", guidance=_policy_guidance())

    resolved = _policy_file()
    if resolved is not None and not resolved[1]:
        fail(
            FileNotFoundError(f"no access-policy file at {resolved[0]}"),
            guidance=_policy_guidance(),
        )

    try:
        policy = load_access_policy(access_policy, server.TOOL_SECURITY)
    except AccessPolicyError as exc:
        fail(exc)

    if http:
        try:
            server.main_http(
                policy,
                host=listen_host,
                port=port,
                enable_arbitrary_command=enable_arbitrary_command,
                allow_remote=allow_remote,
                audit_level=level,
            )
        except ValueError as exc:
            raise typer.BadParameter(
                f"{terminal_field(exc, err=True)}"
                f" Re-run with --allow-remote if you understand the risk."
            ) from exc
    else:
        server.main_stdio(
            policy,
            enable_arbitrary_command=enable_arbitrary_command,
            audit_level=level,
        )
