# CLI cleanup journey proof

## Problem

Issue #1704 requires composed offline evidence after #1695's CLI changes.
Per-command tests do not prove identifiers survive the complete operator journey.

## Scope

One Cast unit extends `tests/app/test_cli_e2e.py` and its existing HTTPS fixture,
using the installed `hmcpctl` entry point with real subprocess stdin/stdout.
The fixture serves synthetic REST responses and records mutations; operations,
selectors, authorization and rendering remain real. Reuse established XML shapes.
Carry parsed identifiers between inventory, scoped inspection, dry-run,
confirmation and explicit backing-file cleanup; retain unrelated fixture objects.
Document the same recipe and exact old-to-new syntax in `docs/cli.md`, linking
README installation and `docs/live-testing.md` rather than duplicating them.
No ownership transition, production edits, new runner, framework or dependency.
Optional reserved ADR 0236 remains unused: existing transport architecture suffices.
Expected implementation size: 350–650 changed lines (M), derived from stateful
fixture routes, subprocess journey assertions and the operator recipe.

### Failure model

Account for wrong system, duplicate names, unknown mapping identity, mapped disk,
mounted media, mutation refusal without consent, and partial operation failure.
Test subprocesses have bounded timeouts and isolated configuration; server and
children must close. Synthetic results establish offline composition only;
real HMC behavior remains unproven without a separately authorized live window.

## Success

Help discovers the groups; parsed systems/LPARs/VIOS/VG identities feed later
commands. Scoped inspection and dry-run do not mutate. Unknown mappings remain
visible in unfiltered inspection and block unsafe progression. Confirmation
executes decommission while retaining backing storage; explicit deletes remove
only the selected disk and ISO, preserving unrelated resources. Wrong-system,
duplicate-name, mapped/mounted and partial-failure cases cannot falsely succeed.
Partial failures retain earlier effects, report failed/skipped phases, and issue
no later destructive requests. JSON parses without prose; redirected identifiers survive narrow widths, and
human mutation output excludes unrelated hardware payloads and stays bounded.
The recipe explains retained storage, uncertainty, non-TTY consent and migration.

## Validation

- Mode: focused-test; `tests/app/test_cli_e2e.py` cleanup journey cases cover
  composition, identity transfer, safety refusals, partial failure and output.
  Controlled fixture faults (wrong identity, unexpected retained/deleted object,
  missing refusal or corrupt JSON) must make the corresponding assertions red;
  restore each fault and run `uv run --no-sync pytest tests/app/test_cli_e2e.py --no-cov -q` green.
- Mode: task-test-not-applicable; human-readable recipe/migration prose has no
  executable consumer; validate its command sequence against the journey and
  installed help, without snapshotting prose.
- Run `just lint`, `just format-check`, final `just verify` and
  `uv run --no-sync prek run --all-files` before push. Report offline arms and
  unrun live hardware evidence separately; no live command is authorized.
