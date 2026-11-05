# ADR 0333: Safe HMC failures in logical-operation warnings

## Status

Accepted for issue #1885.

## Context

The logical engine persists `_failure` output and returns it in operation status.
HMCError text can include remote message/body diagnostics. Issue #1885 requires
safe metadata while retaining curated OperationFailed advice.

## Decision

Keep rendering in `_failure`; for HMCError and its subclasses emit the concrete
exception type and HTTP status, explicitly marking an absent status unavailable.
Do not render its message/body or parse remote text for optional error codes.
Keep other exception rendering, warning bounds and store structure unchanged.

## Consequences

New body and reconciliation warnings disclose less diagnostic detail; callers retain
status/type for triage. Historical records are unchanged. Transport sanitization
belongs to #1884 and is independently useful.

## Considered & rejected

- Sanitize only transport errors. judgment: the engine's durable disclosure boundary
  needs its own policy, including errors created outside transport.
- Render an allowlisted substring of remote text. judgment: recognizing safe prose
  adds ambiguity when type and status satisfy the issue.
- Change the store schema or migrate historical warnings. judgment: unnecessary to
  prevent newly persisted warnings and outside the approved scope.
