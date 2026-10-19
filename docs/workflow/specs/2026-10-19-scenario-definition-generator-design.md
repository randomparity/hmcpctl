# Scenario definition generator (#1093)

## Problem
`scripts/live_test/scenario_definitions.json` (ADR 0247) is empty, and every operation a
single targeted call exercises would otherwise be hand-written by a #620 child.

## Scope
Decision: [ADR 0249](../../adr/0249-scenario-definitions-derived-from-the-ledger.md).

- `scripts/gen_scenario_definitions.py` (new): `derive(operation, tool, security, schema)`
  returns one ADR 0247 definition dict or a hand-written reason; `generate(operations,
  security, schemas)` returns the document and the `hand-written:` lines, validating the
  document with `definitions.load_definitions`; `main(argv)` composes schemas offline,
  writes `--output` (default `definitions.DEFINITIONS_PATH`) as `json.dumps(indent=2)`
  plus a newline, and prints the hand-written lines and one `summary:` line.
- `scripts/live_test/scenario_definitions.json`: regenerated content.
- `tests/scripts/test_gen_scenario_definitions.py` (new), `tests/test_live_scenario_definitions.py`
  (the committed registry is no longer empty).
- `justfile`: `scenario-definitions` regenerates; not a `static` member.
- `docs/capabilities/README.md`: the generator and its rule, in the scenario paragraph.

No change to `definitions.py`, the gap report, the runner, `SUBTASKS`, or maturity.

## Failure model
1. Actors and deployments
   - an operator or agent running the generator in a worktree; CI legs running the suite.
2. Invariants and assets at stake
   - no definition commits a lab identity or connection (placeholders only);
   - no mutating definition claims success for a defaulted call that may not act;
   - the committed registry equals a fresh generation;
   - nothing is executed.
3. Accepted failure classes
   - a derived read may fail live for a lab reason (absent target): bounded, a definition
     is not evidence and counts as nothing (ADR 0247);
   - a derived mutating call may still fail live: bounded, it needs a per-run opt-in.
4. Covered elsewhere
   - executing definitions and mutating opt-in: future executor, ADR 0247;
   - argument names and types against served schemas: `just scenario-gap` (#1094);
   - maturity: #620 children / #622.

## Success
1. Running the generator writes a registry `definitions.read_definitions()` accepts, holding
   exactly the operations ADR 0249's rule derives, sorted by operation.
2. Each ledger operation with no definition prints one `hand-written:` line with its reason;
   `lpar.plan` and `provision.lpar` print the nested-container reason.
3. For mutating operations, an argument outside the target selectors, connection argument
   and `ownership_override` yields hand-written.
4. A stale committed registry fails `just test`; `just scenario-gap` stays green.

## Validation
- S1 Mode: focused-test — `test_committed_registry_is_fresh` (red: committed file empty).
- S2 Mode: focused-test — synthetic cases for non-target, container and unregistered
  reasons; `test_nested_container_operations_are_hand_written` over the live registry.
- S3 Mode: focused-test — synthetic mutating cases, targets and `ownership_override` only vs `wait`.
- S4 Mode: focused-test — S1's test; `just scenario-gap` exit 0.
- `main` writing `--output` and printing the summary: focused-test `test_main_writes_output`.
