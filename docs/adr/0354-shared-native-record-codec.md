# 0354 — Own native-record construction below SSH adapters

## Status

Accepted (2026-11-10), internal shared primitive relocation.

## Context

Document validation imports the attribute encoder from SSH commands. That module
also parses responses; attribute and filter encoders share one grammar, validator
and secret handling.

## Decision

Move both encoders and their shared validation and table into
`documents/native_records.py`. Direct callers import that owner, parsers stay in
SSH, and no re-exports remain. Retain the exact existing grammar rules. Two existing
source witnesses add the new owner while retaining genuine parser coverage.

## Consequences

Documents no longer import SSH for pure construction. Private paths and
fingerprints change; old source captures remain stale. No format, version or
support claim changes.

## Considered & rejected

- Keep the SSH-owned shared encoder. Judgment: it inverts the request-construction boundary.
- Move only the attribute encoder. Verified: both encoders call `_validated_value`
  and use the same delimiter table. Moving only one creates reverse shared-helper
  imports or duplicates validation instead of establishing one coherent owner.
- Add a codec framework. Judgment: the existing concrete functions already suffice.
