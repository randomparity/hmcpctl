# 0334 — Authorize recorded decommission targets

## Status

Accepted (2026-11-04); bounded integration approved for issue #1886.

## Context

The registered decommission wrapper authorizes raw arguments before its handler runs.
A target-limited grant therefore rejects documented request-ID-only resume and abandon
calls. Recorded selectors must be recovered without first allowing a call or exposing
records to callers whose tool and connection are withheld.

## Decision

Add an optional target-resolution callback to the dispatch authorizer factory. The server
supplies a decommission-only callback; the registry wrapper and public tool parameters
stay unchanged. Invoke the callback once at the first connection-matching grant inside
the existing grant loop. It reads immutable original selectors, not caller overlays or
canonical UUID substitutes. The final conjunction still belongs to one grant (ADR 0039).
Emit only the final authorization decision. Unknown or unreadable recorded targets deny;
resolved-target denial messages disclose no recorded selector values. Reject supplied
selector conflicts before effective arguments can trigger a fallback HMC read, retaining
`request_conflict` and the engine's independent binding checks.

## Consequences

Name-only grants retain exact-string meaning. Request-ID-only continuations can succeed
under their current grant. Record resolution adds a local store read only after tool and
connection eligibility; no HMC lookup resolves authorization. Provision remains governed
by its non-exhaustive orchestration grant and delegated specialist authorization.

## Considered & rejected

- **Do nothing or add only handler authorization.** verified: at `2487f390`, the registered
  wrapper's real dispatcher raises `target-selector-absent` for request-only resume and
  abandon before entering the handler (`tool_registry.authorized`, `dispatch_scope`).
- **Use a synthetic connection-only authorization.** verified: `dispatch_scope.authorize`
  emits an allow on each successful check; it cannot represent an incomplete preflight.
- **Authorize canonical UUIDs or a name/UUID union.** verified: ADR 0039 requires exact
  selector strings and explicitly declines name/UUID equivalence.
- **Change the generic registry wrapper.** judgment: the factory callback is sufficient
  while retaining argument binding, call context and schema behavior in their owner.
