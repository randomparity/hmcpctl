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
Replace each sensitive region, and values under those dictionary keys, with
`<REDACTED-SECRET>`. Then remove named session header lines (colon/equals,
quoted keys, whitespace-prefixed continuation lines), before existing generic
secret/path redaction. Header removal includes the whole credential-bearing line.

## Success and validation
C1/C2: constructed raw header/XML failures, nested dictionaries and CallFailure
messages lose session values in console and saved JSON, without upstream help.
C3: preserve row status/shape and non-sensitive sibling diagnostics. Offline
runner tests cover case/prefix/attributes, repetition, truncation, CDATA, comments,
nesting and self-closing sibling messages. Full verification and hooks must pass.

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
