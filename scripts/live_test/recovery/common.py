"""Recovery findings and failures shared by independent witnesses."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    """One thing a run left behind, and the command that clears it."""

    what: str
    detail: str
    remedy: str


class StateUnreadable(RuntimeError):
    """Raised when a check could not read the state it is there to judge.

    Distinct from "nothing stranded". A check that cannot see the system has
    not found it clean, and the caller turns this into exit 2 rather than the
    exit 0 that a returned empty list would mean.

    It carries the findings already confirmed so an operator still sees what
    was found before the read failed.
    """

    def __init__(self, message: str, findings: list[Finding] | None = None) -> None:
        super().__init__(message)
        self.findings: list[Finding] = findings or []


#: The HMC refuses `rmsyscfg` on a partition outside this state; a survivor
#: found in any other state needs a shutdown first (#950).
NOT_ACTIVATED = "Not Activated"


def recorded_calls(
    document: dict[str, Any], subtasks: AbstractSet[int], tools: AbstractSet[str]
) -> list[dict]:
    """Rows in *subtasks* recording a call to one of *tools*.

    A row's label opens with the tool it called. A `SKIP` row is a call never
    made, or one answered with a declared expected outcome, which by declaration
    changed nothing.
    """
    rows = document.get("results")
    return [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and row.get("subtask") in subtasks
        and row.get("status") != "SKIP"
        and str(row.get("tool", "")).split(" ", 1)[0] in tools
    ]
