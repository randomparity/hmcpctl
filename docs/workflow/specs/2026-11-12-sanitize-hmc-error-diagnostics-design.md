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
     name such as `set-cookie`, optionally quoted) followed by `=` or `:`, found
     in each text node (`.text`, `.tail`), in the document's concatenated
     character data (labels split across nodes), and in each attribute value; the
     value ends at the first CR, LF, `}`, `, <name>=` (the next `RequestHeaders`
     map entry), or the end of the string searched; a value opening with `[`
     ends at its `]` (a `[a=1, b=2]` header list);
   - an element whose local name contains a label, or one of whose attribute
     values is a label: its text and attribute values; an attribute whose name
     contains a label: its value (labels match `cookie` or `x-api-session`, the
     hyphens optional);
   - a parent with a child whose whole stripped text is such a label: every
     child's text (a `Name`/`Value` header pair).
   Each value contributes its stripped whole, each `;`/`,`-separated part, each
   part's right-hand side after its first `=`, and each of those with surrounding
   quotes stripped, plus every maximal run of 8 or more token characters
   (`[A-Za-z0-9._~+/-]`), so a bracketed or space-separated value still yields
   the bare credential. Values with no letter or digit are dropped; more than 1024
   values withholds the body. No cookie-name allowlist.
3. **Keep only the code-bearing fields**: the joined text (`itertext`, so an echo
   split by child markup is matched whole) of the first `ReasonCode`
   (100 characters) and first `Message` (500 characters) element by local name.
   In each, within its first 8192 characters: every occurrence of a collected
   value, overlapping occurrences included, is located and the merged spans are
   replaced by one `<redacted>` each, in a single pass. A value of 8 or more
   characters matches anywhere; a shorter value matches only where neither
   neighbour is a letter or digit, so a sibling cookie such as `lb=0` cannot cut
   `REST0269`. The field keeps received text up to its cap, extended to the end
   of any echo straddling the cap, so shrinking earlier echoes never pulls a
   window-cut echo into the field. Then a label cuts the text there and appends `<redacted>` (after
   the values, so a label inside a value cannot strand the value's prefix).
   Then the field is truncated to its cap and stripped.
4. **Retain** `<HttpErrorResponse>` holding the kept fields, plus
   `<Recognized>` when the received body contains a recognized plaintext marker
   (only `Nested path contains null property`, ADR 0138), built with
   `saxutils.escape`. Maximum size about 3.2 KB, so `MAX_ERROR_BODY_BYTES`
   never truncates it. Render the kept Message, or
   `no Message in the response body`.
5. **Withhold** a body over 65536 characters (`oversized`), an unparseable body,
   or one whose collection exceeds 64 assignments, a 4096-character value, 1024
   values, or 256 redacted occurrences of one value in a field (`unsanitizable`): retain
   `<HttpErrorResponse>` whose Message is
   `<oversized|unparseable|unsanitizable> <N>-character response body withheld`, plus
   `; recognized: <marker>` and `<Recognized>` when one matched. The rendered
   detail is that Message. The operation label and HTTP status always remain.

Re-sanitizing a retained body can only redact more, never re-expose, and keeps
its kept text for every body in S1–S3, so the sites that re-wrap `exc.body` in a
new `HMCError` (`rg -n 'exc\.body|error\.body' src`) need no private bypass.
`client_profiles.py` passes `response.text` and drops its own extraction.

## Success

- S1: for the echoed-header fixture (captured `HttpErrorResponse` with a token
  Cookie and X-API-Session, echoed once in `Message`), neither `str(error)` nor
  `error.body` contains any token, `RequestHeaders`, or `RequestBody`.
- S2: REST0269, HSCLB021, REST0001, VTD name-in-use and null-property
  classifiers return their current results for the captured/fixture bodies.
- S3: every non-XML or truncated body renders `<label> (HTTP <status>): unparseable
  <N>-character response body withheld`, plus `; recognized: <marker>` when a
  recognized marker matched, and retains no received character other than that
  fixed marker literal.
- S4: `HMCError(m, s, e.body).body == e.body` for every retained body in S1–S3
  and for a Message cut at its cap next to whitespace.
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
   - An echo of a value shorter than 8 characters glued between letters or
     digits survives: session credentials are long; short values are sibling
     cookies, and matching them inside words would corrupt the codes in S2.
   - Bodies a caller cuts before raising (`resp.text[:500]`: success-response
     excerpts, and `client_pcm.py`'s failed metrics fetch) are withheld as
     unparseable and lose their Message: fail closed, label and status kept.
   - A second sanitation of a retained body may redact more than the first
     (labels formed across adjacent kept fields): never less.
4. Covered elsewhere: warning policy #1885; result-document redaction #1896;
   secrets in application-supplied labels and historical documents: operator.

### Threat model

- Boundary (existing, narrowed): HMC response body into `HMCError` text/`.body`,
  then to logs, CLI output, MCP tool results and stored warnings.
- Actor: the HMC echoing request headers; anyone later reading those sinks.
  Parsing uses defusedxml; the sanitizer's work, done synchronously in the
  event loop, is bounded by the 65536-character input bound and the step 5 limits,
  not by the 32 MiB response-size cap.
- Control: allow-listed fields only, value replacement, fail-closed withholding;
  leaks on failure only the body length and a fixed marker.
- Out of scope: the exclusions above; a caller assigning raw text to `.body`.

## Validation

Focused: `uv run --no-sync pytest tests/unit/test_errors.py -q --no-cov`.
Tokenized echoed-header fixture tests (S1, S4), withheld/truncated tests (S3),
classifier tests (S2) including a short sibling cookie beside REST0269, a
value ending its text node before `<RequestBody>`, and a quoted
`"X-API-Session": "tok"` key, profile test (S5). Controlled faults: restore the
`body[:500]` fallback and raw retention; each must turn S1/S3 red. Closure: the
caller tests asserting raw `.body` equality or raw plaintext rendering are
updated to the retained form; the `client_systems.py` comment describing
`.body` as received text and the `[Unreleased]` CHANGELOG entry are updated; `tests/app/test_system_hold_coverage.py` returns an
`HttpErrorResponse` sentinel. `just verify` and all-files prek before push.
