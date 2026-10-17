# ADR 0228: Lossless CLI inventory output

## Status

Accepted for issue #1698.

## Context

Inventory tables are useful interactively but fold identifiers in redirected output.
The CLI's JSON switches and single-object defaults differ across inventory groups.

## Decision

The addressed inventory commands select table on terminal stdout and escaped TSV
otherwise; --format overrides this, with --json/-j an agreeing JSON alias. Reject
conflicting alias/format requests before I/O. Keep the existing JSON result schema.
Use fixed TSV columns, a header even when empty, backslash-N for missing/null,
and reversible backslash/tab/LF/CR escaping. Object output is Field/Value rows;
nested values are JSON cells. Document the exact projections in docs/cli.md.

Retain presentation in CLI output.py. Add ManagedSystem UUID/SystemName metadata
to fleet entries at read_fleet using the parent summary already in hand, preserving
the original entry and Resource data. No additional per-row discovery occurs.

## Consequences

Piped default output changes from Rich tables or JSON to TSV. Consumers requiring
the old JSON explicitly select --json; --no-json is removed in favor of --format.
Fleet JSON entries gain additive parent metadata; entries/unreadable_systems remain.
Explicit tables may fold; partial fleet diagnostics require retaining stderr.

## Considered & rejected

- A second compact JSON schema. verified: issue #1698 explicitly requires retaining
  existing JSON envelopes and choosing TSV for compact output.
- Extract TSV from Rich-rendered tables. judgment: rendered cells have already lost
  absent/empty distinctions and may fold; raw projections keep the contract explicit.
- Fetch each partition's parent while rendering. judgment: fleet discovery already
  owns the required parent identity, and extra requests add cost and failure paths.
