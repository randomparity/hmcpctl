"""HMC errors and diagnostic redaction shared by REST, CLI grammar and SSH transport."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from xml.sax.saxutils import escape  # nosec B406 - serializes, never parses

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from .xmlutil import localname

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element  # nosec B405 - type only

MAX_ERROR_BODY_BYTES = 4096

#: What a declared secret value reads as in any diagnostic (ADR 0246).
REDACTED: Final = "<redacted>"

# HMC error bodies echo the request's session cookie and X-API-Session token in
# <RequestHeaders> (#1884). HMCError keeps only these received fields, the ones
# every .body code lookup reads; the caps keep the escaped canonical document
# under MAX_ERROR_BODY_BYTES (ADR 0369).
_FIELD_CHARS: Final = (("ReasonCode", 100), ("Message", 500))
# Scrubbing this prefix of a field finds every echo that starts inside its cap,
# because a longer credential value withholds the body.
_SCRUB_WINDOW: Final = 8192
_MAX_VALUE_CHARS: Final = 4096
_MAX_ASSIGNMENTS: Final = 64
_MAX_VALUES: Final = 1024
_MAX_MATCHES: Final = 256
# Captured HMC error bodies are about 2 KB; bounding the input bounds the
# sanitizer's work, which HMCError does synchronously.
_MAX_SANITIZED_CHARS: Final = 65536
# Maximal runs of token characters, so a bracketed or space-separated header
# value still yields the bare credential.
_TOKEN_RUN: Final = re.compile(r"[A-Za-z0-9._~+/-]{8,}")
# A shorter value (a sibling cookie such as lb=0) is redacted only where it is
# not glued to a letter or digit, so it cannot cut a code like REST0269.
_ANYWHERE_CHARS: Final = 8
_LABEL: Final = re.compile(r"cookie|x-?api-?session", re.IGNORECASE)
_ASSIGNMENT: Final = re.compile(
    r"(?:cookie|x-?api-?session)[\"']?\s*[=:]\s*", re.IGNORECASE
)
_VALUE_END: Final = re.compile(r"[\r\n}]|,\s*[\w-]+\s*=")
# Reported by fixed literal, never by received text: client_systems.py's
# management-console guards match this live-observed HMC phrase (ADR 0138).
_RECOGNIZED_PLAINTEXT: Final = ("Nested path contains null property",)


class _Unsanitizable(Exception):
    """A credential source exceeds the bounds its removal is proven for."""


def _recognized(body: str) -> list[str]:
    return [marker for marker in _RECOGNIZED_PLAINTEXT if marker in body]


def _canonical(fields: dict[str, str], body: str) -> str:
    if recognized := _recognized(body):
        fields = {**fields, "Recognized": "; ".join(recognized)}
    inner = "".join(f"<{name}>{escape(text)}</{name}>" for name, text in fields.items())
    return f"<HttpErrorResponse>{inner}</HttpErrorResponse>"


def _withheld(body: str, reason: str) -> tuple[str, str]:
    detail = f"{reason} {len(body)}-character response body withheld"
    if recognized := _recognized(body):
        detail += "; recognized: " + "; ".join(recognized)
    return _canonical({"Message": detail}, body), detail


def _values(raw: str) -> set[str]:
    """Every form in which a Cookie or X-API-Session value can be echoed."""
    raw = raw.strip()
    if len(raw) > _MAX_VALUE_CHARS:
        raise _Unsanitizable
    found = {raw}
    for part in re.split(r"[;,]", raw):
        found.add(part.strip())
        found.add(part.partition("=")[2].strip())
    found |= {value.strip("\"'") for value in found}
    found.update(_TOKEN_RUN.findall(raw))
    return {value for value in found if any(char.isalnum() for char in value)}


def _assigned_values(text: str) -> set[str]:
    found: set[str] = set()
    for count, match in enumerate(_ASSIGNMENT.finditer(text)):
        if count >= _MAX_ASSIGNMENTS:
            raise _Unsanitizable
        rest = text[match.end() : match.end() + _MAX_VALUE_CHARS + 1]
        if rest.startswith("[") and "]" in rest:  # a [a=1, b=2] header list
            found |= _values(rest[1 : rest.index("]")])
            continue
        end = _VALUE_END.search(rest)
        found |= _values(rest[: end.start()] if end else rest)
    return found


def _credentials(root: Element) -> set[str]:
    """Values from every identifiable Cookie / X-API-Session source in the body."""
    found = _assigned_values("".join(root.itertext()))
    for parent in root.iter():
        for text in (parent.text or "", parent.tail or "", *parent.attrib.values()):
            found |= _assigned_values(text)
        for key, value in parent.attrib.items():
            if _LABEL.search(localname(key)):
                found |= _values(value)
        if _LABEL.search(localname(str(parent.tag))) or any(
            _LABEL.fullmatch(value.strip()) for value in parent.attrib.values()
        ):
            found |= _values("".join(parent.itertext()))
            for attribute in parent.attrib.values():
                found |= _values(attribute)
        children = list(parent)
        if any(_LABEL.fullmatch((child.text or "").strip()) for child in children):
            for child in children:
                found |= _values("".join(child.itertext()))
        if len(found) > _MAX_VALUES:
            raise _Unsanitizable
    return found


def _spans(text: str, value: str) -> list[tuple[int, int]]:
    """Redactable occurrences of one value; glued short-value finds are skipped.

    Only redactable occurrences count against the budget, so benign digits next
    to a short sibling cookie cannot withhold a body. Skipped finds are bounded
    anyway: at most one per short-value length per window position.
    """
    spans, start = [], text.find(value)
    while start >= 0:
        end = start + len(value)
        glued = len(value) < _ANYWHERE_CHARS and (
            (start > 0 and text[start - 1].isalnum())
            or (end < len(text) and text[end].isalnum())
        )
        if not glued:
            spans.append((start, end))
            if len(spans) > _MAX_MATCHES:
                raise _Unsanitizable
        start = text.find(value, start + 1)
    return spans


def _scrubbed(text: str, credentials: set[str], limit: int) -> str:
    """Redact every located echo in one pass, then cut at a credential label.

    The field is cut at ``limit`` received characters, extended to the end of
    any echo straddling that point, so redaction shrinking earlier echoes can
    never pull later received text into the kept field. Echoes go first: a label
    inside a credential value must not cut the value and leave its prefix behind.
    """
    spans = sorted(span for value in credentials for span in _spans(text, value))
    cut = limit
    for start, end in spans:
        if start < cut < end:
            cut = end
    text, spans = text[:cut], [span for span in spans if span[0] < cut]
    out, cursor = [], 0
    for start, end in spans:
        if start < cursor:  # overlaps the redaction just emitted
            cursor = max(cursor, end)
            continue
        out += [text[cursor:start], REDACTED]
        cursor = end
    text = "".join(out) + text[cursor:]
    if label := _LABEL.search(text):
        text = text[: label.start()] + REDACTED
    return text


def _sanitized(body: str) -> tuple[str, str]:
    """Return the canonical body an HMCError retains and the detail it renders."""
    if len(body) > _MAX_SANITIZED_CHARS:
        return _withheld(body, "oversized")
    try:
        root = DET.fromstring(body)
    except (DET.ParseError, DefusedXmlException):
        return _withheld(body, "unparseable")
    try:
        credentials = _credentials(root)
        fields: dict[str, str] = {}
        for name, limit in _FIELD_CHARS:
            element = next(
                (el for el in root.iter() if localname(str(el.tag)) == name), None
            )
            if element is not None:
                text = "".join(element.itertext()).strip()[:_SCRUB_WINDOW]
                fields[name] = _scrubbed(text, credentials, limit)[:limit].strip()
    except _Unsanitizable:
        return _withheld(body, "unsanitizable")
    shown = fields.get("Message") or "no Message in the response body"
    return _canonical(fields, body), shown


class HMCError(Exception):
    """Error returned by the HMC REST API or an HMC CLI command.

    ``HMCCLIError`` (SSH-transported CLI failures) subclasses this so callers
    can handle both paths with a single ``except HMCError``. A received body is
    never kept or rendered as received; ``body`` holds its sanitized canonical
    form (ADR 0369).
    """

    def __init__(
        self, message: str, status_code: int | None = None, body: str | None = None
    ):
        self.status_code = status_code
        detail = message
        if status_code is not None:
            detail = f"{message} (HTTP {status_code})"
        if body:
            body, shown = _sanitized(body)
            detail = f"{detail}: {shown}"
        self.body = body
        super().__init__(detail)


class HMCCLIError(HMCError):
    """An HMC CLI operation failed or was refused before execution."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
        *,
        remote_refusal_code: str | None = None,
        remote_nonzero_completed: bool = False,
    ) -> None:
        super().__init__(message, status_code, body)
        self.remote_refusal_code = remote_refusal_code
        self.remote_nonzero_completed = remote_nonzero_completed is True


TLS_IDENTITY_FAILURE: Final = "tls-identity-verification-failed"

# Safe post-submission diagnostic: never include untrusted exception details.
TLS_OBSERVATION_ABORTED: Final = (
    "TLS certificate identity verification failed after submission; remaining "
    "observation stopped. Inspect the submitted operation before retrying."
)


class LparUpdateError(HMCError):
    """A validated partition edit failed during or after its single submission."""

    def __init__(
        self,
        message: str,
        submission: str,
        status_code: int | None = None,
        body: str | None = None,
    ):
        self.submission = submission
        super().__init__(message, status_code, body)


class HMCTransportError(HMCError):
    """REST request failed before the HMC returned an HTTP response."""


class HMCTLSFingerprintError(HMCError):
    """The HMC presented a TLS certificate whose SHA-256 digest is not the configured one.

    Deliberately not an :class:`HMCTransportError`: callers that degrade on an
    unreachable REST API (the legacy-port logon retry, the SSH name fallback) must
    abort on a pin mismatch instead (ADR 0224).
    """
