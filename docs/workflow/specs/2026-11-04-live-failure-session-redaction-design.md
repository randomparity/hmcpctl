# Live failure session redaction

## Problem and scope

Issue #1896 requires a credential boundary at the live runner's failure writer,
independent of exception sanitization in #1884. `RunState.record` already routes
FAIL data and `CallFailure.message` through `_redact_failure_text`; raw-write
arms pass dictionaries containing `HMCError.body`. Extend that owner in place.
The public-safe frozen authority is issue comment 6645350743, token q1896-f2aadcc8.

## Design

Before the existing generic secret, hostname and path replacements, remove
sensitive XML regions and header values. Use the existing stdlib `re` machinery;
no dependency or public API is added. XML matching is case-insensitive, accepts
an optional namespace prefix and opening attributes, spans lines, and replaces
RequestHeaders, Cookie, Set-Cookie, sessionCookie and X-API-Session elements with
`<REDACTED-SECRET>`. A missing closing tag consumes through end of input, so
truncated error bodies fail closed. Redaction precedes the existing path pattern,
which otherwise changes XML closing tags before a later matcher could see them.

Header matching accepts Cookie, Set-Cookie, sessionCookie and X-API-Session
followed by a colon or equals sign, including a quoted mapping-style key.
Discard the rest of that line and whitespace-prefixed continuation lines.
This deliberately loses other text on a credential-bearing line rather than
splitting cookie values on semicolons, whitespace or punctuation that may belong
to a credential. Diagnostic text outside these regions retains existing behavior.
The XML matcher runs first, so ordinary message elements remain outside the cut.
For dictionary data, a case-insensitive key matching one of those session names
or RequestHeaders replaces the whole value, including nested headers. Other
dict/list recursion, CallFailure handling and output schema stay with their callers. No caller migration or compatibility shim is needed.

## Success and verification

- C1: Synthetic unsanitized header, XML and RequestHeaders payloads lose their
  session values at `_redact_failure_text`, with mixed case, namespace prefixes,
  attributes, repeated values, multiline content and truncated bodies covered.
- C2: `RunState.record` tests cover nested dictionaries/lists and CallFailure;
  an offline `runner.main` test proves both console and saved JSON omit the same
  secrets while retaining FAIL status and a separate non-sensitive message.
- C3: Existing redaction tests and runner behavior pass; `just verify` and
  `uv run --no-sync prek run --all-files` pass. No live HMC operation is needed.

## Failure model

- Actors and deployments: local operators run live-test arms; HMC or transport
  errors provide diagnostic strings; offline tests construct those same boundary inputs.
- Invariants and assets: session credentials in the named header/XML forms must
  not survive failed-result serialization or its console output; the JSON row
  structure and failure status remain compatible.
- Accepted failure classes: diagnostic text sharing a credential-bearing region
  may be removed (bounded loss required for conservative redaction); arbitrary
  encodings or opaque strings without a named credential field are not decoded.
- Covered elsewhere: exception retention is #1884; historic documents belong to
  the operator; PASS-row redesign is outside/operator, as explicitly approved.

## Threat model

- Boundary inventory: existing HMC/transport diagnostic -> runner FAIL data ->
  console and results JSON; no new or widened entry points.
- Actor model: remote diagnostic content is untrusted for publication, even when
  it arrived through an authenticated HMC; the local runner code is trusted.
- Control per boundary: redact at the shared failure writer before printing and
  serialization, independently of upstream HMCError behavior; tests inject raw
  strings without constructing an HMCError to prevent upstream masking the bug.
- Out of scope: previously saved documents, PASS rows and exception object
  retention have the explicitly approved owners above; this change adds no
  general arbitrary-secret classifier or decoder for unspecified encodings.

## Alternatives and rollback

A whole-body drop would discard useful failure context; an XML parser would need
an additional malformed/text fallback. Extend the existing text boundary instead.
The decision is recorded in ADR 0337. Reverting this change restores the known
credential leak and is unsuitable while affected failure results are shared.
