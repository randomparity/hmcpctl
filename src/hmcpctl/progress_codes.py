"""Informational POWER9 code definitions, available without network access.

These describe a reported checkpoint; they do not establish the current state of
an operating system or partition. See docs/progress-codes.md for coverage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

SOURCE_URL = "https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf"
_VARIABLES = frozenset("XYST")
# Per-position limits in the source explanations, physical PDF pages 72 and 77.
_VARIABLE_BOUNDS = {
    "C104550X": {7: "01"},
    "C116C2XX": {6: "01"},
    "C16453XX": {7: "23"},
}


@dataclass(frozen=True)
class ProgressCodeDefinition:
    """An original factual summary with a locator in the public IBM reference."""

    notation: str
    definition: str
    applicability: str
    pdf_pages: tuple[int, ...]

    def source(self) -> dict[str, object]:
        """Return JSON-compatible provenance, independent of the cached record."""
        return {
            "notation": self.notation,
            "applicability": self.applicability,
            "url": SOURCE_URL,
            "pdf_pages": list(self.pdf_pages),
        }


@cache
def _catalog() -> tuple[
    dict[str, ProgressCodeDefinition],
    tuple[tuple[re.Pattern[str], ProgressCodeDefinition], ...],
]:
    data = json.loads(
        files("hmcpctl").joinpath("_progress_codes.json").read_text(encoding="utf-8")
    )
    exact: dict[str, ProgressCodeDefinition] = {}
    patterns: list[tuple[int, re.Pattern[str], ProgressCodeDefinition]] = []
    aliases: dict[str, ProgressCodeDefinition] = {}
    for entry in data["entries"]:
        record = ProgressCodeDefinition(
            entry["notation"],
            entry["definition"],
            entry["applicability"],
            tuple(entry["pdf_pages"]),
        )
        notation = record.notation.upper()
        exact[notation] = record
        if any(char in _VARIABLES for char in notation):
            bounds = _VARIABLE_BOUNDS.get(notation, {})
            expression = ""
            cardinality = 1
            for position, char in enumerate(notation):
                if char in _VARIABLES:
                    values = bounds.get(position, "0123456789ABCDEF")
                    expression += f"[{values}]"
                    cardinality *= len(values)
                else:
                    expression += re.escape(char)
            patterns.append((cardinality, re.compile(expression), record))
        if notation.startswith("0") and record.applicability in (
            "AIX configuration program indicators",
            "AIX diagnostic load progress indicators",
            "Dump progress indicators (dump status codes)",
        ):
            width = 2 if record.applicability.startswith("Dump") else 3
            aliases.setdefault(notation[-width:], record)
            # A dump's two leftmost positions may be blanks or zeros.
            if width == 2:
                aliases.setdefault("0" + notation[-width:], record)
    for alias, record in aliases.items():
        exact.setdefault(alias, record)
    # Stable sorting preserves document order for equal specificity.
    patterns.sort(key=lambda entry: entry[0])
    return exact, tuple((pattern, record) for _, pattern, record in patterns)


def lookup_progress_code(code: str) -> ProgressCodeDefinition | None:
    """Look up a code, allowing case/outer whitespace and diagnostic suffixes.

    Exact notations and documented short aliases precede variable patterns;
    fewer admitted concrete values win, with document order breaking ties. Unknown
    or malformed values return None. The original input is never rewritten.
    """
    if not isinstance(code, str) or not code.strip():
        return None
    token = code.split(maxsplit=1)[0].upper()
    if not re.fullmatch(r"[0-9A-FXYST-]{2,11}", token):
        return None
    exact, patterns = _catalog()
    if token in exact:
        return exact[token]
    return next(
        (record for pattern, record in patterns if pattern.fullmatch(token)), None
    )
