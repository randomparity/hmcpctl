# Live runner validation and evidence owners
Full-spec M denominator250, two behavior-preserving owner extractions with many
existing direct test callers; no new execution policy. Estimate800–1400 changed
lines from moving roughly400 lines plus imports/caller bindings and tests.

## Charter
Interaction: interactive. Scope identity: current live_runner_combines_execution_source_analysis_and_publication.
Outcome: runner coordinates execution; focused modules own source validation and evidence.
Completion: C1–C5. Provenance: user Approved explicit scenario_validation.py/evidence.py
moves, shared atomic writer, direct callers/no aliases, preserved saved data/safety/order;
then finish current four and stop short of92. No further score-driven cleanup.
Exclusions/owners: RunState, scenario registry/SUBTASKS/_SCENARIO_MODULES and executable
lifecycle remain runner; result vocabulary/grading remain observation.py; saved config/
artifact models remain config.py. No new registry, model, framework, dependency or live run.
Surface: runner.py, two new live_test library modules, their direct source/test consumers.
Ambiguities: environment/gap admission helpers feed evidence publication and move with it;
existing schema grammar remains owned by check_capability_inventory, not copied.

## Success
C1: scenario_validation.py owns current _validate_expected_dispatch, _declared_names,
_result_names, _call_result_names, _validate_declared_function, _validate_declared_outcomes.
Public cross-module entry names validate_expected_dispatch(tool, expected) and
validate_declared_outcomes(modules) make the registered module collection explicit;
private AST helper names may stay private within new owner. Registry TOOL_SECURITY and
ExpectedOutcome stay existing owners, imported normally. No runner runtime import.
C2: evidence.py owns current _write_results, _read_environment, _git, _repository_root,
_run_provenance, _destination_is_ignored, _observations_path, _tree_is_clean,
_emit_observations, _load_known_gaps and associated ENVIRONMENT_KEYS/RUN_HMC_RELEASE.
Expose shared entry names without leading underscore; retain _git/_tree_is_clean private
inside owner. No independent function copy or compatibility re-export remains in runner.
TYPE_CHECKING-only RunState annotation import is allowed; no runtime cycle/new Protocol.
C3: runner uses direct module-qualified calls. RunState.call validation still precedes
client/hardware dispatch; main validates scenario source before repository/catalog reads
and client entry. _SCENARIO_MODULES remains derived from executable SUBTASKS in runner,
passed into validator; existing check_capability_inventory/promoter registry consumers
still use that owning registry, not a duplicated registry in validation module.
C4: preserve exact policy and data: literal declared expected-outcome source rules,
operation matching, gap fingerprint/environment eligibility, ignored output gate, dirty
src/scripts gate, HEAD/provenance fields, partial/failure persistence, duplicate-ID
refusal, unknown handler behavior, redaction/grammar checks, atomic report replacement,
print messages/return booleans and execution/interruption/client cleanup order.
No data/schema migration or new accepted error behavior. Generic report writer used by
runner and evidence stays single-owner in evidence with same atomic cleanup semantics.
C5: migrate every direct import/call/monkeypatch to actual owner. Preserve original
assertions, including tests/test_live_runner.py, tests/scripts/test_promote_live_observations.py,
tests/test_live_lpar_config_arm.py and discovered additional callers. Historical decision
prose remains historical; no aliases to keep old tests passing.

## Failure model
Actors: operator startup, malformed scenario source, forged/mismatched expected outcomes,
invalid environment tokens, dirty/unignored destinations, interrupted writes/runs.
Assets: no hardware before validation, truthful evidence/fingerprints, private untracked
outputs and atomic recoverable reports. Accepted new failure classes: none.
Existing external hardware limits remain; no live execution authorized.

## Validation
C1/C2 pure relocation Mode task-test-not-applicable for new behavior: moved function
bodies retain AST semantics after explicit owner name/collection bindings; no invented
behavioral RED for relocation. Compare canonical AST bodies with only documented
module/name/argument binding transformations allowed; all other differences unresolved.
C3/C5 Mode focused-test: meaningful module-boundary import/AST ownership check requires
new owners, no obsolete runner defs/reexports/cycles, exact registry collection passed.
Old layout fails before move; controlled old runner import or source-validation bypass
must fail boundary/startup test; restore before regression. Avoid prose snapshots.
C4 Mode focused-test: retain existing invalid-source before client, publication dirty/
unignored/noenv/invalidenv/duplicate/unknownhandler/fingerprint and partial-write failure
behavior tests; actual temporary Git repos and atomic filesystem writes remain real.
Run affected runner/promoter/config-arm/entrypoint tests --no-cov; existing invocation
import/--help proof without credentials/hardware; lint/format/type/all14 actual hooks
and normal commit hooks. Root final current-four review/fullverify and scan, then stop.
