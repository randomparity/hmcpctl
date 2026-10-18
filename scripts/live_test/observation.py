"""Result vocabulary, failure classification and expected outcomes for live tests."""

from __future__ import annotations

import re
import traceback
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

#: Every value an observation's ``cleanup`` disposition may take. This ``not-run``
#: means cleanup did not run; it is unrelated to ADR 0126's deleted ``not-run``
#: observation shape, which ADR 0127 dropped. The result vocabulary itself
#: (``observed``, ``passed``, ``failed``, ``skipped``) is produced by
#: ``_result_for`` and ``record_verified`` in the runner and has no such member.
CLEANUP = frozenset({"not-run", "not-required", "failed", "passed"})

ASSERTION_ID = re.compile(r"\A[a-z][a-z0-9-]{1,62}[a-z0-9]\Z")
SCENARIO_ID = re.compile(r"\Ast\d+-[a-z0-9-]+\Z")
OPERATION_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
VARIANT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")

_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")

#: The ``on <targets>`` segment is optional because three of the four
#: ``src/hmcpctl/authorization/target_scope.py`` templates (``:71``, ``:76``,
#: ``:80``) omit it, and all four are denials.
_DENIAL_RE = re.compile(r" is not permitted (?:on .+ )?by access policy ")


@dataclass(frozen=True)
class CallFailure:
    """One tool call's failure, classified from the exception it raised."""

    exception_type: str
    message: str
    traceback_text: str
    http_status: int | None
    denied: bool


@dataclass(frozen=True)
class Assertion:
    """One named postcondition a live scenario checked, and whether it held."""

    id: str
    holds: bool

    def __post_init__(self) -> None:
        if not ASSERTION_ID.fullmatch(self.id):
            raise ValueError(f"assertion id is not a closed-shape token: {self.id!r}")


def grade_assertions(assertions: Sequence[Assertion], cleanup: str) -> tuple[str, str]:
    """Judge postconditions and cleanup without recording or promoting observations."""
    if not assertions:
        raise ValueError("a verified observation must assert at least one condition")
    if cleanup not in CLEANUP:
        raise ValueError(f"cleanup disposition is not a known value: {cleanup!r}")
    unmet = [item.id for item in assertions if not item.holds]
    if cleanup not in {"passed", "not-required"}:
        unmet.append(f"cleanup {cleanup}")
    return ("FAIL", "unmet: " + ", ".join(unmet)) if unmet else ("PASS", "")


@dataclass(frozen=True)
class ExpectedOutcome:
    """A known HMC limitation a scenario declares in advance, and how to spot it."""

    operation: str
    variant: str
    reason: str
    error_codes: frozenset[str] = field(default_factory=frozenset)
    denial: bool = False
    transient: bool = False

    def __post_init__(self) -> None:
        if not OPERATION_ID.fullmatch(self.operation):
            raise ValueError("expected outcome operation must be a domain.action token")
        if not VARIANT_ID.fullmatch(self.variant):
            raise ValueError("expected outcome variant must be a closed slug")
        if not self.error_codes and not self.denial:
            raise ValueError("an expected outcome must name an error code or a denial")

    def matches(self, failure: CallFailure) -> bool:
        """Report whether ``failure`` is the limitation this outcome declares.

        Matching is case-insensitive, as the substring match this replaced was:
        HMC messages render `Not Acceptable` and `Not Running` in title case,
        which a case-sensitive pattern would miss, silently recording a known
        limitation as a real failure. What changed is *where* and *how* the
        match runs — the message only, whole tokens only — not its case rule.
        """
        return any(
            re.search(rf"\b{re.escape(code)}\b", failure.message, re.IGNORECASE)
            for code in self.error_codes
        ) or (self.denial and failure.denied)


@dataclass(frozen=True)
class KnownGap:
    """A previous confirmation skipped dispatch; it is not a fresh observation."""

    outcome: ExpectedOutcome


def classify_failure(exc: BaseException) -> CallFailure:
    """Classify a raised tool failure from its message, never from its traceback.

    The traceback text is carried for a human reader but is not read here: a
    frame quoting an unrelated status would otherwise reclassify the failure.
    """
    message = f"{type(exc).__name__}: {exc}"
    status = _HTTP_STATUS_RE.search(str(exc))
    return CallFailure(
        exception_type=type(exc).__name__,
        message=message,
        traceback_text="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        http_status=int(status.group(1)) if status else None,
        denied=_DENIAL_RE.search(str(exc)) is not None,
    )


def plain_data(data: Any) -> Any:
    """*data* as plain data: a served typed result arrives as a generated dataclass.

    Nested dataclasses, such as workflow steps, become dicts too (#1369).
    """
    if is_dataclass(data) and not isinstance(data, type):
        return asdict(data)
    return data


def judge_create_result(status: str, data: Any) -> tuple[str, str]:
    """Judge a create or provision workflow result by its steps, not the call status.

    On the mksyscfg path, ``hmc_create_lpar`` returns normally (``status`` "PASS")
    even when its ``apply_profile`` step, or a later PCIe-assignment step, reports
    ``status: "error"`` (#939, PR #986): the partition exists but
    ``workflow_completed`` is False and it carries no current configuration.
    Recording that call's PASS/FAIL row from ``status`` alone hides the real
    outcome and leaves every later REST failure on the misconfigured partition
    unexplained (#997).

    Returns the ``(status, note)`` a caller should pass to ``RunState.record`` —
    "FAIL" with the failed step's reason in place of a masked PASS, or the
    original ``status`` and an empty note when nothing in the steps disagrees.
    This never overrides a non-"PASS" call status, and it does not speak to
    whether the partition was actually created — a caller that tracks that
    separately keeps using the original ``status`` for its own control flow.

    A served typed result is read as the plain data it carries (#1369).
    """
    data = plain_data(data)
    if status != "PASS" or not isinstance(data, dict):
        return status, ""
    steps = data.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("status") == "error":
                name = step.get("step", "step")
                result = step.get("result")
                reason = f"{name} failed: {result}" if result else f"{name} failed"
                return "FAIL", reason
    if data.get("workflow_completed") is False:
        return "FAIL", "workflow_completed is false"
    return status, ""
