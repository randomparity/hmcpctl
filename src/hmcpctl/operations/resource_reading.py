"""Primitive reads of nested resource containers."""

from __future__ import annotations

from typing import Any


def resource_container(resource: dict[str, Any], name: str) -> dict[str, Any]:
    value = resource.get(name)
    return value if isinstance(value, dict) else {}
