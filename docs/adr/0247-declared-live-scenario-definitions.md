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

- `scenario` — matches `SCENARIO_ID` and is `st100-<slug>` or `st100-<slug>-<variant>`,
  where `<slug>` is the operation ID with `.` and `_` written as `-`. Every definition uses
  the reserved number 100, which no hand-written scenario uses, so a generated ID is a pure
  function of operation and variant and cannot collide with one. IDs are unique.
- `operation` — matches `OPERATION_ID` and names a registered tool's operation.
- `class` — `read-only` or `mutating`; it must equal the tool's registered effect (`read`
  is `read-only`, every other effect is `mutating`).
- `arguments` — tool argument name to a JSON literal, or to the placeholder
  `{"target": "<kind>"}` naming a target kind other than `none`. An argument listed in
  `REQUIRED_TARGET_ARGUMENTS` must be a placeholder of its mapped kind, so no lab identity
  is committed; a run binds placeholders from operator configuration.
- `expected` — `success`. Other outcomes need a format version bump.

The loader rejects a malformed document as a whole. The gap report reads the registry: it
checks each definition's arguments against the tool's served input schema, reporting a
disagreement as `definition-mismatch:` and a load failure as `unreadable:`, both failing
`--fail-on-dispatch`; and it counts defined operations. A definition does not count as
exercised.

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
- **A stored `runnable` flag for mutating definitions.** judgment: a committed flag could
  enable a mutating run by default; the opt-in belongs to the run, not the data.
- **Literal target identities.** judgment: they would commit lab names to the repository
  and tie a definition to one lab.
