"""HMC resource command construction and parsing over the SSH transport.

HMC CLI reference:
    https://www.ibm.com/docs/en/power10/7063-CR1?topic=hmc-commands
"""

from __future__ import annotations

import csv
import re
from collections.abc import Collection, Sequence
from typing import Any

from .transport import REDACTED, HMCCLIError

_RECORD_DELIMITERS: dict[str, tuple[str, str]] = {
    ",": ("a comma", "a comma separates one attribute from the next"),
    "=": (
        "an equals sign",
        "an equals sign separates an attribute name from its value",
    ),
    '"': (
        "a double quote",
        "a double quote opens an HMC quoted region that swallows later attributes",
    ),
}
_ATTRIBUTE_NAME = re.compile(r"^[a-z_][a-z0-9_]*[+-]?$")
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


def build_attribute_record(
    pairs: Sequence[tuple[str, object]],
    *,
    quoted: Collection[str] = (),
    surface: str = "-i",
    secret: Collection[str] = (),
    allow_nontrailing_quoted: bool = False,
) -> str:
    """Return the ``-i`` attribute record for *pairs*, or raise.

    *quoted* names list-valued attributes whose HMC-side grammar is a
    comma-separated list (ADR 0061).  A marked value containing a comma is
    rendered as the IBM quoted pair ``"name=v1,v2"``; without a comma it
    renders bare, byte-identical to the unmarked form.  Every other record
    delimiter is refused inside a marked value: only the comma's behaviour
    inside a quoted region is live-verified.

    Only typed vNIC creation opts into *allow_nontrailing_quoted* (ADR 0311);
    all other callers retain the final-pair restriction.

    *pairs* is an ordered sequence of ``(attribute, value)``.  Each value is
    rendered with :func:`str` and checked against the record grammar before the
    record is joined, so no caller-supplied value can introduce or terminate an
    attribute the caller was not given an argument for.

    Callers still wrap the result in :func:`shlex.quote`.  The two mechanisms
    protect different layers and neither substitutes for the other:
    ``shlex.quote`` keeps the record a single word for the *remote shell*; this
    function keeps the record's own ``,``, ``=``, and ``"`` structure meaningful
    for the *HMC's* parser, which runs afterwards on the already-unquoted text.

    The rejection message quotes the offending value back to the caller,
    except for an attribute named in *secret*, whose value reads as
    :data:`~hmcpctl.ssh.transport.REDACTED`.  Name every credential attribute
    (``chhmcusr -i "name=…,passwd=…"``) there (ADR 0246).

    Raises:
        HMCCLIError: If *pairs* is empty, repeats an attribute, names a
            malformed attribute, or carries a value containing a character the
            record's parser treats as structure.  The message names the
            attribute, the character, and its effect.
    """
    if isinstance(secret, str):
        raise TypeError(
            "secret must be a collection of attribute names, not one string"
        )
    if not pairs:
        raise HMCCLIError(
            f"cannot build an HMC CLI {surface} attribute record with no "
            "attributes; at least one attribute is required"
        )
    seen: set[str] = set()
    for attribute, _value in pairs:
        if attribute in seen:
            raise HMCCLIError(
                f"HMC CLI {surface} attribute {attribute!r} appears twice in "
                "one record; the HMC's handling of a repeated attribute is "
                "undefined, so the record is refused rather than sent"
            )
        seen.add(attribute)
    quotable = frozenset(quoted)
    secret_names = frozenset(secret)
    if unknown := sorted(secret_names - seen):
        # A misspelled secret name would leave the real credential unredacted.
        raise ValueError(f"secret names attributes not in the record: {unknown}")
    parts = []
    for index, (attribute, value) in enumerate(pairs):
        text = _validated_value(
            attribute,
            value,
            allow_comma=attribute in quotable,
            surface=surface,
            redact=attribute in secret_names,
        )
        if attribute in quotable and "," in text:
            if index != len(pairs) - 1 and not allow_nontrailing_quoted:
                # The live probes verified the quoted pair only as the final
                # element of the record (ADR 0061); a non-trailing quoted
                # pair is an unprobed form, so it fails closed like every
                # other unprobed grammar variant.
                raise HMCCLIError(
                    f"HMC CLI {surface} attribute {attribute!r} renders as a "
                    'quoted pair ("name=v1,v2"), which the HMC has only been '
                    "shown to accept as the record's final element; it cannot "
                    f"be followed by {pairs[index + 1][0]!r}. Place "
                    f"{attribute!r} last or split the record."
                )
            parts.append(f'"{attribute}={text}"')
        else:
            parts.append(f"{attribute}={text}")
    return ",".join(parts)


def build_filter(pairs: Sequence[tuple[str, object]]) -> str:
    """Return the ``--filter`` expression for *pairs*, or raise.

    The ``--filter`` grammar is the same ``name=value`` comma-joined record
    grammar (ADR 0061): a delimiter inside a value adds or rewrites a filter
    pair, so a mutation would select a partition the caller did not name.
    Values are validated by the same ``_validated_value`` primitive the
    record builder uses; there is no second delimiter table.

    A comma *inside* one value — IBM's multi-value list form — is refused
    until its encoding is probed; every site here selects a single resolved
    object by name.
    """
    if not pairs:
        raise HMCCLIError(
            "cannot build an HMC CLI --filter expression with no pairs; "
            "at least one name=value pair is required"
        )
    seen: set[str] = set()
    for attribute, _value in pairs:
        if attribute in seen:
            raise HMCCLIError(
                f"HMC CLI --filter attribute {attribute!r} appears twice in "
                "one expression; the HMC's handling of a repeated filter "
                "attribute is undefined, so the expression is refused"
            )
        seen.add(attribute)
    return ",".join(
        f"{attribute}={_validated_value(attribute, value, surface='--filter')}"
        for attribute, value in pairs
    )


def _validated_value(
    attribute: str,
    value: object,
    *,
    allow_comma: bool = False,
    surface: str = "-i",
    redact: bool = False,
) -> str:
    """Return *value* as record text, or raise :class:`HMCCLIError`.

    *allow_comma* marks a quotable list attribute (ADR 0061): the comma is
    permitted because the caller renders the pair quoted.  *surface* names
    the command surface in refusal messages so a ``--filter`` or ``-a``
    refusal never blames an ``-i`` record.  *redact* keeps a secret value
    out of the refusal (ADR 0246).
    """
    if not _ATTRIBUTE_NAME.match(attribute):
        raise HMCCLIError(
            f"invalid HMC CLI {surface} attribute name {attribute!r}; expected a "
            "lower-case identifier, optionally with a '+' or '-' list operator"
        )
    text = str(value)
    shown = REDACTED if redact else repr(text)
    for character, (name, reason) in _RECORD_DELIMITERS.items():
        if character in text and not (allow_comma and character == ","):
            raise HMCCLIError(
                f"HMC CLI {surface} attribute {attribute!r} value {shown} contains "
                f"{name} ({character!r}); {reason}, so the value would alter "
                f"the record's structure. Remove {name} from the value."
            )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise HMCCLIError(
            f"HMC CLI {surface} attribute {attribute!r} value {shown} contains a "
            "control character; the record is one line and the same data "
            "format is read one record per line by the -f file form, so a "
            "newline may terminate the record and a NUL may truncate it. "
            "Remove the control character from the value."
        )
    return text


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
