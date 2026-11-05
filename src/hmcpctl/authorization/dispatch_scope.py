"""The dispatch-boundary authorization decision: one grant, every dimension.

ADR 0036 fixed that grants combine disjunctively and a grant is evaluated
conjunctively — a call is permitted only when a *single* grant covers its tool,
its connection, and its targets together. ADR 0038 wrote that loop inside the
connection dimension and left a comment on it, because with one condition a union
across grants and a conjunction inside one give the same answer. ADR 0039 added
the second condition, which is what finally makes them differ, so the loop stopped
being a comment and became this module.

That is the whole of it. ``connection_scope`` and ``target_scope`` each answer one
dimension for one grant and are given neither the policy nor a grant sequence, so
neither can combine one grant's connection with another grant's targets. This file
is the only place in the package that iterates ``AccessPolicy.grants_for``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hmcpctl.audit import records as audit
from hmcpctl.authorization.access_policy import AccessPolicy
from hmcpctl.authorization.connection_scope import (
    ConnectionScopeError,
    connection_denial,
    connection_permitted,
    selected_connection,
)
from hmcpctl.authorization.target_scope import (
    Selected,
    TargetScopeError,
    audit_state,
    denial_reason,
    selected_targets,
    target_denial,
    targets_permitted,
)
from hmcpctl.tool_registry import Authorize, ToolSecurity


def _audit_view(
    connection: str | None,
    extracted: tuple[Selected, ...],
    *,
    scoped: bool,
) -> tuple[str | None, tuple[audit.AuditTarget, ...] | None]:
    """The half of the audit record this module builds, or nulls if it cannot.

    Guarded here rather than beside the decision, because ADR 0040's totality rule
    covers building a record as well as writing one, and ``audit._emit``'s guard
    only reaches the half ``audit`` assembles. A failure degrades the record to
    nulls rather than dropping it, and either way cannot touch the decision.

    *scoped* is false for a tool declaring no connection argument, and then
    ``resolved`` is null rather than ``resolved_connection(None)``. That function
    renders an omitted ``profile`` as ``<default>``, which is true of a call that
    could have named a connection and did not; such a call never could. Null there
    is what distinguishes the two in the stream (ADR 0039).
    """
    try:
        return (audit.resolved_connection(connection) if scoped else None), tuple(
            audit.AuditTarget(
                kind=kind,
                argument=selector,
                state=audit_state(value),
                value=value if isinstance(value, str) else None,
            )
            for kind, selector, value in extracted
        )
    except Exception:  # noqa: BLE001 - a diagnostic must not fail a call
        return None, None


def dispatch_authorizer(
    policy: AccessPolicy,
    *,
    resolve_targets: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]
    | None = None,
) -> Authorize:
    """An authorizer denying any call no single grant of *policy* covers whole.

    The returned callable is what ``server._gates`` hands to every registration
    site. It closes over the frozen policy and holds no other state.
    """

    def authorize(
        name: str, security: ToolSecurity, arguments: Mapping[str, Any]
    ) -> None:
        # A tool declaring no connection argument opens no HMC connection, so the
        # connection dimension has nothing to bound and every grant reaching the
        # tool satisfies it vacuously — ADR 0038's structural fact, unchanged. The
        # *target* dimension still decides: whether a `targets` table can bound a
        # tool is a fact about its declared selectors, not about how it opens a
        # connection. ADR 0039 keyed the wrapper on the connection argument and so
        # skipped that decision entirely; the explicit scope fields close that gap,
        # and the two spellings
        # now reach the same answer through the same loop below.
        argument = security.connection_argument
        # Indexed, not `.get`: `authorized` applies the handler's defaults and
        # `validate_security` guarantees the parameter exists, so an absent key is
        # a malformed call, and treating it as an omitted argument would silently
        # make it the default connection. A KeyError here is a registration-path
        # defect rather than an authorization outcome, so it goes unrecorded.
        token = None if argument is None else arguments[argument]

        def record(
            decision: audit.Decision,
            reason: audit.Reason,
            resolved: str | None,
            targets: tuple[audit.AuditTarget, ...] | None,
        ) -> None:
            """One emission point, closed over what every record shares.

            Typed to the closed vocabularies rather than to ``str``: this is the
            only place they are enforced, and widening here would erase the check
            at exactly the four call sites that can be checked exactly.

            No guard here. ``audit.record_authorization`` is total by construction
            — ``audit._emit`` takes a builder and catches around both the build and
            the log call — and everything above is a frozen-dataclass attribute
            read. The part this module builds for itself is guarded where it is
            built, below.
            """
            audit.record_authorization(
                policy=policy.name,
                tool=name,
                effect=security.effect,
                decision=decision,
                reason=reason,
                token=token,
                resolved=resolved,
                targets=targets,
            )

        # May raise ConnectionScopeError when the configuration cannot be read at
        # all — before any grant is examined, which is ADR 0038's behaviour
        # preserved by ordering rather than by a clause. The record is emitted and
        # the original error re-raised unchanged; `targets` and `resolved` are both
        # null because nothing was extracted and nothing was resolved.
        connection: str | None = None
        if argument is not None:
            try:
                connection = selected_connection(token, tool=name)
            except ConnectionScopeError:
                record("deny", "configuration-unreadable", None, None)
                raise
        extracted = selected_targets(security, arguments)
        resolved, audited = _audit_view(
            connection, extracted, scoped=argument is not None
        )

        connection_matched = False
        resolved_targets = False
        # One conjunction per grant, never a union across them. Both conditions
        # are evaluated against the *same* grant before it can permit, so a policy
        # whose first grant allows the connection and whose second allows the
        # targets denies. That is ADR 0036's combination rule, and it is the
        # fail-open this module exists to make structurally unavailable. A tool
        # with no connection argument skips the first condition rather than
        # bypassing the loop: the conjunction is over the dimensions that exist,
        # and the target one still has to hold within one grant.
        for grant in policy.grants_for(name):
            if argument is not None and not connection_permitted(
                connection, grant.connections
            ):
                continue
            if not connection_matched and resolve_targets is not None:
                try:
                    replacements = resolve_targets(name, arguments)
                except TargetScopeError:
                    record("deny", "target-selector-unreadable", resolved, None)
                    raise
                if replacements is not None:
                    extracted = selected_targets(security, replacements)
                    resolved, audited = _audit_view(
                        connection, extracted, scoped=argument is not None
                    )
                    resolved_targets = True
            connection_matched = True
            if targets_permitted(grant.targets, security, extracted):
                record("allow", "permitted", resolved, audited)
                return

        # Assigned inside the loop and read only after it: the message says which
        # dimension blocked, and cannot change which grant satisfied the call.
        # Epic #218 requirement 8 asks a denial to name the constraint that
        # blocked it, and this is the only place in the design where grants are
        # considered together — for the message, never for the decision.
        if argument is not None and not connection_matched:
            record("deny", "connection-not-granted", resolved, audited)
            raise connection_denial(name, policy.name, argument, token, connection)
        record("deny", denial_reason(security, extracted), resolved, audited)
        if resolved_targets:
            raise TargetScopeError(
                f"{name} is not permitted for this operation's recorded targets "
                "by the access policy."
            )
        raise target_denial(name, policy.name, security, extracted)

    return authorize
