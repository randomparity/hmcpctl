"""Client exceptions shared by the transport core and domain mixins.

The :mod:`hmcpctl.client` package uses these exceptions from its transport
implementation in :mod:`hmcpctl.client.core` and from its domain mixin modules.
"""

from __future__ import annotations

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from .xmlutil import find_text

MAX_ERROR_BODY_BYTES = 4096


class HMCError(Exception):
    """Error returned by the HMC REST API or an HMC CLI command.

    ``HMCCLIError`` (SSH-transported CLI failures) subclasses this so callers
    can handle both paths with a single ``except HMCError``.
    """

    def __init__(
        self, message: str, status_code: int | None = None, body: str | None = None
    ):
        self.status_code = status_code
        detail = message
        if status_code is not None:
            detail = f"{message} (HTTP {status_code})"
        if body:
            # HMC error bodies are XML; pull out the message if possible. Parse the
            # untruncated body -- the response-size cap already bounds it upstream --
            # so a message past MAX_ERROR_BODY_BYTES isn't lost to a truncation cut
            # that leaves the XML malformed. Fall back to raw body text if it is not
            # valid XML.
            try:
                msg = find_text(body, "Message") or body[:500]
            except (DET.ParseError, DefusedXmlException):
                msg = body[:500]
            # Bound the rendered detail independently of the extracted message's own
            # length: an untruncated body can carry a <Message> far longer than the
            # fallback's 500-char slice.
            detail = f"{detail}: {msg[:500]}"
        if body is not None:
            body = (
                body[:MAX_ERROR_BODY_BYTES]
                .encode("utf-8")[:MAX_ERROR_BODY_BYTES]
                .decode("utf-8", errors="ignore")
            )
        self.body = body
        super().__init__(detail)


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
