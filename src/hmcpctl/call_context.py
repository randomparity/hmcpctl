"""The front-end call a partition resolution belongs to (ADR 0193 Decision 2, ADR 0327).

The MCP tool wrapper and each CLI command record which registry tool is running, whether it
is a read, any presented ``hold_id``, and the system-wide partition-write marker (ADR 0339).
The hold hook reads it; nothing else does. A
resolution with no call context is treated as a mutation presenting no ``hold_id``.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# ADR 0193 Decision 3: the handoff tool itself, and console capture, whose leasing is kdive's.
HOLD_EXEMPT_TOOLS = frozenset({"hmc_capture_lpar_console", "hmc_prepare_host_handoff"})


@dataclass(frozen=True)
class CallContext:
    """One front-end call: the registry tool it runs, if any, and its presented ``hold_id``."""

    tool: str | None
    read: bool
    hold_id: str | None
    system_partition_write: bool = False


_CURRENT: ContextVar[CallContext | None] = ContextVar("hmcpctl_call", default=None)


def current_call() -> CallContext | None:
    """Return the call this code runs in, or None outside any front-end call."""
    return _CURRENT.get()


@contextmanager
def calling(context: CallContext) -> Generator[None]:
    """Run the body as *context*, restoring the enclosing call afterwards."""
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)
