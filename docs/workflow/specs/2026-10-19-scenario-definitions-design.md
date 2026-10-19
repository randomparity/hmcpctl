# Declared scenario definitions (#1092)

## Problem

Live scenarios exist only as hand-written functions in `SUBTASKS`
(`scripts/live_test/runner.py`). The generator in #1093 needs a declared format to emit,
and the gap report (#1091) needs to read it. ADR 0247 records the format.

## Scope

New `scripts/live_test/definitions.py` (loader and validator),
`scripts/live_test/scenario_definitions.json` (empty registry), the gap report's reading
of it, tests, ADR 0247 and a `docs/capabilities/README.md` pointer. No generator, executor,
runner change, scenario migration, maturity record or live run (#1093, #620, operator).

- **Loader**: `load_definitions(document, security) -> tuple[ScenarioDefinition, ...]`
  validates a parsed document against `security` (tool name to `ToolSecurity`, normally
  `TOOL_SECURITY`); `read_definitions(path=DEFINITIONS_PATH, security=TOOL_SECURITY)`
  reads the file and calls it. `ScenarioDefinition` is a frozen dataclass of `scenario`,
  `operation`, `tool`, `read_only`, `arguments` (a mapping in which a placeholder is a
  `Placeholder(kind)`) and `expected`. Any violation of ADR 0247 raises
  `DefinitionError` naming the definition index and the field: a non-object, missing or
  extra key, wrong type, unknown operation, class/effect disagreement, a scenario other
  than `st100-<slug>` or a duplicate, a literal for a top-level selector argument, a
  placeholder of the wrong kind or on a non-selector argument, a supplied connection or
  nested-selector container argument, or an unsupported `format_version` or `expected`.
- **Gap report**: `main` reads the registry; a `DefinitionError` or unreadable file
  becomes one `unreadable: <path> <message>` line. `build_report` takes the definitions;
  for each it calls the runner's `_dispatch_problems(tool, values, schemas)` with
  placeholders replaced by `UNRESOLVED_ARGUMENT`, and emits
  `definition-mismatch: <scenario> <problem>` per problem, added to the
  `--fail-on-dispatch` prefixes. A definition whose operation is not in `operations.json`
  is `unregistered: definition <scenario> operation <operation>` and is not checked
  further. The summary gains `N definitions` and `operations D/K defined`, where K is
  the ledger's operation count and D the distinct defined operations among them.

## Acceptance

1. The empty registry loads, and `just scenario-gap` stays green with an unchanged
   finding list except the summary counts.
2. Each ADR 0247 rule has a rejecting test, and a valid read-only and mutating definition
   with a placeholder and a literal load.
3. A definition whose arguments the served schema rejects yields `definition-mismatch:`
   and exit 1 under `--fail-on-dispatch`; a malformed registry yields `unreadable:`.
4. No hand-written scenario literal under `scripts/live_test/` uses `st100-`, `100` is not
   a `SUBTASKS` key, and the slug is injective over every registered operation.
5. Scanning `definitions.py` adds no dispatch, verified or unreadable entry to the report.

### Failure model

- Actors: an operator, agent or the #1093 generator writing the registry; CI reading it.
- Invariants: offline and read-only; a mutating definition is never executable by default
  (nothing executes definitions); no committed target-selector or connection argument
  carries a lab identity. Other literals are reviewed like any committed data.
- Accepted failure classes: the registry is validated against the composed tool registry,
  not against a lab, so a definition can be well-formed and still fail live.
- Covered elsewhere: execution and its operator opt-in (future); generation (#1093).
