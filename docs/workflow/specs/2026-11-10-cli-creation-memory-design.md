# Shared CLI creation-memory conversion

## Problem
LPAR and VIOS creation repeat the same eight-field conversion and error boundary.

## Scope
Move the exact conversion into `load_creation_memory` in the existing
`cli_commands/lpar/assignment_input.py`, already imported by both callers.
Use eight explicit keyword-only parameters with the existing raw input types.
Return the existing LparCreationMemory; retain exact auto/none case, int/Decimal
conversion, validation, terminal escaping and BadParameter chaining/param_hint.
Both commands call the helper in place before serial and layout validation.
Remove their obsolete conversion blocks and now-unused imports; retain option
signatures, unrelated TypeAdapter uses, and LPAR layout Decimal conversion.
No compatibility aliases, new module, schema or option is needed.

### Failure model
- Actors and deployments: local CLI operators, direct Python callers and CI.
- Invariants and assets: exact accepted typed values and refusal before client
  entry or confirmation; retained diagnostic escaping and admission order.
- Accepted failure classes: no new hardware proof; this moves offline parsing.
- Covered elsewhere: Pydantic creation-memory validators and existing operation
  admission remain their current owners.
- Threat boundary: operator-controlled strings retain existing conversion,
  strict model validation and terminal_field(err=True); no boundary is widened.

## Success
The two commands share one conversion owner and preserve their eight input
bindings, Typer signatures and validation/error behavior. No other policy moves.

## Validation
- task-test-not-applicable: pure relocation introduces no new input/result or
  structural contract; do not add tests of the helper name or duplicate logic.
- Existing tests/app/test_app_creation_memory.py exercises actual commands,
  defaults, exact fields, token/Decimal refusal and refusal before client entry.
- Compare original blocks by AST and retain raw source when extracting. Compare
  helper against original compiled block over omitted/valid/invalid/token values,
  including exact error type/message/param_hint/cause/suppression.
- Run affected CLI creation, terminal-control and help tests with --no-cov, lint,
  types, formatting and normal hooks. Final assembled full verification is owed.
