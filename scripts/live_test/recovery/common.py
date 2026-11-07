"""Recovery findings and failures shared by independent witnesses."""

from __future__ import annotations

from dataclasses import dataclass


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
