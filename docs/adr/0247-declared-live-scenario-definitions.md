# ADR 0247: Declared live scenario definitions

## Status
Accepted (2026-10-19), issue #1092.

## Context
Every live scenario is an `async def` reached from the runner's numbered `SUBTASKS`
registry, with its operation, arguments and expectation authored inline. A generator
(#1093) needs a declared format to emit into, and the scenario gap report (#1091) can
only infer what a function exercises. `record_verified` already requires a closed-shape
scenario ID (`observation.SCENARIO_ID`, `st<N>-...`) and the ledger an operation ID
(`observation.OPERATION_ID`).

## Decision
Scenario definitions are data in `scripts/live_test/scenario_definitions.json`, loaded and
validated by `scripts/live_test/definitions.py`. The document is
`{"format_version": 1, "definitions": [...]}`; each definition has exactly these fields:

- `scenario` — matches `SCENARIO_ID` and equals `st100-<slug>`, where `<slug>` is the
  operation ID with `.` and `_` written as `-`. The ID is a pure function of the
  operation, so the registry holds at most one definition per operation and a generator
  re-emits the same ID. `100` is a namespace marker, not a result row: no hand-written
  scenario or `SUBTASKS` key uses it, and an executor must not record it as one.
- `operation` — matches `OPERATION_ID` and names a registered tool's operation.
- `class` — `read-only` or `mutating`; it must equal the tool's registered effect (`read`
  is `read-only`, every other effect is `mutating`).
- `arguments` — tool argument name to a JSON literal, or to the placeholder
  `{"target": "<kind>"}`. An argument that is one of the tool's top-level target
  selectors (`ToolSecurity.targets`) must be a placeholder of that selector's kind, and a
  placeholder is accepted nowhere else. The tool's connection argument and the container
  of a nested selector may not be supplied. So no target identity or connection is
  committed; binding a placeholder is a future executor's concern.
- `expected` — `success`. Other outcomes need a format version bump.

The loader rejects a malformed document as a whole. The gap report reads the registry: it
checks each definition's arguments against the tool's served input schema, reporting a
disagreement as `definition-mismatch:`, a definition whose operation the ledger lacks as
`unregistered:`, and a load failure as `unreadable:`, all failing `--fail-on-dispatch`;
and it counts defined ledger operations. A definition does not count as exercised.

Nothing executes definitions yet. Any later executor must run a `mutating` definition only
on an explicit, per-run operator selection, never by default or as part of a group.

## Consequences
The hand-written `SUBTASKS` registry is unchanged and coexists; no scenario migrates.
The empty registry keeps `just scenario-gap --fail-on-dispatch` green. Definitions record
no maturity and promote nothing.

## Considered & rejected
- **Python definitions in a module.** judgment: a generator would have to emit and format
  code; JSON is emitted and diffed deterministically.
- **Reuse ADR 0241's "no registry" judgment.** judgment: 0241 rejected a layer between
  existing validation and evidence owners; this is the declared registry #1092 requires,
  owned by one module with no service API.
- **Free choice of `st<N>`.** judgment: hand-written numbers follow result rows (0-43); a
  free number invites collision and makes a generated ID depend on allocation order.
- **Several definitions per operation (`st100-<slug>-<variant>`).** verified: `-` cannot
  separate a variant from a slug; `lpar.get` + `state` and `lpar.get_state` give the same
  ID, as do 25 slug-prefix pairs among the 165 operations. #1093 needs one per operation.
- **A stored `runnable` flag for mutating definitions.** judgment: a committed flag could
  enable a mutating run by default; the opt-in belongs to the run, not the data.
- **Literal target identities.** judgment: they would commit lab names to the repository
  and tie a definition to one lab.
- **Placeholders keyed on `REQUIRED_TARGET_ARGUMENTS`.** verified: five registered
  selectors (`user_profile_uuid` on three user tools, `user_id`, `vios_name`) are outside
  that table; the tool's own selectors are the authority.
