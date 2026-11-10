"""Construct HMC clients from configured connection sources."""

from __future__ import annotations

from ..config import build_config
from .core import HMCClient


def client_from_env(profile: str | None = None) -> HMCClient:
    """Create an HMC client from the selected profile or environment defaults."""
    return HMCClient(build_config(profile=profile))


def open_client(connection: str) -> HMCClient:
    """A client for the operation's recorded connection (``<default>`` is the environment)."""
    return client_from_env(None if connection == "<default>" else connection)
