"""The authorization audit record: its vocabulary and rendering.

One record per authorization decision, emitted from ``dispatch_scope.authorize`` —
the only place in the package that reaches one. See
docs/adr/0040-authorization-audit-events.md.

This module imports only the sink's :func:`emit` function from ``hmcpctl``.
``target_scope`` imports :data:`Reason` and :data:`State` from here, so every
other value arrives as a primitive. The single exception is
:data:`ATTRIBUTION_ENV`, read from
``os.environ`` here rather than passed in: no module on the decision path may name
that variable (which is what makes "attribution cannot change an outcome" an
invariant rather than a sample), and reading it through ``HMCConfig`` would apply
validators that reject exactly the malformed values worth recording.

The record's grammar is one physical line of ASCII JSON. ``ensure_ascii=True`` is the
control, not a default worth keeping: JSON encoding already escapes newlines and C0
control characters, and this is what additionally stops a bidirectional override or
U+2028 in an LPAR name passing through raw to a line-oriented reader.

Stability, so a later change knows what it may not do: a field may be added, never
renamed, removed, or retyped; a reason code may be added, never repurposed; a
consumer ignores what it does not know. There is no version field — a written rule
settles it, and a second mechanism would have to be kept in agreement with it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, get_args

from .sink import emit

#: Every caller-supplied value is truncated to this, with no marker. In band a
#: marker is forgeable; out of band it is a field on every record for something a
#: reader can already measure, since a truncated value is exactly this long.
MAX_VALUE_LENGTH: Final = 128

ATTRIBUTION_ENV: Final = "HMC_AGENT_ID"

#: What ``connection_scope.selected_connection``'s ``None`` renders as — the
#: environment/default connection, in the access policy's own vocabulary.
DEFAULT_RENDERING: Final = "<default>"

#: What its ``UNRESOLVED`` sentinel renders as. Both of these share a string space
#: with legal profile keys, so a profile literally so named is indistinguishable
#: from the sentinel in this field. Narrow enough to document rather than escape.
UNRESOLVED_RENDERING: Final = "<unresolved>"

Decision = Literal["allow", "deny"]

#: Closed vocabulary exposed for validation and documentation checks.
DECISIONS: frozenset[str] = frozenset(get_args(Decision))

Reason = Literal[
    "permitted",
    "configuration-unreadable",
    "connection-not-granted",
    "target-selector-unreadable",
    "target-unboundable",
    "target-selector-absent",
    "target-not-granted",
]

#: Closed vocabulary exposed for validation and documentation checks.
REASONS: frozenset[str] = frozenset(get_args(Reason))

Event = Literal[
    "authorization",
    "console-write",
    "install-attempted",
    "install-submitted",
    "lpar-power-off",
    "ownership-denied",
    "ownership-override",
    "power-ownership-guard",
    "records-dropped",
    "tls-fingerprint-verified",
    "tls-verification-disabled",
]

#: Closed vocabulary; ``records-dropped`` is emitted by the audit sink.
EVENTS: frozenset[str] = frozenset(get_args(Event))

#: Whether a caller supplied a readable selector value.
State = Literal["present", "absent", "unreadable"]

#: Which ADR 0011 guard entry point refused an ownership request.
OwnershipOperation = Literal[
    "lpar-mutation",
    "lpar-decommission-snapshot",
    "lpar-profile-restore",
]

#: Closed ownership-operation vocabulary.
OWNERSHIP_OPERATIONS: frozenset[str] = frozenset(get_args(OwnershipOperation))

#: Which of the guard's two rules refused. Named ``denial`` on the record and not
#: ``reason``, which already names ADR 0040's access-policy vocabulary: one key cannot
#: name two boundaries' vocabularies and still tell a reader which it is reading.
OwnershipDenial = Literal["malformed-token", "foreign-owner"]

#: Closed ownership-denial vocabulary.
OWNERSHIP_DENIALS: frozenset[str] = frozenset(get_args(OwnershipDenial))

#: Whether a console write shared the channel with the collector or held it exclusively.
ConsoleWriteMode = Literal["shared", "exclusive"]

#: What a console write sent: caller bytes, or a framed SysRq request.
ConsoleInputKind = Literal["raw", "sysrq"]

_DENY_LEVEL: Final = logging.WARNING
_ALLOW_LEVEL: Final = logging.INFO


@dataclass(frozen=True)
class AuditTarget:
    """One declared target selector, as the record carries it.

    A transport for what ``target_scope.selected_targets`` extracted; the value is
    truncated here rather than by the caller, so bounding has one owner.
    """

    kind: str
    argument: str
    state: State
    value: str | None


def resolved_connection(value: str | None) -> str:
    """Render what ``selected_connection`` returned, in policy terms.

    ``None`` is the environment/default connection and ``""`` is its ``UNRESOLVED``
    sentinel; anything else is a profile key, returned unchanged.
    """
    if value is None:
        return DEFAULT_RENDERING
    if value == "":
        return UNRESOLVED_RENDERING
    return value


def _bounded_audit_text(raw: Any) -> str | None:
    """One caller-supplied value, truncated, or ``None`` if there is none to render.

    A non-``str`` renders ``None`` rather than its ``repr()``, for the reason
    ``target_scope.target_denial`` already declines to render one: an arbitrary
    object's repr is not the caller's token in any useful sense and can carry
    anything.
    """
    if isinstance(raw, str):
        return raw[:MAX_VALUE_LENGTH]
    return None


def _connection(token: Any, resolved: str | None) -> dict[str, Any]:
    """The ``connection`` object, mirroring ``selected_connection``'s three arms."""
    if token is None or token == "":
        state: State = "absent"
    elif isinstance(token, str):
        state = "present"
    else:
        state = "unreadable"
    # Rendered only in the "present" state, so "the selector is the caller's own
    # string, or null otherwise" is true of the field rather than of two of its
    # three states: `_bounded_audit_text("")` is `""`, not None, and an empty token is absent.
    return {
        "state": state,
        "selector": _bounded_audit_text(token) if state == "present" else None,
        "resolved": resolved,
    }


def _env_var_value(name: str) -> str | None:
    """*name*'s value from the environment, matched the way ``HMCConfig`` matches it.

    Environment names are matched case-insensitively, like ``HMCConfig`` fields.

    Keys are snapshotted before lookup so a concurrent deletion cannot escape as
    ``KeyError`` while an authorization record is built.
    """
    wanted = name.lower()
    found: str | None = None
    for key in list(os.environ):
        if key.lower() == wanted:
            found = os.environ.get(key, found)
    return found


def _attribution(claim: Any, source: str) -> dict[str, Any]:
    """Unverified attribution, with its source named.

    One builder, two sources: the authorization record reads the environment while
    the ownership override reads the effective ``agent_id`` the ADR 0011 check
    compared. Naming them separately is the honest form; ``verified`` is constant
    ``False`` because neither is authenticated, and it is what lets an operator
    filter without knowing this ADR exists.
    """
    return {"claim": _bounded_audit_text(claim), "source": source, "verified": False}


def record_authorization(
    *,
    policy: str,
    tool: str,
    effect: str,
    decision: Decision,
    reason: Reason,
    token: Any,
    resolved: str | None,
    targets: tuple[AuditTarget, ...] | None,
) -> None:
    """Emit one record for one dispatch-boundary decision.

    *token* is the caller's raw connection argument and *resolved* is
    :func:`resolved_connection`'s rendering of what it resolved to — or ``None``
    when nothing was resolved at all, which is the ``configuration-unreadable`` case
    and the only one where *targets* is also ``None``. ``None`` there rather than
    ``"<unresolved>"``: a token that could not be checked has not been found absent.
    """

    def build() -> dict[str, Any]:
        event: Event = "authorization"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "policy": policy,
            "tool": tool,
            "effect": effect,
            "decision": decision,
            "reason": reason,
            "connection": _connection(token, resolved),
            "targets": None
            if targets is None
            else [
                {
                    "kind": target.kind,
                    "argument": target.argument,
                    "state": target.state,
                    "value": _bounded_audit_text(target.value),
                }
                for target in targets
            ],
            "attribution": _attribution(
                _env_var_value(ATTRIBUTION_ENV), f"environment:{ATTRIBUTION_ENV}"
            ),
        }

    emit(_ALLOW_LEVEL if decision == "allow" else _DENY_LEVEL, build)


def record_ownership_override(
    *, system: str, lpar: str, host: str, agent_id: str
) -> None:
    """Emit an approved ADR 0011 LPAR ownership override.

    Ownership events use their own fields rather than access-policy fields; all
    caller-supplied values are bounded by :func:`_bounded_audit_text`.
    """

    def build() -> dict[str, Any]:
        event: Event = "ownership-override"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "system": _bounded_audit_text(system),
            "lpar": _bounded_audit_text(lpar),
            "host": _bounded_audit_text(host),
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_ownership_denied(
    *,
    operation: OwnershipOperation,
    denial: OwnershipDenial,
    system: str,
    lpar: str,
    owner: str | None,
    host: str,
    agent_id: str,
) -> None:
    """Emit a refused ADR 0011 ownership check.

    The event is separate from overrides so filters for approved bypasses remain
    precise. ``owner`` is present only when a foreign-owner token was parsed.
    """

    def build() -> dict[str, Any]:
        event: Event = "ownership-denied"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "operation": operation,
            "denial": denial,
            "system": _bounded_audit_text(system),
            "lpar": _bounded_audit_text(lpar),
            "owner": _bounded_audit_text(owner),
            "host": _bounded_audit_text(host),
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_install_attempted(
    *, system: str, partition: str, log_path: str, host: str, agent_id: str
) -> None:
    """Emit an ``installios`` attempt before submission.

    Recording before the irreversible command preserves the ambiguous-failure
    case; bounded fields identify the target and HMC.
    """

    def build() -> dict[str, Any]:
        event: Event = "install-attempted"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "system": _bounded_audit_text(system),
            "partition": _bounded_audit_text(partition),
            "log_path": _bounded_audit_text(log_path),
            "host": _bounded_audit_text(host),
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_install_submitted(
    *,
    system: str,
    partition: str,
    pid: int,
    log_path: str,
    host: str,
    agent_id: str,
) -> None:
    """Emit the remote PID after a successful ``installios`` submission."""

    def build() -> dict[str, Any]:
        event: Event = "install-submitted"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "system": _bounded_audit_text(system),
            "partition": _bounded_audit_text(partition),
            "pid": pid,
            "log_path": _bounded_audit_text(log_path),
            "host": _bounded_audit_text(host),
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_console_write(
    *,
    system: str,
    lpar: str,
    host: str,
    mode: ConsoleWriteMode,
    input_kind: ConsoleInputKind,
    length: int,
    agent_id: str,
) -> None:
    """Emit one partition-console write before its bytes are queued (ADR 0176).

    The record never carries the written bytes, which can hold credentials;
    ``length`` is their count.
    """

    def build() -> dict[str, Any]:
        event: Event = "console-write"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "system": _bounded_audit_text(system),
            "lpar": _bounded_audit_text(lpar),
            "host": _bounded_audit_text(host),
            "mode": mode,
            "input_kind": input_kind,
            "length": length,
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_lpar_power_off(
    *,
    lpar: str,
    host: str,
    operation: str,
    immediate: bool,
    restart: bool,
    agent_id: str,
) -> None:
    """Emit one LPAR PowerOff immediately before its job is submitted (ADR 0180).

    ``operation``, ``immediate`` and ``restart`` are the values the job document
    carries, so a ``dumprestart`` crash, an ``osshutdown`` and a restart each leave
    a distinct record. ``operation`` arrives as a plain ``str`` because its closed
    vocabulary lives in ``hmcpctl.jobs``, which this module does not import; it is
    bounded like any other value.
    """

    def build() -> dict[str, Any]:
        event: Event = "lpar-power-off"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "lpar": _bounded_audit_text(lpar),
            "host": _bounded_audit_text(host),
            "operation": _bounded_audit_text(operation),
            "immediate": immediate,
            "restart": restart,
            "attribution": _attribution(agent_id, "config:agent_id"),
        }

    emit(_DENY_LEVEL, build)


def record_tls_verification_disabled(*, host: str, source: str) -> None:
    """Emit one bounded audit record with ``source: VerifySSLSource``."""

    def build() -> dict[str, Any]:
        event: Event = "tls-verification-disabled"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "host": _bounded_audit_text(host),
            "source": _bounded_audit_text(source),
        }

    emit(_DENY_LEVEL, build)


def record_tls_fingerprint_verified(*, host: str, fingerprint: str) -> None:
    """Emit one record per TLS handshake trusted by ``cert_fingerprint`` (ADR 0224)."""

    def build() -> dict[str, Any]:
        event: Event = "tls-fingerprint-verified"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "host": _bounded_audit_text(host),
            "fingerprint": _bounded_audit_text(fingerprint),
        }

    emit(_ALLOW_LEVEL, build)


def record_power_ownership_guard(
    *,
    connection: str,
    authorize_power_operations: bool | None,
    source: str,
    detail: str | None,
) -> None:
    """Emit the effective ownership guard for one startup connection."""

    def build() -> dict[str, Any]:
        event: Event = "power-ownership-guard"
        return {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "connection": _bounded_audit_text(connection),
            "authorize_power_operations": authorize_power_operations,
            "source": _bounded_audit_text(source),
            "detail": _bounded_audit_text(detail),
        }

    emit(_DENY_LEVEL, build)
