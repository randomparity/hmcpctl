# Literal backslash-u identifier guard (#1684)

## Problem

Raw `\u` hides WWNs and separated MACs from scrub/leak checks (#1684).
Authority: `q1684-e82330a7`, parent #1558, human continuations 2026-10-16/17.

## Scope

Guard survivors at the end of each existing `Tokenizer.tokenize` call, before
separate REST/SSH fields or tool JSON are combined or decoded. Reuse `_WWN_16`,
`_MAC_FORMS` and the exact MAC scrub matcher; exempt only its value-group spans
in that same representation. Merge ordered spans/matches once; raise existing
`LeakError` on a survivor. Keep legacy flat leak checks unchanged.
Check retained capture metadata before combining scrubbed fields; share the
derived writer's actual-string/key guard. Unscrubbed metadata has no MAC exemption.
Files: `scripts/live_capture_export.py`, its test module, this unnumbered spec.
Private `json_encoded=True` is set only at actual `json.dumps` producers or
explicit direct JSON callers; raw/default calls and derived metadata stay guarded.
Approved exclusions (campaign chat 2026-10-16): closing tags #1680, serial-key
consolidation #1682, live/release validation #1506, IPv4 escapes #1683.

### Failure model

- Actors/deployments: local operators export untrusted offline capture strings
  and supply derived metadata; CI uses synthetic/committed fixtures.
- Invariants/assets: scoped identifiers cannot silently enter published outputs;
  genuine JSON stays valid; committed fixture exports remain unchanged.
- Accepted failures: ambiguous surviving raw values may refuse before writing
  (#1684); existing LUN/wholly zeroed-MAC exemptions remain accepted.
- Covered elsewhere: closing tags #1680; serial consolidation #1682;
  IPv4 escapes #1683; live/release checks #1506.
- Boundary inventory: capture strings enter scrubbing; survivors are checked
  before aggregation/dequoting; retained metadata and derived data before writing.
- Actor/trust model: producers control strings/metadata; refuse survivors before publication.
- Control per boundary: odd JSON slash runs retain genuine Unicode; even runs
  remain literal. Only actual scrub-call MAC value spans retain exemptions.
  An unclosed stdout opener cannot own stderr/another record; a decoded Unicode
  opener cannot claim ownership absent from the tool's encoded scrub input.
  Attributes, unmatched tags, CDATA and child-separated text remain checked.
  Retained headers/provenance cannot bypass the shared raw metadata guard.
  Fixed lookbehind, guarded runs, ordered spans and iterative metadata traversal
  stay linear; use existing serialization limits, with no new nesting policy.
- Threat exclusions: independently owned classes above; live hosts are unreachable offline.
## Success

Scoped raw literal-u identifiers never export silently (#1684); genuine
`json.dumps` escapes retain current handling and valid JSON. All committed
fixtures remain byte-identical and acquire no new refusal.

## Validation

- focused-test: raw spans, record boundaries, retained headers/provenance/keys,
  Unicode and direct JSON in the existing test module; red:
  raw exports succeed or genuine Unicode refuses; green:
  `uv run --no-sync pytest tests/scripts/test_live_capture_export.py --no-cov -q`.
- focused-test: existing JSON, zeroed/LUN, long-run and fixture cases; compare
  all 119 source bytes, capture re-exports and derived writes against the base.
- task-test-not-applicable: prose has no executable consumer; review its mapping
  and size. CLI: actual producer headers/raw refusal leave no output; JSON stays valid.
