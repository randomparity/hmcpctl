# ADR 0369: Sanitize HMC error diagnostics

## Status

Accepted (2026-11-12), under the operator-approved #1884 charter.

## Context

HMC error bodies echo the request's session cookie and X-API-Session token in
`<RequestHeaders>`. `HMCError` rendered `body[:500]` for non-XML bodies and kept
up to 4096 bytes of the received body as `.body`, so credentials reached
exception text and every sink downstream of it. Code lookups read `.body` for
HMC codes and markers (five lookups), and several sites re-wrap `exc.body` into a new `HMCError`.

## Decision

`HMCError` never keeps or renders a received body. It parses it and keeps a
canonical `<HttpErrorResponse>` holding only the first `ReasonCode` and
`Message` text, capped, with every identifiable Cookie / X-API-Session value and
its exact echoes replaced by `<redacted>`, and a label cutting the field.
Unparseable or out-of-bounds bodies are withheld: only their length and a fixed
recognized-marker literal survive. The canonical form is a fixed point, so
re-wrapping is safe without a bypass. The design is
[the #1884 specification](../workflow/specs/2026-11-12-sanitize-hmc-error-diagnostics-design.md).

## Consequences

`.body` is no longer the received text; tests asserting equality with it change.
Diagnostics in other elements, Message text past 500 characters or after a
cookie/session label, and every plaintext body are lost. The null-property
classifier survives through the fixed `<Recognized>` marker. Secrets with no
identifiable source remain the operator's accepted exclusion.

## Considered & rejected

- **Remove `<RequestHeaders>` and redact echoes in the received document,
  keeping the rest.** verified: the #1884 attempt of 2026-11-04/05 took this
  route and ran sixteen review rounds, each finding a new carrier (attributes,
  namespaces, comments, split text/tail, tails) before parking on a blocking
  finding (issue #1884 trajectory comments).
- **Keep only the `<Message>` text, as the profile write did.** judgment: the
  re-wrap sites would then re-parse plain text and hit the unparseable path,
  losing every code on the second wrap.
- **A private raw-bypass constructor for re-wrapping.** judgment: a second entry
  point that skips sanitation is the surface this decision removes.
- **Pass the client's own session token to `HMCError` and redact it.** judgment:
  every one of the ~25 client raise sites would change signature, and the echoed
  cookie is not the token.
