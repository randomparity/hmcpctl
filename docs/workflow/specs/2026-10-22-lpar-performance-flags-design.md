# LPAR performance, priority and restart flags

## Problem and authority

Issue #1762, C01 of #638, binds five fields to partition creation and update
in the POWER10 and POWER11 command corpora (twenty occurrences).
The approved campaign starts against merged #1724/#1725/#1726; #637 retains
residual profile reconciliation. Scope token `q1762-3ed8f6e1`, exclusions empty.
[ADR 0254](../../adr/0254-lpar-performance-priority-flags.md) records transport choice.

## Contract

`LparSettings` holds optional `allow_perf_collection`, `lpar_avail_priority`,
`time_ref`, `simplified_remote_restart_capable` and `migration_disabled`.
Four switches accept strict booleans; priority accepts integer 0–255, excluding
Python bool. Omission preserves HMC defaults/current values; False and zero
are explicit. Unknown constructor keys fail. Empty updates fail before effects.
Simplified restart and migration-disable apply to AIX/Linux and IBM i, not VIOS.
The existing LogicalPartition create admits those two types only.

Source bindings: `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`,
`docs/refs/hmc-commands-p11/commands/mksyscfg.md:49`, and each corpus's
`commands/chsyscfg.md:49`. All five appear in each context. P10 describes a
performance alias; the public input exposes only the canonical spelling.
These sources establish grammar and partition applicability, not universal
firmware acceptance or an OS restart requirement. HMC admission remains final.
Do not claim that a successful setting proves migration/restart readiness or
that it changes guest runtime behavior. Readback proves partition configuration.

## Architecture and data flow

Keep the existing create workflow and owning authorization. Add settings to
`LparCreation` and the existing CLI/MCP create presenters. When requested and
keylock is absent, use typed `mksyscfg`, passing existing resource/max-slot values,
partition ID and the five supplied settings. Existing assignment prevalidation,
ownership stamping and optional apply remain. Do not mutate profile APIs.
When an explicit keylock requires REST, retain REST create and then configure
settings on the known created partition after creation ownership stamping;
report this as a separate workflow step. This preserves the existing keylock
contract without guessing a CLI keylock attribute. CLI fallback must refuse
an unrepresentable explicit keylock before creation rather than discard it.

`configure_lpar_settings(hmc, lpar, settings, *, system_name_or_uuid,
ownership_override=False)` uses `resolve_and_authorize_lpar_names` before SSH.
Add a single `configure` CLI command and `hmc_configure_lpar` MCP tool, plus a
read presenter using the same typed settings reader. No raw command input.
The transport builds an attribute record using `build_attribute_record`, quotes
shell words using `shlex.quote`, and sends `chsyscfg -r lpar` for one target.
Before update, scoped `lssyscfg -r lpar --filter ... -F` establishes partition
type and baseline settings. Missing/malformed/duplicate rows refuse effects.
Read after write and compare every supplied field; retain omitted baseline
values for detecting collateral setting changes. Commands have configured finite
timeouts and request a fixed scalar field count. Strict shape validation happens
after output collection; the existing SSH transport has no byte limit. This path
trusts the administered HMC to return bounded command output.

Results distinguish `verified`, `mismatch`, `readback-unavailable`, and
`write-outcome-unknown`: a command/transport failure cannot prove no mutation.
Return requested values, available readback and warnings. Never automatically
retry or roll back a potentially applied update. Local validation and ownership
failures raise before mutation. Failed creation post-configuration leaves the
partition and reports a failed workflow step, skipping later assignments.
Creation direct CLI settings readback also has an explicit workflow step;
unavailable/mismatching settings cannot yield workflow_completed=True.

## Time-reference impact

IBM's [time management guidance](https://www.ibm.com/support/pages/time-and-date-management-powervm)
explains that a running time-reference partition can govern hypervisor and service
processor time. This is not confined to the target's own clock. Present that
impact in both write presenter docs; do not change guest clocks or activate an
LPAR. The ad-hoc live procedure excludes True probes on shared systems unless
the operator explicitly grants system-wide time-source impact. False and zero
probes on an owned powered-off disposable partition do not manufacture runtime
proof. Configuration evidence is separate from clock/restart/migration evidence.

## Failure model

- Actors and deployments: local CLI operators and authenticated MCP clients
  against supported POWER10/POWER11 HMCs; offline tests simulate HMC boundaries.
- Invariants/assets: target containment/ownership, preservation of omitted
  settings and existing create options, no false completion claim, no implicit
  activation or time change, finite C01 scope.
- Accepted classes: HMC firmware/state rejection is reported with unknown write
  outcome where appropriate; unavailable live hardware leaves maturity unevidenced;
  concurrent external writers can invalidate before/after comparison because CLI
  supplies no conditional update (report mismatch, no retry).
- Covered elsewhere: profile lifecycle #1724/#637; resource/assignment validation
  existing creation workflow; configured command timeouts existing SSH transport
  (no byte limit; trusted HMC output and post-collection shape validation);
  actual migration/remote-restart actions #671; system clocks remain operator-owned.

## Threat model

- Added boundaries: typed settings input and one fixed scalar CLI update/readback.
  Widened boundaries: existing create command/presenters and ownership operations.
- Actors: local operators/authenticated MCP callers can supply malformed values
  and foreign selectors; trusted HMC can still return malformed/incomplete output.
- Controls: strict dataclass validation before effects; owning scoped resolver and
  description authorization; fixed allowlist of fields; record encoding then shell
  quoting; strict one-row scalar parsing and honest post-effect result statuses.
- Out of scope: compromised HMC administrator and external concurrent mutation;
  existing credential/session protection remains with the transport.

## Success and validation

Focused tests cover the five strict fields, boundaries/omission, twenty reference
bindings, exact encoded create/update commands and readback rejection/mismatch,
ownership-before-effects, invalid type/no target effects, create resource/options
preservation, apply/readback failures, and CLI/MCP forwarding including False/0.
Use `tests/lpar/test_settings.py`, existing create tests and app presentation tests.
A controlled fault must turn each new contract test group red before restoration.
Regenerate tools and maturity projection using repository recipes; keep new
operation implementation implemented and live status unevidenced until evidence.
Run `just verify` and `uv run --no-sync prek run --all-files` before push;
CI checks Python 3.11–3.14. The reproducible procedure names preflight, named arm,
recovery and deployed-HEAD check; hardware execution requires the root's exclusive window.

## Bounded named live arm

Extend existing `scripts/live_test/lpar_config.py` with opt-in subtask47,
`lpar-settings`, registered in runner/preflight and the existing prefix-based
recovery witness. It reuses the arm's baseline, unique-name/token, inactive
teardown and resource comparison helpers, without the ST39 activation/configuration
sequence. Create one small disposable AIX/Linux partition through the owning
core with `stamp_policy="required"`, explicit safe settings (False/0), and
`apply_profile=False`. Require ownership and creation settings readback before
MCP update probes: performance bool round-trip, priority0/255, time_ref=False,
simplified restart and migration bool round-trips if firmware permits. Read
through the typed presenter; compare supplied and omitted values. No profile
mutator, power-on, guest clock change or time_ref=True call is made. Cleanup
requires matching caller token and inactive state; teardown gains an explicit
inactive-only mode which refuses active state without a power call, including
a state change after an earlier probe; a failed/uncertain create is
adopted only through existing token proof. Recovery includes subtask47 in the
existing scratch-prefix witness. Preserve baseline names/free resources after
delete. Record direct core creation as observed; promote only actual MCP
settings/read dispatches with assertions and successful cleanup. Failed/skipped
branches remain failed/skipped; root grants the candidate-bound live window.
Focused offline arm tests cover group admission, strict ownership, no True time
reference, cleanup on a failed probe, and registration/recovery coverage.
