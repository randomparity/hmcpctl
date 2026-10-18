"""Shared result contract for ordered LPAR workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class WorkflowStep:
    """Stable outcome for one ordered multi-stage workflow operation."""

    step: str
    status: Literal["ok", "error", "skipped", "dry_run"]
    result: Any = None
