# Separate live PCIe scenario families

## Problem
One 3,000-line scenario module owns both configured-partition SR-IOV exercises
and dedicated-slot fixture lifecycles. The operator approved separating these
existing arms and migrating direct consumers while preserving their behavior.

## Scope
Move the SR-IOV section unchanged into live_test/sriov.py and the dedicated-slot
section unchanged into live_test/dedicated_pcie.py. Their current function,
constant, and dataclass definitions have no cross-section references. Keep the
existing dedicated inventory read scenarios with their dedicated-arm orchestrator,
including its SR-IOV inventory observations. Keep fixture and io_slots grammar
helpers with the dedicated lifecycle used by bare-cec, provisioning and LPAR arms.
Remove live_test/pcie.py and update direct imports, module references, test patch
seams and runner dispatch imports. No compatibility reexports or common framework.
Keep operation IDs such as pcie.assign_dedicated_slot unchanged; they identify
product capabilities, not Python modules. Module names in source diagnostics move.
Root owns this relocation. No environment admission, target selection, ownership,
cleanup, retry, polling, result, observation or dispatch-membership behavior changes.

## Success
Both arms remain registered and discoverable by live vocabulary/scenario/schema
gates. Every moved definition has the same AST. All consumers import the new
owner directly. The SR-IOV identifier guard still checks all 26 original call
arguments across both modules, rather than shrinking coverage after the split.

## Failure model
Actors are offline tooling and operator-run hardware scenarios, Python3.11–3.14.
Assets include existing partitions, owned fixtures, exact baselines and retained
recovery handles. All admission and guarded teardown checks stay unchanged.
Accepted failure: old internal module imports change under explicit approval.
Missing migrations fail imports or static dispatch checks. No hardware is invoked
or claimed as verification of this mechanical move.

## Validation
focused-test: existing PCIe, bare-cec, LPAR config/power, preflight/recovery,
observation promotion and runner offline behavior tests retain all safety cases.
Compare every top-level definition AST before/after. Run both modules through
existing workflow discovery and schema checks; preserve the 26-argument guard.
task-test-not-applicable: no new scenario behavior, so no duplicate lifecycle tests.
Run lint, format, typecheck, live-vocabulary and scenario-gap gates.
