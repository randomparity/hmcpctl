# Concrete vNIC experiment lifecycles

## Authority and outcome

The original quality request authorizes large internal refactors. This implements
ADR 0312's accepted mode boundaries inside the owner retained by ADR 0347; it
changes no public interface, data model, module boundary or remote authority.
[ADR 0366](../../adr/0366-coalesce-concrete-vnic-experiment-lifecycles.md) records
the implementation choice. One coupled Cast unit covers all three lifecycles.

## Components and data flow

Group shared accounting/observations, creation, update-source, retained-fixture,
and entry points in the existing file. Retain unchanged mode helpers and constants.
Creation owns _creation_run, source _source_run, fixture its existing _fixture_run.
Each calls its concrete validator before the existing common client/candidate
_context. Keep validate_packet's union admission and public preflight/capture/
recover/main/wrapper signatures and behavior. Mode validators enforce exact versions.
Source retains its existing creation-base field validation and finite-case rules.

_Budget keeps common counters, deadlines, single allowance and creation defaults.
_SourceBudget owns SOURCE_LIMITS, the fifteen-second reserve, suffix checks,
five fresh removal observations and effect-command journaling. Move the existing
source guards without changing their order relative to allowance spending,
intent persistence, expiry recheck or physical dispatch. _FixtureBudget and its
separate guard retain signature/order/settlement policy. _bounded uses the selected
budget's reserve rather than inspecting packet mode. Migrate four direct source
budget test constructions; creation and fixture callers retain their known policies.

Source snapshots pass expected_lpar_state explicitly to shared snapshot; creation
has no state expectation. Preserve the state refusal before native reads and
selected_after/policy evidence before later backing reads can fail. Source capture,
source stage and fixture source-history admission use the explicit source snapshot.
Retain source_result's existing observation behavior and common reconciliation.

The two concrete stage functions retain the original awaits, catches, writes and
closure checks. Creation alone owns optional_policy_verified; source owns its
version/purpose/evidence/case/session fields and source history. Fixture keeps its
prepare/cleanup and independent recovery policies. At CLI entry, one branch picks
the concrete validator, runner, label and accepted statuses; there is no registry,
new DTO, callback pipeline, compatibility shim or boolean-mode execution driver.

## Success and failure model

For conforming version1/2/3 packets and histories in the current deployments,
public results, errors, status/exit decisions and remote traces remain the same.
Malformed nested fixture packet versions are refused as ADR 0312 requires; the
old direct validator accidentally admitted them after a recomputed digest.
Budgets, transport and concrete execution no longer call mode classifiers.

- Actors/deployments: the existing named CLI/Python arm entry points and private,
  stable invocation packets under the coordinated exclusive writer deployment;
  synthetic HTTP/SSH tests exercising real parsers and procedure entry points.
- Invariants/assets: exact mode admission, bounded effects, owned identity,
  source partial observations, intent-before-effect, logout reserves and truthful
  private histories. Refusal/uncertainty never gains cleanup authority.
- Accepted classes: existing ambiguous remote outcomes and interrupted evidence
  retain manual recovery. No retry, transactional rollback, mutable-packet
  concurrency isolation, traffic/failover qualification or new hardware proof.
- Covered elsewhere: transport TLS, ownership validators, private-file custody,
  native argument encoding and operator hardware admission keep their owners.

## Validation

Use the current 1,132-test baseline across the arm, wrapper, preflight, recovery
and I-record modules. Add a meaningful structural boundary gate, strict concrete
version cases, a public wrong-version prepared-history refusal with no added
effects, and real CLI preflight/capture/recover for all three modes. Preserve
existing effect/latency/count/history assertions and direct fake boundaries.
Compare unchanged helper ASTs, folded creation/source policy and actual baseline
versus refactored packet outcomes and effect traces. Fault source reserves,
partial-evidence timing and CLI labels; require intended failures and exact restore.
Run affected suites, static checks and pinned hooks, inspect and commit the full
migration. Final exact-HEAD whole-branch verification/review and score refresh remain.
