# Descriptive preflight verdict catalog

## Problem
Nineteen preflight functions perform no admission checks and repeat ArmVerdict
construction plus a separate registration entry. Actual admission functions
must remain distinct from descriptions.

## Scope
Replace only the nineteen functions whose body is a single ArmVerdict return,
with literal group and True readiness, with one private description catalog.
Store existing ArmVerdict records as templates; preserve reason and ordered
mutation strings, and use a system-name placeholder only where supplied today.
Keep None system names for affinity-group and users. No new record type.
Convert only source f-string attribute interpolation to equivalent format fields;
escape literal braces and retain exact strings. Runtime config/module attributes
stay runtime values and inserted user text is not recursively formatted.
One constructor formats mutation descriptions and the optional system-name field,
returns a fresh record, and is registered by deriving keys from the catalog.
Keep the dynamic admission functions, native-boot partials and generic fallback.
Selected group order remains owned by runner.SUBTASK_GROUPS, not mapping order.
No compatibility wrappers for the removed private descriptive functions.

### Failure model
- Actors and deployments: local operators running preflight, offline CI callers.
- Invariants and assets: identical readiness, reason, mutation text/order and
  managed-system association; actual admission remains independently enforced.
- Accepted failure classes: no new hardware execution proof for this offline
  presentation refactor; predictions remain distinct from run-time admission.
- Covered elsewhere: config validation/credential bootstrap and live arm checks
  remain owned by their existing functions and runner.

## Success
Nineteen descriptions have one construction/registration path, with no changed
preflight output, group selection, credentials, HMC calls or eligibility policy.

## Validation
- task-test-not-applicable: this is a pure representation change of existing
  descriptions, with no new executable/structural contract; no private-name tests.
- Capture complete ArmVerdict dataclasses before/after for selected groups and
  all selection, with default and altered configs (braces, Unicode, control chars)
  and altered module constants. Compare values and field defaults exactly.
- Audit removal set, unchanged dynamic functions and original placeholders.
- Run existing tests/scripts/test_live_test_preflight.py and bounded runner tests
  with --no-cov; normal hooks/static and final assembled full verification.
