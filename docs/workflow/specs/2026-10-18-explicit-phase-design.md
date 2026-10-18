# Explicit persistent phase transitions
Full-spec S denominator100; operator-approved method rename scope2.

## Scope
Rename OperationContext.phase(name) to set_phase(name) in logical/engine.py;
update the three power-body calls and logical-engine test helper directly.
No alias, record-field/value rename, engine behavior or stored data change.
No new architecture decision beyond spelling the existing mutator explicitly.

## Success and failure model
C1: context.set_phase is the supported existing mutator; no context.phase alias.
C2: body AST unchanged: duplicate phase returns, persistence precedes cache update,
existing transaction/event/audit/recovery behavior retained.
Actors: existing logical workers, power workflows and offline tests. Assets:
persisted phase and event ordering/cached state. Accepted classes: no live HMC
proof for pure naming change. Other owners: store schema/validation/auth unchanged.

## Verification
Focused-test: context method boundary assertion fails old tree, existing engine
body migrated to set_phase and phase journal behavior exercised by actual worker.
Assert repeated same phase produces no duplicate phase event if not already
covered. Preserve phase vocabulary and data. Controlled old-name restoration
must fail boundary, restore. Run logical engine/store and actual logical power
tests found by references; --no-cov, lint/format/types, actual14commit hooks.
Root final fullverify and pinned all-files hooks after three tasks.
