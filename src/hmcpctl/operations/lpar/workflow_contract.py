"""Shared result and resource-ownership contracts for LPAR workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ...jobs import JobOutcome

# Ledger kinds (ADR 0313 Decision 6); persisted values must not change.
PARTITION_LEDGER = "partition"
DISK_LEDGER = "virtual_disk"
MEDIA_LEDGER = "installer_media"


@dataclass(frozen=True)
class WorkflowStep:
    """Stable outcome for one ordered multi-stage workflow operation."""

    step: str
    status: Literal["ok", "error", "skipped", "dry_run"]
    result: Any = None
    job: JobOutcome | None = None
