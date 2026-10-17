"""Async IBM HMC REST API client.

Owns the Logon/Logoff session lifecycle, the HTTP transport and the generic
uom helpers (list_uom / get_uom / search_uom / child resources / jobs / raw
escape hatch). Domain operations live in the per-domain mixin modules
(``client_users``, ``client_storage``, ``client_pcm``, ...) and are composed
into :class:`HMCClient` by inheritance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import warnings
from collections.abc import Mapping
from threading import Lock
from typing import Any, Literal, Self, cast, get_args
from urllib.parse import quote, unquote, urlparse

import httpx
import truststore

from ..audit import records as audit
from ..config import HMCConfig, env_var_value
from ..documents.logon import build_logon_request_document
from ..errors import HMCError, HMCTransportError
from ..jobs import TERMINAL_JOB_STATUSES, canonical_job_path
from .client_adapters import AdaptersMixin
from .client_cluster import ClusterMixin
from .client_contracts import (
    _reject_non_uuid_path_argument,
    _reject_over_long_path_value,
    _reject_unknown_uom_type,
)
from .client_lpars import LparsMixin
from .client_lpm import LpmMixin
from .client_network import NetworkMixin
from .client_parse import _find_all_text, _find_text, _parse_feed
from .client_pcm import PcmMixin
from .client_storage import StorageMixin
from .client_systems import SystemsMixin
from .client_templates import TemplatesMixin
from .client_updates import UpdatesMixin
from .client_users import UsersMixin

_logger = logging.getLogger(__name__)

# Media-type fragments used by the HMC API.
MEDIA_WEB = "application/vnd.ibm.powervm.web+xml"
MEDIA_UOM = "application/vnd.ibm.powervm.uom+xml"

# Bounds on what a rejection message renders. The set it renders is whatever
# the discovery read returned. The parse is captured at V1_17_0 and V1_20_0
# (ADR 0142), so this is no longer the inference it was written against -- but
# the caps are kept on the ground that survived: no other level has been
# measured, and a level answering the query-less /search anchor with an
# instance feed would fill the set with per-instance data bounded only by
# HMC_MAX_RESPONSE_BYTES. It is the entry size that is unbounded, not the
# names' plausibility.
#
# Two bounds, because the count alone is not one. Capping the count leaves each
# name unbounded, and a single element carrying a whole 32 MiB body renders in
# full without the count cap ever engaging -- one name is not twenty-one. So a
# name is also truncated: past this many characters it is already evidence the
# parse is wrong, and no legitimate property name is lost.
#
# What the pair buys is a bounded message, not a private one. If the parse is
# wrong, up to _MAX_REPORTED_NAMES truncated operator instance names still
# reach the message. That is less disclosure, not none.
_MAX_REPORTED_NAMES = 20
_MAX_REPORTED_NAME_LENGTH = 64


def _summarize_names(names: frozenset[str]) -> str:
    """Render *names* for an error message, bounded in count and in length.

    *names* is expected non-empty: an empty set renders a bare ".". Each caller
    (``search_uom``, ``get_quick_property``) renders its own message for an
    empty positive set -- the type defining nothing (ADR 0144) -- before
    reaching here, so this is a stated precondition rather than a branch.
    """
    ordered = sorted(names)
    shown = ", ".join(
        name
        if len(name) <= _MAX_REPORTED_NAME_LENGTH
        else f"{name[:_MAX_REPORTED_NAME_LENGTH]}..."
        for name in ordered[:_MAX_REPORTED_NAMES]
    )
    remaining = len(ordered) - _MAX_REPORTED_NAMES
    if remaining > 0:
        return f"{shown}, and {remaining} more."
    return f"{shown}."


# The element whose text holds a search-parameter name at the /search discovery
# anchors, and the container the anchor answers with. CAPTURED at V1_17_0 and
# V1_20_0 (PR #807); both replace the pre-capture inference, which read
# <Nickname> from a <SearchParameter_Collection> and was wrong on both counts.
#
# The container matters to the parse, not just to the record: a 200 carrying it
# with no SearchParameters child is a type that defines no search parameters,
# which is a different answer from a 200 carrying an HttpErrorResponse feed.
# Without the container test the two are indistinguishable and the legitimate
# one has to raise -- and it is not the edge case it looks like. Of the eleven
# types captured, SIX define nothing: ManagementConsole, VirtualSwitch,
# VirtualNetwork, NetworkBridge, LogicalUnit and SharedProcessorPool.
_SEARCH_PARAMETER_NAME_ELEMENT = "ParameterName"
_SEARCH_PARAMETER_CONTAINER_ELEMENT = "SearchParameterSet"
# One of these per parameter the container holds, and the direct evidence that
# the type defines something this parse could not name: an empty ParameterName
# evidences that only on a level still spelling the name element that way
# (ADR 0144). find_all_text matches the exact local name, so this matches
# neither SearchParameters nor SearchParameterSet.
_SEARCH_PARAMETER_ELEMENT = "SearchParameter"

# The container the /quick discovery anchor answers with (ADR 0140). Consulted
# the same way as _SEARCH_PARAMETER_CONTAINER_ELEMENT, and for the same reason:
# a 200 carrying it with no QuickProperty child is a type that defines no quick
# properties, a different answer from a 200 carrying an HttpErrorResponse feed.
# Without the container test the two are indistinguishable and the legitimate
# one has to raise.
_QUICK_PROPERTY_CONTAINER_ELEMENT = "QuickProperty_Collection"
# One of these per property the collection holds; consulted beside the
# container for the same reason as _SEARCH_PARAMETER_ELEMENT (ADR 0144). Exact
# local-name matching keeps it from matching QuickProperty_Collection.
_QUICK_PROPERTY_ELEMENT = "QuickProperty"


async def _close_response(
    response: httpx.Response, primary: BaseException | None
) -> None:
    """Finish owned cleanup even if the caller is cancelled again during close."""
    close_task = asyncio.create_task(response.aclose())
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as exc:
            if primary is None:
                primary = exc
        except Exception:  # noqa: BLE001 — task.result below propagates or records the close failure.
            break
    try:
        close_task.result()
    except BaseException as exc:
        if primary is None:
            raise
        primary.add_note(f"Response cleanup failed: {str(exc)[:500]}")
    if primary is not None:
        raise primary


async def _read_bounded_response(
    response: httpx.Response,
    max_response_bytes: int,
) -> httpx.Response:
    """Buffer only identity bytes, checking size before retaining each chunk."""
    primary = None
    try:
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding and encoding != "identity":
            raise HMCError(
                "Response encoding refused: expected identity", response.status_code
            )
        declared = response.headers.get("Content-Length", "").strip()
        if declared.isascii() and declared.isdecimal():
            normalized = declared.lstrip("0") or "0"
            limit = str(max_response_bytes)
            if (len(normalized), normalized) > (len(limit), limit):
                display = normalized[:64] + ("..." if len(normalized) > 64 else "")
                raise HMCError(
                    f"Response declared size {display} bytes exceeds limit {limit} bytes",
                    response.status_code,
                )
        body = bytearray()
        # Iterate the public stream directly: httpx's byte iterators close at EOF,
        # outside our cancellation-shielded cleanup. Non-identity encodings are refused above.
        async for chunk in cast(httpx.AsyncByteStream, response.stream):
            observed = len(body) + len(chunk)
            if observed > max_response_bytes:
                raise HMCError(
                    f"Response observed size {observed} bytes exceeds limit "
                    f"{max_response_bytes} bytes",
                    response.status_code,
                )
            body.extend(chunk)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        await _close_response(response, primary)


# The two RFC 3986 dot-segments. Held as a frozenset and compared per path
# segment rather than with a substring test, so a resource legitimately named
# "..log" or "a..b" is not refused for containing the characters.
_DOT_SEGMENTS: frozenset[str] = frozenset({".", ".."})


def _reject_dot_segments(method: str, path: str) -> None:
    """Refuse a request path that could resolve away from the resource it names.

    Raised as ``HMCError`` because it is a request this client will not send,
    which is what every other pre-flight refusal here is. The message names the
    method and the offending segment only — never the full path, which on the
    CLI and API paths can carry an operator's own filesystem-derived values.

    **Its scope is path form, and only dot segments — that is a contract, not an
    omission** (ADR 0143). ``?``, ``#`` and every other character are legitimate
    at this waist: ``list_uom`` appends ``?group=`` to the path it passes here
    and ``search_uom`` passes an instance grammar of ``(``, ``)`` and ``==``, so
    a character rule here would refuse this client's own requests. A path
    segment's own character grammar is checked where the segment is built
    instead — see ``_reject_unknown_uom_type`` for a type segment and
    ``_request_with_uuid_path_arguments`` for a UUID.
    """
    candidate = urlparse(path).path if "://" in path else path
    # Raw *and* percent-decoded. httpx resolves only the raw form, so an earlier
    # version of this guard checked only that and reasoned that `%2e%2e` "addresses
    # nothing". That was an assumption about how the HMC's own web stack decodes a
    # path — untestable from here, and the wrong way round for a fail-closed check.
    # A single decode is enough: `%252e` decodes to `%2e`, not to `.`, so nothing
    # this rejects can be reached by decoding again.
    for form in (candidate, unquote(candidate)):
        for segment in form.split("/"):
            if segment in _DOT_SEGMENTS:
                raise HMCError(
                    f"{method.upper()} refused: the request path contains a "
                    f"{segment!r} segment, which would resolve to a different "
                    "resource than the one addressed. Pass an identifier, not a path."
                )


# The job paths the HMC documents and every captured SELF link uses:
# `/rest/api/uom/jobs/{JobID}` (docs/refs/hmc-rest-api-p10/017-jobs.md) and
# `/rest/api/templates/jobs/{JobID}` for template deployment jobs. A read-side
# `jobs/{JobID}/{uuid}` link is reduced to it before this check. The per-operation
# `.../do/{Op}/Job/{id}` form issue #95 accepted was never observed and is refused
# (#1202).
#
# Both raw and decoded forms use this grammar. A decoded query or fragment
# delimiter is unsafe in a prefix too, not just in the identifier (ADR 0152).
_JOB_PATH = re.compile(r"/rest/api/(?:uom|templates)/jobs/[^/?#]+")


def _reject_non_job_path(path: str, argument: str) -> None:
    """Refuse a job path that does not address a job.

    ``get_job_entry`` and ``delete_job`` build one path expression from
    ``job_href`` or ``job_id`` and both pass it here, so the check binds the path
    the request will use rather than the argument that produced it (ADR 0149).
    Without it, an unrelated web-resource href could be fetched through a tool
    classified ``read``/``job``, and ``job_id = "a/b"`` would address
    ``/rest/api/uom/jobs/a/b``. *argument* names which one supplied the path, so
    the refusal does not tell a ``job_id`` caller to pass a SELF link.

    The check binds the *resource class*, not the identifier. Binding the last
    segment to ``job_id`` would be tighter, and was rejected: ``jobs.job_identifier``
    prefers the response's ``JobID``/``UUID`` over the link's last segment, so the
    two can legitimately differ — a handle an earlier release stored pairs an entry
    UUID as ``job_id`` with a ``jobs/{JobID}`` link. Binding the class is what can
    be verified from this checkout.

    The residual is that a caller may read a *different* job. That is the reach
    an access-policy grant for these tools already confers: job identifiers are
    minted by the HMC at runtime and cannot be enumerated in a policy allowlist, so
    ADR 0039 marks both job tools ``exhaustive_targets=False`` and only
    ``targets = "all-targets"`` grants them — a grant that means "any job".
    After this check the tool can reach exactly what that grant says.

    Raw and once-decoded forms must agree on segment boundaries and both name
    a job resource (ADR 0152). Decoding cannot remove a literal slash, so equal
    slash counts rule out newly introduced separators without rewriting the
    path. Non-structural percent-encoded data remains supported.
    """
    decoded = unquote(path)
    if (
        decoded.count("/") != path.count("/")
        or not _JOB_PATH.fullmatch(path)
        or not _JOB_PATH.fullmatch(decoded)
    ):
        raise HMCError(
            f"{argument} refused: it does not address a job resource. Pass the "
            "JobID as job_id, or a SELF link from the job's entry as job_href."
        )


def _job_request_path(job_id: str, job_href: str | None) -> str:
    """Return the one path a job read or delete requests, refused unless it is a job.

    Without *job_href* the path is the documented global one built from *job_id*.
    With it, the link's path is used, except that a read-side SELF link
    (``jobs.canonical_job_path``) is addressed by its JobID segment: the HMC
    refuses that link as a request URL. The refusal runs on the path that will
    be requested (ADR 0149), after that step, so dropping the per-read segment
    cannot let a path through the guard that it would not accept on its own.
    """
    if not job_href:
        path = f"/rest/api/uom/jobs/{job_id}"
        _reject_non_job_path(path, "job_id")
        return path
    path = canonical_job_path(urlparse(job_href).path)
    _reject_non_job_path(path, "job_href")
    return path


def _env_flag(value: str) -> bool | None:
    """Parse a boolean environment value the way pydantic-settings would.

    Returns ``None`` for anything unparseable. Unreachable in practice — a
    value pydantic cannot parse fails ``HMCConfig`` construction long before
    this runs — but kept total so the caller can never raise out of an
    otherwise-successful client construction (#379).
    """
    lowered = value.strip().lower()
    if lowered in {"1", "t", "true", "y", "yes", "on"}:
        return True
    if lowered in {"0", "f", "false", "n", "no", "off"}:
        return False
    return None


VerifySSLSource = Literal[
    "explicit-argument",
    "environment:HMC_VERIFY_SSL",
    "field-default",
]


class TLSVerificationDisabledWarning(UserWarning):
    """Warning emitted when HMC TLS certificate verification is disabled."""


class HMCVerifySSLDefaultWarning(UserWarning):
    """Warning emitted at client construction when ``verify_ssl`` defaults to false.

    The ``false`` default will change to ``true`` in the 1.0 release; this warning
    names the forthcoming change and the migration options available before then.
    """


_reported_tls_warning_keys: set[tuple[str, VerifySSLSource]] = set()
_tls_warning_lock = Lock()

#: Runtime form of the closed audit vocabulary declared by ``VerifySSLSource``.
VERIFY_SSL_SOURCES: frozenset[str] = frozenset(get_args(VerifySSLSource))


def _verify_ssl_source(config: HMCConfig) -> VerifySSLSource:
    """Name the source controlling ``verify_ssl`` using :data:`VerifySSLSource`.

    An unset model field is the default, including for isolated configurations
    created by :meth:`HMCConfig.from_mapping`. Otherwise, a case-insensitive
    environment value is named only when it matches the effective value; a
    mismatch means the explicit argument won source precedence.
    """
    if "verify_ssl" not in config.model_fields_set:
        return "field-default"
    raw = env_var_value("HMC_VERIFY_SSL")
    if raw is None or _env_flag(raw) != config.verify_ssl:
        return "explicit-argument"
    return "environment:HMC_VERIFY_SSL"


class HMCClient(
    UpdatesMixin,
    UsersMixin,
    SystemsMixin,
    LparsMixin,
    AdaptersMixin,
    StorageMixin,
    ClusterMixin,
    LpmMixin,
    PcmMixin,
    NetworkMixin,
    TemplatesMixin,
):
    """Async context-manager client for one HMC session.

    Usage:
        async with HMCClient(config) as hmc:
            systems = await hmc.list_system_summaries()
    """

    def __init__(self, config: HMCConfig) -> None:
        config.validate_credentials()
        self.config = config
        self._session_token: str | None = None
        # Quick-property names per resource type, read once and kept for this
        # client's lifetime; None means a discovery read that yielded no names,
        # cached like any other answer (ADR 0141).
        self._quick_property_names: dict[str, frozenset[str] | None] = {}
        # Serializes the read-through so concurrent validated calls share one
        # discovery request instead of each issuing its own. Constructed here
        # rather than lazily: asyncio.Lock binds to the running loop on first
        # await, not at construction, so a client built outside a loop is fine.
        self._quick_property_names_lock = asyncio.Lock()
        # Search-parameter names per resource type, on the same terms as the
        # quick-property cache above: read once, kept for this client's
        # lifetime, None meaning a discovery read that yielded no names
        # (ADR 0142).
        self._search_parameter_names: dict[str, frozenset[str] | None] = {}
        self._search_parameter_names_lock = asyncio.Lock()
        self._legacy_port_fallback = (
            config.port == 443 and "port" not in config.model_fields_set
        )
        self._verify_ssl_source = _verify_ssl_source(config)
        if not self.config.verify_ssl:
            # #379. Once per construction — not per request, which would flood
            # the sink, and not per process, which would miss a later client
            # built with different settings. The logon-time warnings.warn stays:
            # it is the CLI user's channel; this is the durable record's.
            audit.record_tls_verification_disabled(
                host=self.config.host,
                source=self._verify_ssl_source,
            )
        if not self.config.verify_ssl and self._verify_ssl_source == "field-default":
            # ADR 0224 Decision 3: one-time pre-1.0 startup warning that the
            # false default will flip to true in 1.0. Only operators who have
            # not set HMC_VERIFY_SSL explicitly (field-default) hear it; an
            # explicit false is a deliberate choice and is silent here.
            warnings.warn(
                "HMC_VERIFY_SSL defaults to false, but that default will change "
                "to true in the 1.0 release. To keep verification off for this "
                "HMC, set HMC_VERIFY_SSL=false explicitly (or pass "
                "--no-verify-ssl). To verify instead, trust the HMC's "
                "certificate: set HMC_TRUST_SYSTEM_STORE=true (the default) and "
                "HMC_VERIFY_SSL=true, set HMC_CA_BUNDLE to a PEM file, or set "
                "SSL_CERT_FILE/SSL_CERT_DIR. See docs/configuration.md#tls-trust.",
                HMCVerifySSLDefaultWarning,
                stacklevel=2,
            )
        # X-Audit-Memento is evaluated once at construction time — this is safe
        # because each tool invocation creates a new HMCClient (via asyncio.run(_go)).
        # If the transport ever moves to a persistent shared client, this header would
        # stale when HMC_AGENT_ID changes; re-evaluate effective_audit_memento per-request
        # in that case.
        try:
            self._http = self._new_http_client(config.port)
        except httpx.InvalidURL as exc:
            raise HMCError(
                "HMC client refused: the base URL could not be built."
            ) from exc
        self._rest_base_url = str(self._http.base_url).rstrip("/")

    def _new_http_client(self, port: int) -> httpx.AsyncClient:
        base_url = httpx.URL(self.config.base_url).copy_with(port=port)
        verify: bool | ssl.SSLContext | str = self.config.verify_ssl
        if self.config.verify_ssl and self.config.ca_bundle:
            # ADR 0223: HMC_CA_BUNDLE takes the highest precedence. Pass the
            # path string directly; httpx accepts a PEM path for verify=.
            # SSL_CERT_FILE/SSL_CERT_DIR and trust_system_store are ignored.
            verify = self.config.ca_bundle
        elif (
            self.config.verify_ssl
            and self.config.trust_system_store
            and not os.environ.get("SSL_CERT_FILE")
            and not os.environ.get("SSL_CERT_DIR")
        ):
            # ADR 0222: use the OS trust store via truststore when both
            # verify_ssl and trust_system_store are on and no explicit bundle
            # is set. SSL_CERT_FILE or SSL_CERT_DIR takes precedence, so the
            # operator can always pin a bundle without toggling this flag.
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            verify = ctx
        return httpx.AsyncClient(
            base_url=base_url,
            verify=verify,
            timeout=self.config.timeout,
            headers={
                "X-Audit-Memento": self.config.effective_audit_memento,
                # Most HMC builds ignore charset but honour JSON when asked;
                # we stick to the canonical XML representation everywhere.
            },
        )

    # Session lifecycle

    async def __aenter__(self) -> Self:
        try:
            await self.logon()
        except BaseException as exc:
            try:
                await self._http.aclose()
            except BaseException as cleanup_exc:  # noqa: BLE001 - preserve primary failure
                exc.add_note(f"session cleanup failed: {cleanup_exc}")
            raise
        return self

    async def __aexit__(self, _exc_type, exc, _traceback) -> None:
        """Log off and close the transport without masking the body's error.

        Cleanup runs even when the ``async with`` body raised. When it did,
        the body's exception is primary and wins: cleanup failures are
        attached to it via ``exc.add_note`` and never replace it — replacing
        the in-flight error would hide the failure that actually matters
        behind an incidental one. A failing logoff (an HMC rejection as
        :class:`HMCError`, or a transport failure as
        :class:`HMCTransportError`) is therefore recorded as a note on the
        body's exception rather than raised.

        When the body exited cleanly, a logoff :class:`HMCTransportError` is
        logged as a warning instead of raised: the operation already
        succeeded, and the session may persist until the HMC times it out.
        Every other cleanup error, an HMC rejection of the logoff included,
        propagates (ADR 0028, amendment for #1325).
        """
        cleanup_error: BaseException | None = None
        try:
            await self.logoff()
        except HMCTransportError as logoff_error:
            if exc is not None:
                cleanup_error = logoff_error
            else:
                _logger.warning(
                    "HMC logoff to %s failed before a complete response was read; "
                    "the HMC session may persist until the HMC times it out: %s",
                    self.config.host,
                    logoff_error,
                )
        except BaseException as logoff_error:  # noqa: BLE001 - BaseException is deliberate: the failure is noted on the in-flight exception, and narrowing would swallow CancelledError
            cleanup_error = logoff_error

        try:
            await self._http.aclose()
        except BaseException as close_error:  # noqa: BLE001 - BaseException is deliberate: the failure is noted on the in-flight exception, and narrowing would swallow CancelledError
            if cleanup_error is None:
                cleanup_error = close_error
            else:
                cleanup_error.add_note(
                    f"HTTP client close also failed: {close_error!r}"
                )

        if cleanup_error is None:
            return
        if exc is not None:
            exc.add_note(f"HMC session cleanup also failed: {cleanup_error!r}")
            return
        raise cleanup_error

    @property
    def is_logged_on(self) -> bool:
        return self._session_token is not None

    async def logon(self) -> str:
        """Authenticate and store the X-API-Session token.

        Emits one warning per ``(host, verify_ssl source)`` per process when TLS
        certificate verification is disabled, so the MITM exposure of the
        credentials in flight is never silent.
        """
        if not self.config.verify_ssl:
            warning_key = (self.config.host, self._verify_ssl_source)
            with _tls_warning_lock:
                if warning_key not in _reported_tls_warning_keys:
                    warnings.warn(
                        "TLS certificate verification is disabled (verify_ssl=False). "
                        "HMC credentials travel over an unverified TLS connection and "
                        "can be intercepted by a man-in-the-middle. Install the HMC's "
                        "CA locally and set HMC_VERIFY_SSL=true (or --verify-ssl) to "
                        "enable verification.",
                        TLSVerificationDisabledWarning,
                        stacklevel=2,
                    )
                    _reported_tls_warning_keys.add(warning_key)
        body = build_logon_request_document(
            user=self.config.user, password=self.config.password
        )
        try:
            token = await self._logon_once(body)
        except HMCTransportError:
            if not self._legacy_port_fallback or self._session_token is not None:
                raise
        else:
            self._legacy_port_fallback = False
            return token
        self._legacy_port_fallback = False
        await self._http.aclose()
        self._http = self._new_http_client(12443)
        self._rest_base_url = str(self._http.base_url).rstrip("/")
        return await self._logon_once(body)

    async def _logon_once(self, body: str) -> str:
        resp = await self._request(
            "PUT",
            "/rest/api/web/Logon",
            content=body,
            headers=self._web_headers(
                {
                    "Content-Type": f"{MEDIA_WEB}; type=LogonRequest",
                    "Accept": f"{MEDIA_WEB}; type=LogonResponse",
                }
            ),
        )
        if resp.status_code != 200:
            raise HMCError("HMC logon failed", resp.status_code, resp.text)
        token = _find_text(resp.text, "/rest/api/web/Logon", "X-API-Session")
        if not token:
            raise HMCError("HMC logon response did not contain an X-API-Session token")
        self._session_token = token
        self._http.headers["X-API-Session"] = token
        return token

    async def logoff(self) -> None:
        """Invalidate the session token (DELETE the Logon resource).

        The DELETE is expected to answer 200, 202, or 204; any other status
        raises :class:`HMCError`, so a rejected logoff is never mistaken for
        a closed session (ADR 0028). A transport-level failure surfaces as
        :class:`HMCTransportError` from ``_request`` — distinct from an HMC
        rejection, because they mean different things to a caller.
        ``__aexit__`` logs, rather than raises, that transport failure when
        the ``async with`` body exited cleanly.

        Local state clears either way: the token and the ``X-API-Session``
        header are dropped even when the request fails, so a client that
        believes it is logged off never re-sends the dead token.
        """
        if not self._session_token:
            return
        try:
            resp = await self._request(
                "DELETE",
                "/rest/api/web/Logon",
                headers=self._web_headers({"Accept": MEDIA_WEB}),
            )
            if resp.status_code not in (200, 202, 204):
                raise HMCError("HMC logoff failed", resp.status_code, resp.text)
        finally:
            self._session_token = None
            self._http.headers.pop("X-API-Session", None)

    # Generic request helpers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one REST request and normalize transport failures.

        Rejects raw and percent-encoded RFC 3986 dot-segments before transport.
        This shared guard prevents interpolated resource identifiers from
        retargeting an authorized request when httpx resolves the path against
        ``base_url`` (ADR 0039).

        A path httpx refuses to build a URL from is refused as
        :class:`HMCError`, not :class:`HMCTransportError`: nothing was
        transported, and no other route can build it either (ADR 0148).
        """
        _reject_dot_segments(method, path)
        try:
            headers = httpx.Headers(kwargs.pop("headers", None))
            headers["Accept-Encoding"] = "identity"
            request = self._http.build_request(method, path, headers=headers, **kwargs)
            response = await self._http.send(request, stream=True)
            return await _read_bounded_response(
                response, self.config.max_response_bytes
            )
        except httpx.InvalidURL as exc:
            # Not the path: it holds the character httpx refused and this
            # message reaches logs. httpx's reason renders that character
            # through `!r`, so it comes back escaped (ADR 0148).
            raise HMCError(
                f"{method.upper()} refused: the request URL could not be built. {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            timeout = f"{self.config.timeout:g}"
            raise HMCTransportError(
                f"{method.upper()} {path} timed out after the configured {timeout}s. "
                f"Increase HMC_TIMEOUT above {timeout} for a slower HMC or network."
            ) from exc
        except httpx.TransportError as exc:
            raise HMCTransportError(
                f"{method.upper()} {path} failed before the HMC returned a response: {exc}"
            ) from exc

    async def _request_with_uuid_path_arguments(
        self,
        method: str,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        """Validate UUID-only path arguments before entering the transport."""
        for argument, value in uuid_path_arguments.items():
            _reject_non_uuid_path_argument(argument, value)
        return await self._request(method, path, **kwargs)

    def _uom_headers(
        self,
        resource_type: str | None,
        include_schema_version: bool = True,
    ) -> dict[str, str]:
        accept = MEDIA_UOM
        if resource_type:
            # The Accept destination for the same value the path sites validate.
            # Truthiness, not `is not None`: "" already yields the generic
            # Accept with no type= parameter, so it reaches no destination.
            _reject_unknown_uom_type("resource_type", resource_type)
            accept = f"{MEDIA_UOM}; type={resource_type}"
        headers: dict[str, str] = {"Accept": accept}
        if include_schema_version and self.config.schema_version:
            headers["X-HMC-Schema-Version"] = self.config.schema_version
        return headers

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str:
        resp = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments=uuid_path_arguments or {},
            headers=self._uom_headers(resource_type, include_schema_version),
        )
        if resp.status_code == 204:
            return ""
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        return resp.text

    async def _post(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str:
        return await self._write_uom(
            "POST",
            path,
            body,
            resource_type,
            include_schema_version,
            uuid_path_arguments,
            (200, 201, 202),
        )

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str:
        return await self._write_uom(
            "PUT",
            path,
            body,
            resource_type,
            include_schema_version,
            uuid_path_arguments,
            (200, 201, 202, 204),
        )

    async def _write_uom(
        self,
        method: str,
        path: str,
        body: str | bytes,
        resource_type: str | None,
        include_schema_version: bool,
        uuid_path_arguments: Mapping[str, str] | None,
        success_codes: tuple[int, ...],
    ) -> str:
        """Send a UOM write with an untyped Accept and a typed Content-Type (ADR 0178).

        V10R3 answers a typed or generic uom Accept on writes with 406, and a
        generic Content-Type with 415.
        """
        headers = self._uom_headers(resource_type, include_schema_version)
        headers["Content-Type"] = headers["Accept"]
        headers["Accept"] = "*/*"
        resp = await self._request_with_uuid_path_arguments(
            method,
            path,
            uuid_path_arguments=uuid_path_arguments or {},
            content=body,
            headers=headers,
        )
        if resp.status_code not in success_codes:
            raise HMCError(f"{method} {path} failed", resp.status_code, resp.text)
        return resp.text

    async def _delete(
        self,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> None:
        headers = self._uom_headers(None)
        headers["Accept"] = (
            "*/*"  # V10R3 answers a generic uom Accept with 406 (ADR 0178).
        )
        resp = await self._request_with_uuid_path_arguments(
            "DELETE",
            path,
            uuid_path_arguments=uuid_path_arguments or {},
            headers=headers,
        )
        if resp.status_code not in (200, 202, 204):
            raise HMCError(f"DELETE {path} failed", resp.status_code, resp.text)

    # Web endpoint helpers (/rest/api/web/)
    #
    # The HMC exposes non-UOM resources under /rest/api/web/ with the MEDIA_WEB
    # content type. These helpers mirror
    # _get/_post/_delete but use MEDIA_WEB for Content-Type and Accept.
    #
    # The session token is set on the shared httpx client during logon and
    # therefore applies to documented web resources that use these helpers.

    def _web_headers(self, extra: dict[str, str]) -> dict[str, str]:
        """Build headers for a web-endpoint request.

        Merges *extra* with a conditional X-HMC-Schema-Version header so that
        HMC versions requiring the header on /rest/api/web/ endpoints receive
        it whenever the caller has configured schema_version (issue #99).
        """
        headers = dict(extra)
        if self.config.schema_version:
            headers["X-HMC-Schema-Version"] = self.config.schema_version
        return headers

    async def _web_get(self, path: str) -> str:
        resp = await self._request(
            "GET", path, headers=self._web_headers({"Accept": MEDIA_WEB})
        )
        if resp.status_code == 204:
            return ""
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        return resp.text

    async def _web_post(self, path: str, body: str) -> str:
        resp = await self._request(
            "POST",
            path,
            content=body,
            headers=self._web_headers({"Content-Type": MEDIA_WEB, "Accept": MEDIA_WEB}),
        )
        if resp.status_code not in (200, 201, 202):
            raise HMCError(f"POST {path} failed", resp.status_code, resp.text)
        return resp.text

    async def _web_delete(self, path: str) -> None:
        resp = await self._request(
            "DELETE", path, headers=self._web_headers({"Accept": MEDIA_WEB})
        )
        if resp.status_code not in (200, 202, 204):
            raise HMCError(f"DELETE {path} failed", resp.status_code, resp.text)

    # uom resources

    async def list_uom(
        self, resource_type: str, group: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /rest/api/uom/{ResourceType} and parse the Atom feed.

        A *group* names an extended property group; it is percent-encoded
        before it reaches the query string, so it names one group and cannot
        append a second parameter (ADR 0145).
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        path = f"/rest/api/uom/{resource_type}"
        if group:
            encoded_group = quote(group, safe="")
            path += f"?group={encoded_group}"
        xml = await self._get(path, resource_type)
        if not xml:
            return []
        return _parse_feed(xml, path)

    async def get_uom(
        self, resource_type: str, uuid: str, group: str | None = None
    ) -> dict[str, Any] | None:
        """GET /rest/api/uom/{ResourceType}/{uuid} and parse the entry.

        *group* is percent-encoded before it reaches the query string, on the
        same terms as ``list_uom`` (ADR 0145).
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        path = f"/rest/api/uom/{resource_type}/{uuid}"
        if group:
            encoded_group = quote(group, safe="")
            path += f"?group={encoded_group}"
        xml = await self._get(path, resource_type, uuid_path_arguments={"uuid": uuid})
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        return entries[0] if entries else None

    async def get_quick_property(
        self,
        resource_type: str,
        uuid: str,
        property_name: str,
        *,
        validate: bool = False,
    ) -> str | None:
        """GET a quick property, e.g. LogicalPartition/{uuid}/quick/PartitionState.

        quick/ endpoints return a plain-text value and require Accept: */* —
        a typed uom+xml Accept header causes HTTP 406.

        With *validate* the name is checked against the ones the type defines
        before anything is sent, raising ``ValueError`` on a name the HMC does
        not know. The names come from ``list_quick_properties`` and are cached
        for this client's lifetime, so validating costs at most one extra
        request per resource type per session. It is off by default: the client
        is constructed per tool call, so the cache would rarely be reused and
        every call would pay that request (ADR 0141). A level where the
        discovery read itself fails validates nothing rather than raising.

        A type defining no quick properties is refused locally, every name of
        it: the anchor answering with the container and no property is a fact
        about the type rather than a failed read (ADR 0144). A 204, a failed
        read, and a container holding properties this parse cannot name still
        validate nothing.

        The name is percent-encoded before it is interpolated, not checked
        against a grammar (ADR 0146). The path is its only destination — this
        method sends ``Accept: */*`` and never reaches ``_uom_headers`` — so
        encoding is the rule RFC 3986 defines for it, where the type segment
        beside it also lands in an ``Accept`` media-type parameter and is
        validated instead (ADR 0143). Encoding changes no byte for any name this
        repository passes, and *validate* above remains the namespace check.
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        if validate:
            defined = await self._defined_quick_property_names(resource_type)
            if defined is not None and property_name not in defined:
                detail = (
                    f"Defined names: {_summarize_names(defined)}"
                    if defined
                    else "The type defines none at all."
                )
                raise ValueError(
                    f"{resource_type} defines no quick property named "
                    f"{property_name!r}. {detail}"
                )
        # Before encoding, and as the path the segment occupies rather than as a
        # bare one. Encoding hides a dot segment from the waist guard, and
        # `_reject_dot_segments` parses what it is handed as a path: a value
        # carrying `://` escapes through `urlparse`'s scheme branch when passed
        # bare, and through its netloc branch when passed behind a lone `/`. A
        # non-empty leading segment closes both (ADR 0146).
        _reject_dot_segments("GET", f"/quick/{property_name}")
        encoded_property = quote(property_name, safe="")
        path = f"/rest/api/uom/{resource_type}/{uuid}/quick/{encoded_property}"
        resp = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments={"uuid": uuid},
            headers={"Accept": "*/*"},
        )
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        # The HMC sometimes wraps the value in double-quotes; strip them.
        value = resp.text.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1]
        return value or None

    async def _defined_quick_property_names(
        self, resource_type: str
    ) -> frozenset[str] | None:
        """The quick-property names *resource_type* defines, or None if unknown.

        Reads the root ``/quick`` anchor once per type per client and caches the
        answer, the failure included: a level where discovery does not work
        yields None, which callers read as "do not validate", while a type that
        defines no quick properties yields the empty positive set, which they
        read as "refuses every name" (ADR 0144). A transport failure is cached
        as durably as a firmware-level one, so a transient one leaves validation
        off for this type until a new client is constructed.
        """
        if resource_type in self._quick_property_names:
            return self._quick_property_names[resource_type]
        async with self._quick_property_names_lock:
            # Re-check under the lock: a task that waited here may have been
            # waiting on the very read that populates this entry, and the cost
            # bound is per type per client, not per caller.
            if resource_type in self._quick_property_names:
                return self._quick_property_names[resource_type]
            try:
                names, _ = await self.list_quick_properties(resource_type)
            except HMCError:
                # Covers HMCTransportError too, which subclasses it. Degrading
                # is #799's fourth criterion: ADR 0139 records levels where the
                # sibling /operations anchor answers 500, and ADR 0140 records
                # a type answering 400 at the root /quick anchor.
                names = None
            # None is the level's answer not being a fact about the type: a 204, a
            # failed read, or a container holding properties this parse cannot
            # name. An empty list is a fact about the type, and caching it as an
            # empty positive set is what makes validate=True refuse locally
            # (ADR 0144).
            defined = None if names is None else frozenset(names)
            self._quick_property_names[resource_type] = defined
            return defined

    async def list_quick_properties(
        self,
        resource_type: str,
        *,
        parent_type: str | None = None,
        parent_uuid: str | None = None,
    ) -> tuple[list[str] | None, str | None]:
        """GET the quick-property names a type defines, with the schema version.

        Reads ``/rest/api/uom/{R}/quick``, or ``/rest/api/uom/{P}/{UUID}/{C}/quick``
        when both *parent_type* and *parent_uuid* are given; supplying exactly one
        of them is a caller error.

        There is no ``all_properties`` argument because there is no working
        ``/quick/all``: FW950 answers 400 on both anchors. The capitalized
        ``/quick/All`` this client sends from ``client_systems`` is a different
        endpoint returning per-instance values, not names (ADR 0138, ADR 0140).

        Returns the names paired with the response's ``X-HMC-Schema-Version``,
        with a first element of ``None`` when the level's answer is not a fact
        about the type -- a 204, or a container holding properties this parse
        cannot name -- and a list, empty or not, when it is. The version is
        ``None`` when the HMC sends none; that value is verbatim and is not
        guaranteed to hold a version: FW950 echoes the request's
        ``X-Audit-Memento`` into it, as V1_17_0 does for ``/operations``
        (ADR 0139), so callers must not parse it as a level.

        Not every type offers the root anchor: ``NetworkBridge`` answers 400 there
        and 200 as a child of ``ManagedSystem``. A type that does not serve the
        anchor asked for surfaces as ``HMCError`` carrying that status.

        The body is an Atom ``<entry>`` whose ``<content>`` holds a
        ``QuickProperty_Collection``; each ``QuickProperty`` carries its name in a
        ``Nickname`` child, beside ``RESTElement`` and a prose ``Description``, and
        the ``Nickname`` texts are the result. They are read document-wide rather
        than through ``_parse_feed``, which flattens an entry to a dict and
        collapses a repeated element to a bare value when the HMC sends exactly
        one -- the hazard ADR 0139 recorded for ``OperationSet``, and reachable
        here because ``VirtualNetwork`` defines exactly one quick property
        (ADR 0140). An empty ``Nickname`` is dropped. When no name is found, the
        body is checked for the ``QuickProperty_Collection`` container, and the
        answer is three-way: present with nothing under it this parse could
        have named -- no ``QuickProperty`` element and no ``Nickname`` element
        -- it is a type that defines none and returns ``([], version)``, an
        emptiness that is a fact about the type (ADR 0144); present with either
        element there but no usable name, it holds properties this parse cannot
        name and returns ``(None, version)`` as a 204 does; absent, the
        200 is the HMC's known ``HttpErrorResponse``-feed shape and still raises
        ``HMCError``, because without the container the two are
        indistinguishable.

        Sends ``Accept: */*``: ``quick/`` endpoints answer 406 to a typed uom
        Accept, as ``get_quick_property`` records.
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        uuid_path_arguments: dict[str, str] = {}
        if parent_type is not None and parent_uuid is not None:
            _reject_unknown_uom_type("parent_type", parent_type)
            path = f"/rest/api/uom/{parent_type}/{parent_uuid}/{resource_type}/quick"
            uuid_path_arguments["parent_uuid"] = parent_uuid
        elif parent_type is None and parent_uuid is None:
            path = f"/rest/api/uom/{resource_type}/quick"
        else:
            raise ValueError(
                "parent_type and parent_uuid must be given together: a "
                "child-anchored read needs both the parent type and the "
                "parent instance UUID"
            )
        resp = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments=uuid_path_arguments,
            headers={"Accept": "*/*"},
        )
        schema_version: str | None = resp.headers.get("X-HMC-Schema-Version")
        if resp.status_code == 204:
            return None, schema_version
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        found = _find_all_text(resp.text, f"GET {path}", "Nickname")
        names = [n for n in found if n]
        if names:
            return names, schema_version
        # The container separates "defines none" from "not this shape at all",
        # and is only consulted when no name was found. Its presence alone is
        # tested: it carries no text of its own, so a text filter would reject
        # the very body this distinguishes.
        if not _find_all_text(
            resp.text, f"GET {path}", _QUICK_PROPERTY_CONTAINER_ELEMENT
        ):
            raise HMCError(
                f"GET {path} returned no {_QUICK_PROPERTY_CONTAINER_ELEMENT} "
                "element; expected the quick-property names the type defines",
                resp.status_code,
                resp.text,
            )
        # Container and nothing under it this parse could have named: the type
        # defines no quick properties, and that is a fact about the type
        # (ADR 0144). A Nickname whose text is empty, or a QuickProperty
        # carrying no Nickname at all, is the parse-artefact shape instead --
        # the container holds properties this parse cannot name -- so it reads
        # as unknown, like a 204.
        unnamed = found or _find_all_text(
            resp.text, f"GET {path}", _QUICK_PROPERTY_ELEMENT
        )
        return (None if unnamed else []), schema_version

    async def search_uom(
        self,
        resource_type: str,
        property_name: str,
        property_value: str,
        *,
        validate: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /rest/api/uom/{ResourceType}/search/({Property}=={Value}).

        The HMC rejects an unsupported search property rather than returning an
        empty feed: the property names a search may use are per type and
        published at the ``/search`` discovery anchor. **The captured status is
        500, not the 400 this was written against** -- V1_17_0 and V1_20_0 both
        answer ``ReasonCode: Unknown internal error.`` with the message *The
        left hand side of the expression is not a registered search parameter*.
        A 500 is a worse round trip than a 400, not a better one, which is the
        cost this method's pre-flight exists to remove; both statuses surface
        as ``HMCError``, so only the reason recorded here changes.

        With *validate* the name is checked against the ones the type defines
        before anything is sent, raising ``ValueError`` on a name the HMC does
        not know. The names come from ``list_search_parameters`` and are cached
        for this client's lifetime, so validating costs at most one extra
        request per resource type per session. It is off by default: the client
        is constructed per tool call, so the cache would rarely be reused and
        every call would pay that request (ADR 0142). A level where the
        discovery read itself fails validates nothing rather than raising, and
        a transport failure is cached as durably as a firmware-level one -- a
        transient one therefore leaves validation off for that type until a new
        client is constructed.

        A type defining no search parameters is refused locally, every name of
        it: ``ManagementConsole`` answers the anchor with the container and no
        parameter, which is a fact about the type rather than a failed read,
        and six of the eleven captured types answer that way (ADR 0144). A 204,
        a failed read, and a container holding parameters this parse cannot
        name still validate nothing.
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        # Ahead of the `validate` read, not beside the encoding below it: an
        # over-long value would otherwise pay a discovery request before being
        # refused. Cheap local argument checks go together (ADR 0150).
        _reject_over_long_path_value("property_value", property_value)
        _reject_over_long_path_value("property_name", property_name)
        if validate:
            defined = await self._defined_search_parameter_names(resource_type)
            if defined is not None and property_name not in defined:
                detail = (
                    f"Defined names: {_summarize_names(defined)}"
                    if defined
                    else "The type defines none at all."
                )
                raise ValueError(
                    f"{resource_type} defines no search parameter named "
                    f"{property_name!r}. {detail}"
                )
        encoded_property = quote(property_name, safe="")
        encoded_value = quote(property_value, safe="")
        path = (
            f"/rest/api/uom/{resource_type}/search/"
            f"({encoded_property}=={encoded_value})"
        )
        xml = await self._get(path, resource_type)
        if not xml:
            return []
        return _parse_feed(xml, path)

    async def list_search_parameters(
        self, resource_type: str
    ) -> tuple[list[str] | None, str | None]:
        """GET the search-parameter names a type defines, with the schema version.

        Reads ``/rest/api/uom/{R}/search``. This is the type-anchored anchor,
        not the instance search ``search_uom`` builds -- the names it returns
        are what that search's property argument may be, which the HMC
        otherwise answers with an HTTP 500 (captured; see ``search_uom``).

        **No captured level serves a child-anchored form.** The corpus
        documents ``/rest/api/uom/{P}/{UUID}/{C}/search``; V1_17_0 and V1_20_0
        both answer it 400 ``INVALID_URL`` -- "REST000B The URL presented to
        the Management Console REST Web Services is not valid." -- and at
        V1_17_0 that holds for both ``LogicalPartition`` and
        ``VirtualIOServer`` under a parent answering its plain child feed and
        its ``/quick`` anchor 200 in the same session. The HMC calls the URL *shape* invalid while serving two other
        child anchors on that exact parent, so this is the form being absent
        rather than the parent being wrong. Parent arguments were removed on
        that evidence; see ADR 0142.

        **The response shape is captured, at V1_17_0 and V1_20_0.** The root
        anchor answers 200 ``application/atom+xml`` with an ``<entry>`` whose
        content is a ``<SearchParameterSet>``: an ``<ElementName>`` naming the
        type, then a ``<SearchParameters>`` holding one ``<SearchParameter>``
        per property, each with ``<ParameterName>``, ``<Comparator>`` and
        ``<XPath>``. The names are the ``ParameterName`` texts. The vendored
        corpus documents the path and never the body, so this shape comes from
        the capture on PR #807 and nowhere else.

        They are read document-wide rather than through ``_parse_feed``, which
        flattens an entry to a dict and collapses a repeated element to a bare
        value when the HMC sends exactly one -- the hazard ADR 0139 recorded
        for ``OperationSet``, and reachable here for any type defining a single
        search parameter -- ``Cluster`` defines exactly one, so that collapse
        is reachable, not theoretical. An empty name is dropped.

        **A 200 with no name is not always an error, and the empty answers are
        not all the same answer.** A body carrying ``<SearchParameterSet>`` and
        nothing under it this parse could have named -- no ``SearchParameter``
        element and no ``ParameterName`` element -- is a type that defines no
        search parameters, and returns ``([], version)``: ``ManagementConsole``
        answers exactly that at both captured levels, and that emptiness is a
        fact about the type (ADR 0144). A body carrying either element with no
        usable name holds parameters this parse cannot name, so it returns
        ``(None, version)`` as a 204 does. A 200 carrying
        no usable name and no container raises ``HMCError``, because the HMC is
        known to answer 200 with an ``HttpErrorResponse`` feed and without the
        container that is indistinguishable from a type defining nothing.

        Returns the names paired with the response's ``X-HMC-Schema-Version``,
        with a first element of ``None`` when the level's answer is not a fact
        about the type -- a 204, or a container holding parameters this parse
        cannot name -- and a list, empty or not, when it is. The version is
        ``None`` when the HMC sends none; that value is verbatim and is not
        guaranteed to hold a version: ADR 0139 and ADR 0140 both record the HMC
        echoing the request's ``X-Audit-Memento`` into it, and the capture
        confirms it here -- every 200 came back with this client's own memento
        in that header, never a level. Callers must not parse it as one.

        Sends ``Accept: */*``. The captured content type is
        ``application/atom+xml``, and the capture also sent
        ``application/atom+xml; type=feed`` and got 200 with a byte-identical
        body. A typed *uom* Accept was not probed. So ``*/*`` is kept on the
        ground that survives: it is the one Accept that cannot fail
        negotiation on a level nobody has measured. A level insisting on one
        answers 406, which surfaces as ``HMCError``.

        A type that does not serve the anchor surfaces as ``HMCError``
        carrying that status. An unrecognised type is rejected at the URL with
        **400 ``INVALID_URL``, not 404** -- captured here, not merely predicted
        from ADR 0139.
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        path = f"/rest/api/uom/{resource_type}/search"
        resp = await self._request("GET", path, headers={"Accept": "*/*"})
        schema_version: str | None = resp.headers.get("X-HMC-Schema-Version")
        if resp.status_code == 204:
            return None, schema_version
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        found = _find_all_text(resp.text, f"GET {path}", _SEARCH_PARAMETER_NAME_ELEMENT)
        names = [n for n in found if n]
        if names:
            return names, schema_version
        # The container separates "defines none" from "not this shape at all",
        # and is only consulted when no name was found. Its presence alone is
        # tested: it carries no text of its own, so a text filter would reject
        # the very body this distinguishes.
        if not _find_all_text(
            resp.text, f"GET {path}", _SEARCH_PARAMETER_CONTAINER_ELEMENT
        ):
            raise HMCError(
                f"GET {path} returned no {_SEARCH_PARAMETER_CONTAINER_ELEMENT} "
                "element; expected the search parameters the type defines",
                resp.status_code,
                resp.text,
            )
        # Container and nothing under it this parse could have named: the type
        # defines no search parameters, and that is a fact about the type
        # (ADR 0144). A ParameterName whose text is empty, or a SearchParameter
        # carrying no ParameterName at all, is the parse-artefact shape instead
        # -- the container holds parameters this parse cannot name -- so it
        # reads as unknown, like a 204.
        unnamed = found or _find_all_text(
            resp.text, f"GET {path}", _SEARCH_PARAMETER_ELEMENT
        )
        return (None if unnamed else []), schema_version

    async def _defined_search_parameter_names(
        self, resource_type: str
    ) -> frozenset[str] | None:
        """The search-parameter names *resource_type* defines, or None if unknown.

        Reads the root ``/search`` anchor once per type per client and caches
        the answer, the failure included: a level where discovery does not work
        yields None, which callers read as "do not validate", while a type that
        defines no search parameters yields the empty positive set, which they
        read as "refuses every name" (ADR 0144). A transport failure is cached
        as durably as a firmware-level one, so a transient one leaves validation
        off for this type until a new client is constructed.
        """
        if resource_type in self._search_parameter_names:
            return self._search_parameter_names[resource_type]
        async with self._search_parameter_names_lock:
            # Re-check under the lock: a task that waited here may have been
            # waiting on the very read that populates this entry, and the cost
            # bound is per type per client, not per caller.
            if resource_type in self._search_parameter_names:
                return self._search_parameter_names[resource_type]
            try:
                names, _ = await self.list_search_parameters(resource_type)
            except HMCError:
                # Covers HMCTransportError too, which subclasses it. ADR 0142
                # degrades rather than raising so a level that does not serve
                # the anchor -- or a wrong parsed element -- cannot break
                # search_uom, which an opt-in pre-flight must not do.
                names = None
            # None is the level's answer not being a fact about the type: a 204, a
            # failed read, or a container holding parameters this parse cannot
            # name. An empty list is a fact -- the six captured types defining
            # nothing -- and caching it as an empty positive set is what makes
            # validate=True refuse locally there (ADR 0144).
            defined = None if names is None else frozenset(names)
            self._search_parameter_names[resource_type] = defined
            return defined

    async def list_operations(
        self,
        resource_type: str,
        *,
        parent_type: str | None = None,
        parent_uuid: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET the job operations a type defines, with the schema version.

        Reads the root anchor ``/rest/api/uom/{R}/operations``, or the child
        anchor ``/rest/api/uom/{P}/{UUID}/{C}/operations`` when both
        *parent_type* and *parent_uuid* are given; supplying exactly one of
        them is a caller error.

        **Returns one entry, not one per operation.** The HMC answers with a
        single ``OperationSet`` naming the type in ``SetName`` and holding
        every operation the type defines under ``DefinedOperations``::

            entries, schema_version = await hmc.list_operations("ManagedSystem")
            operations = entries[0]["Resource"]["DefinedOperations"]["Operation"]

        ``Operation`` is a **list when the type defines several operations and
        a bare dict when it defines exactly one**, because ``element_to_dict``
        keys children by tag and only promotes to a list on the second
        sibling. The same collapse applies to ``OperationParameter`` under
        ``AllPossibleParameters`` and ``AllPossibleResults``, and to
        ``NLSStaticMessage`` under ``AllDiscreteStates`` -- within a single
        response, one operation's results can be a dict while another's are a
        list. Normalise with ``x if isinstance(x, list) else [x]`` before
        iterating; iterating without it walks dict *keys* and raises nothing.
        ``AllPossibleParameters`` is absent for an operation that takes no
        parameters rather than present and empty, and ``AllDiscreteStates``
        appears only when ``ProgressType`` is ``DISCRETE``.

        The second element is the response's ``X-HMC-Schema-Version``, or
        ``None`` when the HMC sends none (ADR 0139). **It is returned verbatim
        and is not guaranteed to be a version string.** On firmware observed at
        V1_17_0 this endpoint echoes the request's ``X-Audit-Memento`` value
        into that response header, so it reads ``hmcpctl`` rather than a level;
        the same firmware returns a real level on ordinary uom feeds. Treat it
        as an opaque provenance tag unless it matches a level you recognise.

        Unlike the reads that go through ``_get``, this method does **not**
        send a configured ``HMC_SCHEMA_VERSION`` request header: it passes its
        own headers straight to the transport and never reaches
        ``_uom_headers``. Pinning a schema version therefore has no effect
        here. That is deliberate -- the endpoint's negotiation is confirmed
        working with ``Accept: */*`` alone and nothing establishes that it
        honours the header -- but it is a real asymmetry with the sibling
        reads, and a caller relying on a pinned version should know it.

        Sends ``Accept: */*``. The content element is in the ``web/mc``
        namespace with content type
        ``application/vnd.ibm.powervm.web+xml; type=OperationSet``, not a uom
        media type, so a typed uom Accept is the wrong guess rather than a
        stricter one; ``*/*`` is the one Accept that cannot fail negotiation.
        A firmware level insisting on a typed Accept answers 406, which
        surfaces as ``HMCError`` carrying 406.

        An unrecognised *resource_type*, *parent_type* or child type is rejected
        at the URL with **400 ``INVALID_URL``, not 404** (observed at V1_17_0):
        the firmware validates the type name before reaching any handler that
        would look up operations, and names the type it did not recognise --
        ``REST000E`` for an unknown root type, ``REST000C``/``REST000D`` for an
        unknown child under a known parent. Either surfaces as ``HMCError``
        carrying 400 and that message.

        Not available on every level: three HMCs at V1_20_0 answered 500 with
        ``java.lang.ClassNotFoundException`` naming a firmware-internal
        operations class, against one working sample at V1_17_0. That is a
        server-side defect no request header changes; it surfaces as
        ``HMCError`` carrying 500. Callers that must work across levels should
        expect it.
        """
        _reject_unknown_uom_type("resource_type", resource_type)
        uuid_path_arguments: dict[str, str] = {}
        if parent_type is not None and parent_uuid is not None:
            _reject_unknown_uom_type("parent_type", parent_type)
            path = (
                f"/rest/api/uom/{parent_type}/{parent_uuid}/{resource_type}/operations"
            )
            uuid_path_arguments["parent_uuid"] = parent_uuid
        elif parent_type is None and parent_uuid is None:
            path = f"/rest/api/uom/{resource_type}/operations"
        else:
            raise ValueError(
                "parent_type and parent_uuid must be given together: a "
                "child-anchored read needs both the parent type and the "
                "parent instance UUID"
            )
        resp = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments=uuid_path_arguments,
            headers={"Accept": "*/*"},
        )
        schema_version: str | None = resp.headers.get("X-HMC-Schema-Version")
        if resp.status_code == 204:
            return [], schema_version
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        # No empty-body guard: the sibling reads carry one because ``_get``
        # collapses 204 to "", so they cannot tell the two apart. This method
        # returns on 204 above, and an empty 200 body is a malformed feed --
        # ``_parse_feed`` reporting it as HMCError is the honest answer.
        return _parse_feed(resp.text, path), schema_version

    # Virtual adapters (children of LogicalPartition)

    async def list_child(
        self, parent_type: str, parent_uuid: str, child_type: str
    ) -> list[dict[str, Any]]:
        """GET /rest/api/uom/{parent}/{uuid}/{child} and parse the feed."""
        _reject_unknown_uom_type("parent_type", parent_type)
        _reject_unknown_uom_type("child_type", child_type)
        path = f"/rest/api/uom/{parent_type}/{parent_uuid}/{child_type}"
        xml = await self._get(
            path,
            child_type,
            uuid_path_arguments={"parent_uuid": parent_uuid},
        )
        return _parse_feed(xml, path) if xml else []

    async def create_child(
        self, parent_type: str, parent_uuid: str, child_type: str, child_xml: str
    ) -> dict[str, Any] | None:
        """PUT a child resource (e.g. a virtual adapter) under a parent.

        Omits the X-HMC-Schema-Version header; the write headers follow ADR 0178.
        """
        _reject_unknown_uom_type("parent_type", parent_type)
        _reject_unknown_uom_type("child_type", child_type)
        path = f"/rest/api/uom/{parent_type}/{parent_uuid}/{child_type}"
        xml = await self._put(
            path,
            child_xml,
            resource_type=child_type,
            include_schema_version=False,
            uuid_path_arguments={"parent_uuid": parent_uuid},
        )
        entries = _parse_feed(xml, path) if xml else []
        return entries[0] if entries else None

    async def delete_child(
        self, parent_type: str, parent_uuid: str, child_type: str, child_uuid: str
    ) -> None:
        """DELETE a child resource instance."""
        _reject_unknown_uom_type("parent_type", parent_type)
        _reject_unknown_uom_type("child_type", child_type)
        await self._delete(
            f"/rest/api/uom/{parent_type}/{parent_uuid}/{child_type}/{child_uuid}",
            uuid_path_arguments={
                "parent_uuid": parent_uuid,
                "child_uuid": child_uuid,
            },
        )

    async def get_uom_path(
        self, path: str, resource_type: str
    ) -> dict[str, Any] | None:
        xml = await self._get(path, resource_type)
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        return entries[0] if entries else None

    # Jobs (long-running operations)

    async def submit_job(
        self, job_path: str, job_request_xml: str
    ) -> dict[str, Any] | None:
        """PUT a JobRequest to /rest/api/uom/.../do/{Operation} and return the job.

        `job_path` is the full do-path, e.g.
        /rest/api/uom/LogicalPartition/{uuid}/do/PowerOn

        The HMC requires PUT (not POST) for do/ job operations, web+xml media
        types, and atom+xml Accept — as confirmed by the ansible-power-hmc
        reference implementation.
        """
        resp = await self._request(
            "PUT",
            job_path,
            content=job_request_xml,
            headers={
                "Content-Type": f"{MEDIA_WEB}; type=JobRequest",
                "Accept": "application/atom+xml",
            },
        )
        if resp.status_code not in (200, 201, 202):
            raise HMCError(f"PUT {job_path} failed", resp.status_code, resp.text)
        entries = _parse_feed(resp.text, job_path) if resp.text else []
        return entries[0] if entries else None

    async def get_job_entry(
        self,
        job_id: str,
        *,
        job_href: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch an HMC job by JobID.

        The documented global endpoint is ``/rest/api/uom/jobs/{id}`` and uses
        the ``web+xml`` content type. It resolves the entry's ``JobID``; a V10R3
        HMC answers the Atom entry UUID with HTTP 406 to this web+xml Accept, and
        with 400 REST000E to ``Accept: */*`` (issue #1160, api-patterns P4).

        When *job_href* is provided (a SELF link from a job entry), its job path
        is read instead; it must be a ``/rest/api/uom/jobs/{JobID}`` path, which
        is what lets a handle that stored the entry UUID as *job_id* still read
        its job. A read-side ``/rest/api/uom/jobs/{JobID}/{uuid}`` SELF
        link is addressed by its JobID segment; the HMC refuses the link itself
        (HTTP 400 REST000B) and its trailing UUID changes on every read.

        Either argument produces one path, and that path is refused as
        :class:`HMCError` unless its raw and decoded forms preserve the job
        resource and segment boundaries — the same refusal ``delete_job``
        applies (ADR 0152).
        """
        path = _job_request_path(job_id, job_href)
        if "/templates/jobs/" in path:
            xml = await self._templates_get(path)
        else:
            xml = await self._web_get(path)
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        return entries[0] if entries else None

    async def wait_for_job_entry(
        self,
        job_id: str,
        timeout_seconds: int = 300,
        poll_interval: int = 5,
        *,
        job_href: str | None = None,
    ) -> dict[str, Any] | None:
        """Poll an HMC job until it reaches a terminal state or timeout.

        Terminal states cover the UOM Job and documented web+xml JobResponse
        completion, failure, warning, and cancellation values.
        Returns the last-seen job entry (terminal or not, after timeout).

        When *job_href* is provided it is forwarded to ``get_job_entry`` so polling
        reads that link's jobs path instead of one built from *job_id*.
        """
        import asyncio

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be greater than or equal to 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than 0")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        entry = await self.get_job_entry(job_id, job_href=job_href)
        while True:
            resource = (entry or {}).get("Resource")
            status = resource.get("Status", "") if isinstance(resource, dict) else ""
            if status in TERMINAL_JOB_STATUSES:
                return entry
            remaining = deadline - loop.time()
            if remaining <= 0:
                return entry
            await asyncio.sleep(min(poll_interval, remaining))
            if loop.time() >= deadline:
                return entry
            entry = await self.get_job_entry(job_id, job_href=job_href)

    async def delete_job(
        self,
        job_id: str,
        *,
        job_href: str | None = None,
    ) -> None:
        """Delete a job, preferring its SELF link when available.

        The link resolves to a path exactly as ``get_job_entry`` resolves it, and
        paths whose raw and decoded forms disagree on job-resource structure are
        refused the same way (ADR 0152).
        """
        path = _job_request_path(job_id, job_href)
        await self._delete(path)

    # Raw escape hatch

    async def raw_get(
        self, path: str, accept: str = "*/*"
    ) -> tuple[str, dict[str, str]]:
        """GET a raw path and return (body, response_headers).

        Returns a 2-tuple so callers can inspect response headers such as
        ``X-HMC-Schema-Version`` to discover the schema version in effect.
        """
        resp = await self._request("GET", path, headers={"Accept": accept})
        if resp.status_code == 204:
            return "", dict(resp.headers)
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        return resp.text, dict(resp.headers)

    async def raw_post(
        self, path: str, body: str, content_type: str = "application/xml"
    ) -> str:
        resp = await self._request(
            "POST", path, content=body, headers={"Content-Type": content_type}
        )
        if resp.status_code not in (200, 201, 202):
            raise HMCError(f"POST {path} failed", resp.status_code, resp.text)
        return resp.text
