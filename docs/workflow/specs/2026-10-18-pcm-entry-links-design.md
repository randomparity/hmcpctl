# Reject incomplete PCM entry links
Full-spec S denominator100, operator-approved new PCM error contract.

## Scope and success
C1: metric_links in client/pcm_payloads.py raises descriptive ValueError when an
Atom entry's existing first selected link is missing or its href is missing,
empty or whitespace-only. Fail the feed if one entry is incomplete; do not
silently discard it. Empty entry-free feeds still return [].
C2: preserve valid first-link selection, href bytes, updated/title extraction,
entry order, newest-document selection and XML/security exception distinctions.
No new link-relation policy, root validation, metadata model or dependency.
C3: document the ValueError; existing PCM client/operation/adapters propagate
incomplete reads as failure, rather than empty metrics. No HTTP/auth redesign.

## Failure model
Actors: callers of helper and existing PCM client/operation paths, with valid,
empty or incomplete Atom entries. Assets: completeness/no falseempty telemetry,
valid metric link metadata and current transport/security behavior.
Accepted classes: existing non-entry XML semantics and link selection remain
unchanged; this correction only rejects incomplete selected entry links.
Other owners: _tag_parse_errors preserves XML/security errors, metric selection
and credential/read authorization remain existing implementations.

## Validation
Focused-test: missing link/href, empty/blank href, mixed good/bad entries must
fail old implementation; valid first link and metadata/empty feed stay exact.
Exercise existing actual client/operation metrics path with incomplete feed and
show failure, no JSON fetch or false {} success. Use existing test_unit_pcm
fixtures and captured valid feed; controlled guard removal makes new tests fail.
Run tests/unit/test_pcm.py and test_pcm_lpar_paths.py plus exact consumer tests,
--no-cov; lint/format/types/diffcheck and actual14commit hooks. Root finalfullverify.
