# Exporter serial-name source (#1682)

## Problem

JSON serial keys and keyword-preserving key=value names repeat the same policy
and have drifted. Issue #1682 requests one source without changed output.

## Scope

Use one ordered tuple of serial names beside `_KEYWORD_SERIAL_KEY`.
Its underscore-suffixed names derive the existing factored regex, preserving
prefix order and the exact `(?:virtual|logical|cluster|peer_collective)_serial_num`
text. Normalize the same tuple through `_key` for `_SERIAL_KEYS`.
The remaining serial spellings stay in that tuple. No caller migration or
compatibility path is needed; remove the hand-maintained normalized set.
This is a private definition consolidation, with no separate architectural choice.
Preserve #1681's generic serial substring predicate and attribute handling.
Further detection changes belong to #1681, XML/closing rules to #1678–#1680,
and MAC/IP rules to #1683–#1684; the operator approved these exclusions on 2026-10-16.

### Failure model

Offline export operators rely on unchanged scrub/backstop expressions and fixtures.
A dropped name or altered alternation may disclose identifiers or change captures.
Compare every compiled module pattern, scrub/backstop pattern and normalized set
against the immediate base. Inject a new suffixed name into the tuple to prove
both consumers derive it. Existing exporter tests cover current behavior;
future changes to serial recognition are owned by #1681 and excluded here.

## Success

Issue #1682's three criteria hold: one editable name list feeds both consumers;
compiled patterns and normalized set remain exact; committed fixtures remain bytes
identical. A newly added suffixed name reaches both derived consumers in a test.

## Validation

- focused-test: `tests/scripts/test_live_capture_export.py::test_serial_names_share_one_source`
  injects a new tuple entry; red before the tuple exists, green when both consumers
  derive it. Command: `uv run --no-sync pytest tests/scripts/test_live_capture_export.py -q --no-cov`.
- focused-test: capture base compiled patterns/key set and fixture hashes privately;
  compare after implementation; run the existing offline exporter integration cases.
- task-test-not-applicable: this prose specification has no executable consumer;
  its content is reviewed against #1682 rather than asserted in tests.
- Final checks: `just verify` and `uv run --no-sync prek run --all-files` before push.
