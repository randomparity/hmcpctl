"""Connection construction and async execution for CLI commands."""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

import typer
from typer._click.globals import get_current_context

from hmcpctl.client.core import (
    HMCClient,
    HMCVerifySSLDefaultWarning,
    TLSVerificationDisabledWarning,
)

from ..audit.sink import cli_audit_sink
from ..config import HMCConfig, build_config
from .output import fail

_T = TypeVar("_T")


@dataclass(frozen=True)
class GlobalOpts:
    """Immutable connection options stored on one root CLI context."""

    host: str | None = None
    user: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    trust_system_store: bool | None = None
    ca_bundle: str | None = None
    profile: str | None = None
    command_line_options: frozenset[str] = frozenset()


def current_options() -> GlobalOpts:
    """Return the connection options belonging to the active CLI invocation."""
    ctx = get_current_context(silent=True)
    if ctx is None or not isinstance(ctx.find_root().obj, GlobalOpts):
        raise RuntimeError(
            "CLI connection options are unavailable outside an invocation"
        )
    return ctx.find_root().obj


def client() -> HMCClient:
    """Build a REST client from the active invocation's connection options."""
    options = current_options()
    return HMCClient(
        build_config(
            profile=options.profile,
            host=options.host,
            user=options.user,
            password=options.password,
            verify_ssl=options.verify_ssl,
            trust_system_store=options.trust_system_store,
            ca_bundle=options.ca_bundle,
        )
    )


def ssh_config() -> HMCConfig:
    """Build an SSH configuration from the active invocation's options."""
    options = current_options()
    return build_config(
        profile=options.profile,
        host=options.host,
        user=options.user,
        password=options.password,
        verify_ssl=options.verify_ssl,
        trust_system_store=options.trust_system_store,
        ca_bundle=options.ca_bundle,
    )


def run_cli_coroutine(fn: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run a coroutine-returning closure through the CLI error path."""
    try:
        with cli_audit_sink():
            original = warnings.formatwarning

            def format_warning(
                message: Warning | str,
                category: type[Warning],
                filename: str,
                lineno: int,
                line: str | None = None,
            ) -> str:
                if issubclass(category, TLSVerificationDisabledWarning):
                    return (
                        "Warning: TLS verification is disabled; credentials can be "
                        "intercepted. Trust the HMC CA and use --verify-ssl.\n"
                    )
                if issubclass(category, HMCVerifySSLDefaultWarning):
                    return (
                        "Warning: TLS verification defaults to off until 1.0. "
                        "Use --no-verify-ssl to keep it off, or configure trust "
                        "and use --verify-ssl. See docs/configuration.md#tls-trust.\n"
                    )
                return original(message, category, filename, lineno, line)

            # ty rejects replacement even with the stdlib callback signature.
            warnings.formatwarning = format_warning  # ty: ignore[invalid-assignment]
            try:
                return asyncio.run(fn())
            finally:
                warnings.formatwarning = original
    except (typer.Abort, typer.Exit):
        raise
    except Exception as exc:  # noqa: BLE001 - CLI top-level boundary: every failure becomes fail(exc), never a traceback
        fail(exc)


def with_client(fn: Callable[[HMCClient], Awaitable[_T]]) -> _T:
    """Run one async client call using the active connection options."""

    async def operation() -> _T:
        async with client() as hmc:
            return await fn(hmc)

    return run_cli_coroutine(operation)
