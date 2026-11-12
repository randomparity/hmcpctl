"""Construct HMC clients from configured connection sources."""

from __future__ import annotations

from ..config import build_config
from .core import HMCClient


def client_from_env(profile: str | None = None) -> HMCClient:
    """Create an HMC client from the selected profile or environment defaults."""
    return HMCClient(build_config(profile=profile))


def open_client(connection: str, admitted_host: str) -> HMCClient:
    """Rebuild the recorded connection only while it still selects the admitted host."""
    config = build_config(profile=None if connection == "<default>" else connection)
    if config.host != admitted_host:
        raise ValueError(
            "The recorded connection no longer resolves to the admitted host; "
            "restore the original connection configuration before resuming this operation"
        )
    return HMCClient(config)
