# Live failure session redaction

## Scope and design
Issue #1896, frozen comment6645350743/token q1896-f2aadcc8, requires the shared
runner FAIL boundary independently of #1884. Extend `_redact_failure_text` and
`_redact_failure_data`; retain their callers and JSON row shape. ADR0337 records why.

Before generic redaction, scan XML lexical tokens with stdlib regex: tags with
quoted attributes, CDATA and comments. Track nested named sensitive elements
(RequestHeaders, Cookie, Set-Cookie, sessionCookie, X-API-Session), ignoring case
and allowing namespace prefixes. CDATA/comments cannot terminate a region;
self-closing elements end immediately. Unclosed regions consume through EOF.
Collect identifiable named session values before replacing sensitive XML regions
and dictionary values with `<REDACTED-SECRET>`. Collect header values with quoted
value handling, XML text/CDATA and named dictionary values; stdlib cookie parsing
extracts cookie values. Retain raw and unquoted/XML-entity-decoded source values.
Then remove named session header lines (colon/equals,
quoted keys, whitespace-prefixed continuation lines), before existing generic
secret/path redaction. Header removal includes the whole credential-bearing line.
A preparation traversal collects values across one complete failure data object;
a finishing traversal replaces their exact echoes across its string values with
one escaped-literal pattern before generic redaction. Longer values match first.
No rescanning to a fixed point, speculative encodings or cross-row secret registry.
`_redact_failure_text` uses the same boundary for a single string. Empty values
are not replacement patterns. Invalid cookie syntax retains the raw named value.

## Success and validation
C1/C2: constructed raw header/XML failures, nested dictionaries and CallFailure
messages lose named values and their identifiable exact echoes in console and JSON.
C3: preserve row status/shape and non-sensitive sibling diagnostics. Offline
runner tests cover case/prefix/attributes, repetition, truncation, CDATA, comments,
nesting, self-closing sibling messages and earlier/later nested exact echoes. Full verification and hooks must pass.

## Failure model
- Actors/deployments: local live-test operators; HMC/transport diagnostic input;
  offline tests exercise the same output boundary without HMC access.
- Assets/invariants: named session fields do not survive FAIL persistence/printing;
  diagnostics outside removed regions and row semantics retain existing behavior.
- Accepted: bounded loss inside credential-bearing regions; opaque/arbitrary
  encodings without named fields are not decoded. Named XML CDATA remains covered.
- Covered elsewhere: exception retention/#1884; historic documents/operator;
  PASS-row redesign/outside/operator, exactly as approved.

## Threat model
- Boundary: existing untrusted HMC/transport diagnostics -> runner -> console/JSON;
  no new or widened entry points. Local runner code remains trusted.
- Controls: shared-writer redaction before both sinks; direct raw-input tests
  prevent upstream sanitization from masking a missing output control.
- Out of scope: the approved owners above; no arbitrary-secret classifier.
