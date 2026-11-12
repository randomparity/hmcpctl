# Sanitize HMC error diagnostics (#1884)

Decision record: [ADR 0369](../../adr/0369-sanitize-hmc-error-diagnostics.md).

## Problem

`HMCError` keeps up to 4096 bytes of the received body as `.body` and renders
`body[:500]` when the body has no `<Message>` or is not XML. HMC error bodies are
`HttpErrorResponse` documents whose `<RequestHeaders>` echo the request's session
cookie and `X-API-Session` token, so a failed call carries a live credential into
exception text, logs, tool results and operation warnings. Only the native profile
write (`client/client_profiles.py`) extracts `<Message>` first.

## Design

`HMCError.__init__` stays the single owner. For a non-empty body it calls one
private `errors.py` function, `_sanitized(body) -> (retained, detail)`; `None`
and `""` are kept unchanged and render nothing, as today.

1. **Parse** the received `str` with `defusedxml.ElementTree.fromstring` (a `str`,
   so a declared encoding cannot reinterpret characters). `ParseError` or
   `DefusedXmlException` withholds the body (step 5). Any other exception
   propagates, as today.
2. **Collect credential values** from the identifiable sources, the set this spec
   means by "identifiable":
   - a `cookie` or `x-api-session` label (case-insensitive, also inside a longer
     name such as `set-cookie`) followed by `=` or `:` in the document's
     concatenated character data or in any attribute value; the value ends at
     the first CR, LF, `}`, or `, <name>=` (the next `RequestHeaders` map entry);
   - an element whose local name is `Cookie` or `X-API-Session`: its text and
     attribute values;
   - a parent with a child whose whole stripped text is such a label: every
     child's text (a `Name`/`Value` header pair).
   Each value contributes its stripped whole, each `;`/`,`-separated part, each
   part's right-hand side after its first `=`, and each of those with surrounding
   quotes stripped. Empty strings are dropped. No cookie-name allowlist.
3. **Keep only the code-bearing fields**: the text of the first `ReasonCode`
   (100 characters) and first `Message` (500 characters) element by local name.
   In each, within its first 8192 characters: a label cuts the text there and
   appends `<redacted>`; then every collected value is replaced with
   `<redacted>`, longest first; then the field is truncated to its cap.
4. **Retain** `<HttpErrorResponse>` holding the kept fields, plus
   `<Recognized>` when the received body contains a recognized plaintext marker
   (only `Nested path contains null property`, ADR 0138), built with
   `saxutils.escape`. Maximum size about 3.2 KB, so `MAX_ERROR_BODY_BYTES`
   never truncates it. Render the kept Message, or
   `no Message in the <N>-character response body`.
5. **Withhold** an unparseable body, or one whose collection exceeds 64
   assignments or a 4096-character value, or where a collected value still
   occurs after replacement: retain `<HttpErrorResponse>` whose Message is
   `<unparseable|unsanitizable> <N>-character response body withheld`, plus
   `; recognized: <marker>` and `<Recognized>` when one matched. The rendered
   detail is that Message. The operation label and HTTP status always remain.

Re-sanitizing a retained body returns it unchanged, so the sites that re-wrap
`exc.body` in a new `HMCError` (`rg -n 'exc\.body|error\.body' src`) keep working
without a private bypass.
`client_profiles.py` passes `response.text` and drops its own extraction.

## Success

- S1: for the echoed-header fixture (captured `HttpErrorResponse` with a token
  Cookie and X-API-Session, echoed once in `Message`), neither `str(error)` nor
  `error.body` contains any token, `RequestHeaders`, or `RequestBody`.
- S2: REST0269, HSCLB021, REST0001, VTD name-in-use and null-property
  classifiers return their current results for the captured/fixture bodies.
- S3: every non-XML or truncated body renders `<label> (HTTP <status>): unparseable
  <N>-character response body withheld` and retains no received character.
- S4: `HMCError(m, s, e.body).body == e.body` for every retained body above.
- S5: a native profile write failure renders the HMC Message without the echo.

## Failure model

1. Actors and deployments: local operator or MCP client running hmcpctl against
   a lab HMC; the HMC is authenticated by TLS and trusted to answer, not to keep
   credentials out of its diagnostics.
2. Invariants and assets: the session cookie and X-API-Session token; the
   `.body` substring contract of the five code lookups in S2.
3. Accepted failure classes:
   - Credential-like text with no identifiable source (step 2): operator-owned
     exclusion in the charter.
   - Message text after a label, Message beyond 500 characters, and every other
     field are dropped: bounded diagnostic loss; no classifier reads them.
   - Withheld bodies lose body-derived codes: fail closed, label and status kept.
   - Short collected values (for example `/`) over-redact Message text: bounded,
     the text is diagnostic only.
4. Covered elsewhere: warning policy #1885; result-document redaction #1896;
   secrets in application-supplied labels and historical documents: operator.

### Threat model

- Boundary (existing, narrowed): HMC response body into `HMCError` text/`.body`,
  then to logs, CLI output, MCP tool results and stored warnings.
- Actor: the HMC echoing request headers; anyone later reading those sinks.
  Trust stays with the TLS-authenticated HMC for parsing (defusedxml, bounded by
  the existing response-size cap).
- Control: allow-listed fields only, value replacement, fail-closed withholding;
  leaks on failure only the body length and a fixed marker.
- Out of scope: the exclusions above; a caller assigning raw text to `.body`.

## Validation

Focused: `uv run --no-sync pytest tests/unit/test_errors.py -q --no-cov`.
Tokenized echoed-header fixture tests (S1, S4), withheld/truncated tests (S3),
classifier tests (S2), profile test (S5). Controlled faults: restore the
`body[:500]` fallback and raw retention; each must turn S1/S3 red. Closure: the
caller tests asserting raw `.body` equality or raw plaintext rendering are
updated to the retained form; `tests/app/test_system_hold_coverage.py` returns an
`HttpErrorResponse` sentinel. `just verify` and all-files prek before push.
