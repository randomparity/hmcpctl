"""Result-shape helpers shared by live-test scenario families."""

from __future__ import annotations

from collections.abc import Mapping


def entries(data: object) -> list[Mapping[str, object]]:
    """Normalize a tool result to mapping entries, discarding malformed values."""
    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, Mapping):
        raw_entries = data.get("entries", [])
    else:
        return []
    if not isinstance(raw_entries, list):
        return []
    return [entry for entry in raw_entries if isinstance(entry, Mapping)]


def resource(entry: Mapping[str, object]) -> Mapping[str, object]:
    """Return a nested Resource mapping, or retain the outer mapping."""
    nested = entry.get("Resource")
    return nested if isinstance(nested, Mapping) else entry


def field(data: object, name: str) -> object:
    """Read *name* from a normalized mapping result."""
    return data.get(name) if isinstance(data, Mapping) else None
