# ADR 0381: Secret-scan filter for evidence hex lines

## Status

Accepted (2026-11-13), under the operator-approved #1970 charter.

## Context

ADR 0002 has secret scanning check tracked content against a reviewed
`.secrets.baseline`, and it treats every baseline entry as a security-sensitive
bypass. `docs/capabilities/maturity.json` records each live observation's
`tested_commit` (40-hex Git SHA-1) and `closure_fingerprint` (64-hex SHA-256),
and the `HexHighEntropyString` detector flags them. Before this change they made
up 70 of the baseline's results.

So every promotion that adds evidence makes the `secrets` hook fail. A new hex
value is a new finding. Inserting an observation also moves the line numbers of
findings already in the baseline, and `detect-secrets-hook` 1.5.0 then rewrites
the baseline and exits 3 (`should_update_baseline`). Its rewrite rebuilds
`filters_used` from runtime settings. The v0.2.0 window had to repair the
promotion commit by hand (#1970).

## Decision

The baseline's `filters_used` gains one
`detect_secrets.filters.regex.should_exclude_line` entry with two patterns:

- `^\s*"tested_commit": "[0-9a-f]{40}",?$`
- `^\s*"closure_fingerprint": "[0-9a-f]{64}",?$`

Each pattern matches only a whole line with exactly that key and a
lowercase hex value of exactly that length, plus an optional trailing comma.
Nothing else can appear on a matching line. The `maturity.json` results leave the
baseline, and that file leaves `BASELINED_FINDINGS`.
`tests/test_ci_pipeline.py` pins the full filter list and these exact patterns,
and runs a controlled fault: a credential-shaped value on any other key, line
shape or length is still reported. The promotion script is unchanged.
Design: [the #1970 specification](../workflow/specs/2026-11-13-promotion-secret-scan-design.md).

## Consequences

A promotion commit now passes the `secrets` hook and CI with no baseline or pin
edits. The filter applies to the whole repository, because detect-secrets has no
per-file line filter. Any tracked line of exactly one of those two shapes goes
unscanned. A secret can hide there only if it is a lowercase hex string of that
exact length under one of those two keys. That is the accepted residual. Widening a
pattern is a reviewed test change, never a silent rescan.

## Considered & rejected

- **Promotion regenerates the baseline and bumps the pin (issue's option A).**
  verified: `detect-secrets-hook` 1.5.0 `pre_commit_hook.should_update_baseline`
  returns modified when a baselined line number moves, so `main` exits 3 after
  rewriting the file (source read at `.venv/.../detect_secrets/pre_commit_hook.py`,
  detect-secrets 1.5.0). Promotion would have to re-scan, restore `filters_used`,
  edit a Python test and widen its commit allowlist every release.
- **Inline `pragma: allowlist secret` on each line.** judgment: it changes the
  JSON format of `maturity.json`, and reworking that format is an approved
  exclusion.
- **Exclude `maturity.json` from scanning (`exclude.files`).** judgment: it
  hides every key in the file, which is a far wider bypass than two fixed shapes.
- **A repository plugin filter scoped to one file (`file://…::fn`).** judgment:
  the scanner would load executable code from the repository just to narrow a
  shape that the anchored patterns already bound.
- **Do nothing.** verified: the v0.2.0 promotion commit failed the hook, and #1968
  carried a hand-built baseline and pin edit.
