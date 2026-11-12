"""HMC CLI response parsing and command field validation.

HMC CLI reference:
    https://www.ibm.com/docs/en/power10/7063-CR1?topic=hmc-commands
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from typing import Any

from ..errors import HMCCLIError

# An empty HMC read exits 0 and prints this sentinel, not an empty string
# (docs/HMC_HINTS.md).
HMC_NO_RESULTS = "No results were found."
# A refusal opens with its HSCL code; run_hmc_command quotes it after ": ".
_HMC_ERROR_CODE = re.compile(r"(?:^|[\r\n]|:\s)(HSCL[A-Z0-9]{4})\b")


def hmc_error_code(error: HMCCLIError) -> str | None:
    """Return the HSCL code that opens the HMC's refusal in *error*, if any.

    The HMC prints a refusal on stdout and leaves stderr empty, and
    :func:`run_hmc_command` quotes whichever stream carries it, so the code is
    read from the error text. Some refusals carry no code.
    """
    match = _HMC_ERROR_CODE.search(str(error))
    return match.group(1) if match else None


def parse_hmc_delimited_rows(
    text: str,
    fields: Sequence[str],
    delimiter: str = ",",
) -> list[dict[str, str]]:
    """Parse strict, header-bearing HMC delimited output into named rows."""
    expected = tuple(fields)
    if not expected or any(not field or field.strip() != field for field in expected):
        raise ValueError(
            "fields must contain non-empty names without surrounding whitespace"
        )
    if len(set(expected)) != len(expected):
        raise ValueError("fields must not contain duplicates")
    if len(delimiter) != 1 or delimiter in {"\r", "\n"}:
        raise ValueError("delimiter must be one non-newline character")

    records = [line for line in text.splitlines() if line.strip()]
    if not records:
        raise ValueError("HMC delimited output is missing its header")
    try:
        parsed = [
            next(iter(csv.reader([line], delimiter=delimiter, strict=True)))
            for line in records
        ]
    except csv.Error as error:
        raise ValueError(f"malformed HMC delimited output: {error}") from error
    if tuple(parsed[0]) != expected:
        raise ValueError("HMC delimited header does not match the requested fields")

    rows: list[dict[str, str]] = []
    for number, values in enumerate(parsed[1:], start=2):
        if len(values) != len(expected):
            raise ValueError(
                f"HMC delimited row {number} has {len(values)} columns; expected {len(expected)}"
            )
        rows.append(dict(zip(expected, values, strict=True)))
    return rows


def parse_hmc_result_rows(
    text: str,
    fields: Sequence[str],
    operation: str,
    *,
    blank_is_empty: bool = False,
) -> list[dict[str, str]]:
    """Parse a header-bearing HMC read, treating the empty-result sentinel as no rows.

    Blank output is a missing header unless *blank_is_empty*.

    Raises:
        HMCCLIError: If the response does not parse as *fields* rows; the
            message names *operation* and the expected fields.
    """
    stripped = text.strip()
    if stripped == HMC_NO_RESULTS or (blank_is_empty and not stripped):
        return []
    try:
        return parse_hmc_delimited_rows(text, fields)
    except ValueError as error:
        raise HMCCLIError(
            f"{operation} response did not match the expected "
            f"{','.join(fields)} fields: {error}"
        ) from error


def _parse_lshwres_output(text: str) -> list[dict[str, Any]]:
    """Parse ``lshwres`` key=value output into a list of dicts.

    Each non-empty line is expected to be a comma-separated sequence of
    ``key=value`` pairs (the default ``lshwres`` output format).  A list value
    prints as one double-quoted pair (``"name=v1,v2"``), so a line is split as
    CSV before each pair is split on ``=``.  Values that are absent (empty
    string) are included as empty strings so callers can distinguish missing
    from absent fields.  The empty-result sentinel is no rows.
    """
    if text.strip() == HMC_NO_RESULTS:
        return []
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row: dict[str, Any] = {}
        last_key: str | None = None
        try:
            pairs = next(iter(csv.reader([line], strict=True)))
        except csv.Error as error:
            raise HMCCLIError(f"malformed HMC name=value record: {error}") from error
        for pair in pairs:
            if "=" in pair:
                key, _, value = pair.partition("=")
                last_key = key.strip()
                row[last_key] = value.strip()
            elif last_key is not None:
                # bare token — comma is part of the previous value (e.g. LPAR name lists)
                row[last_key] = row[last_key] + "," + pair.strip()
            else:
                # bare token with no prior key — store as-is
                row[pair.strip()] = ""
        if row:
            results.append(row)
    return results


# PCI class codes used by lshwres -r io --rsubtype slot.


_COMMAND_STRUCTURAL_CHARACTERS = {
    "/": "slash",
    ",": "comma",
    "=": "equals sign",
    '"': "double quote",
}


def require_nonblank_text(value: str, name: str) -> str:
    """Require text without imposing the stronger HMC-command protocol."""
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def require_command_safe_text(value: str, name: str) -> str:
    """Require text that cannot alter an HMC command's field structure."""
    require_nonblank_text(value, name)
    for character, label in _COMMAND_STRUCTURAL_CHARACTERS.items():
        if character in value:
            raise ValueError(
                f"{name} contains {label}; it would alter HMC command structure"
            )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value
