# Live failure session redaction

## Scope and design
Issue #1896, frozen comment6645350743/token q1896-f2aadcc8, requires the shared
runner FAIL boundary independently of #1884. ADR0345 records the decision.

Before generic redaction, scan XML lexical tokens with stdlib regex: tags with
quoted attributes, CDATA, comments and processing instructions. Track named elements
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
Comment/processing-instruction interiors also supply named header values. Empty
values are not replacement patterns. Invalid cookie syntax retains the raw named value.
Decoded header captures map back to raw value spans using stdlib entity decoding;
source-span scans admit at most 1,000,000 characters per failure object.

Collection admits at most 65,536 source characters and 4,096 object nodes, with
object/XML depth at most 32. Parent text collection pauses across sensitive children,
so interior scans do not overlap. At most 256 learned values and 65,536 total
pattern characters are admitted; value count times retained prepared characters must not
exceed 1,000,000 before pattern compilation/matching. Exhaustion replaces the
complete failure data with `<REDACTED-FAILURE-DATA>`, retaining the outer row
envelope; unknown sibling echoes cannot escape partial collection.

## Success and validation
C1/C2: constructed raw header/XML failures, nested dictionaries and CallFailure
messages lose named values and their identifiable exact echoes in console and JSON.
C3: preserve row status/shape and non-sensitive sibling diagnostics within resource bounds.
Offline regressions exercise both sinks with raw input; full verification and hooks must pass.

## Failure model
- Actors/deployments: local live-test operators; HMC/transport diagnostic input;
  offline tests exercise the same output boundary without HMC access.
- Assets/invariants: named session fields do not survive FAIL persistence/printing;
  diagnostics outside removed regions retain existing behavior within resource bounds;
  resource exhaustion removes all data conservatively while retaining row semantics.
- Accepted: bounded loss inside credential-bearing regions; opaque/arbitrary
  encodings without named fields are not decoded. Named XML CDATA remains covered.
- Covered elsewhere: exception retention/#1884; historic documents/operator;
  PASS-row redesign/outside/operator, exactly as approved.

## Threat model
- Boundary: existing untrusted HMC/transport diagnostics -> runner -> console/JSON;
  local runner code remains trusted.
- Controls: shared-writer redaction before both sinks; direct raw-input tests
  prevent upstream sanitization from masking a missing output control.
- Out of scope: the approved owners above; no arbitrary-secret classifier.
