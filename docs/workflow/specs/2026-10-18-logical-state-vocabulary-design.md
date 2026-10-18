# Enforce the existing logical-operation vocabulary

## Problem
Records declare closed Literal state, outcome and effect-status types, but writers
accept unrestricted strings and row decoding trusts SQLite text. Invalid writes
or corrupt rows can therefore return values outside the declared record contract.
The operator approved enforcing the existing vocabulary without changing the schema.

## Scope
Use existing OperationState, OperationOutcome and EffectStatus aliases in store
set_state/record_outcome/accepted_continuations and engine _end/_settle paths.
Validate state, nullable outcome and effect status before writes, deriving allowed
values from the existing aliases with typing.get_args. Invalid caller values raise
OperationRefused with a specific invalid-vocabulary reason before SQL mutation.
Validate state/outcome in the common operation-row read boundary used by get/find,
and effect status in effect decoding. Invalid persisted values use the existing
store_corrupt refusal and recovery guidance. Cast only after runtime validation
when constructing Literal-typed records. Never echo an invalid value in diagnostics.
Retain all valid values including None outcome. Do not impose new state/outcome
pairing rules, migrate/repair/delete data, change schemas or authorization, or add
another state model. Query filters retain their current input behavior. Root owns scope.

## Success
Every valid alias value roundtrips unchanged. Invalid writes leave rows and events
untouched; invalid persisted fields cannot become typed records or start effect work.
The engine's existing replay, continuation, guard and failure behavior remains.

## Failure model
Actors: typed callers, untyped Python callers and local persisted SQLite rows.
Assets: truthful result types and no effect execution from corrupted control state. Invalid
calls refuse before writes; corrupt reads fail closed with existing store recovery
instructions. Accepted behavior change: previously accepted invalid vocabulary is
refused. Existing data within the declared vocabulary remains readable.

## Validation
focused-test: invalid state/outcome/status writes and raw-SQL corrupted row reads
must fail before implementation; assert no row/event changes and no invalid value
in diagnostics. Roundtrip every existing alias member, including None outcome.
Exercise row readers, status pages and engine continuation before effect execution.
Run logical store/engine, power and MCP adapters, lint, format, typecheck and smoke.
No hardware operation or schema migration is required or claimed.
