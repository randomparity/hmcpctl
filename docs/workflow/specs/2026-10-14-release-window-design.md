# Release window command

## Problem and scope

Issue #1513 composes ADR 0212's installed release mechanisms into one local command.
The command owns sequencing and authorization; claim validation, baseline reads,
baseline implementation, evidence formatting and promotion keep their existing owners.
The approved expansion repairs actual round2 recovery and its producer artifacts.
No caller migration or compatibility path is needed. [ADR 0220](../../adr/0220-release-window-failure-boundary.md)
records the execution and failure boundary. Other release docs remain #1514's scope.

## Interface and execution

`uv run --no-sync python scripts/run_release_window.py --claims FILE --branch promotion/NAME
--output-dir PRIVATE_DIRECTORY [--authorization FILE | --dry-run]` runs at the root
of a clean non-default candidate checkout with its normal `.env` and installed venv.
Require a committed claims file below docs/capabilities/releases, validated through
`load_release_claims`, nonempty and restricted to named arms from docs/live-testing.md.
Derive unique arms in named-wrapper order, independent of claim row order.
Reject main/master and the discovered local origin default branch, detached HEAD,
dirty tracked/staged/untracked trees, existing promotion branch or invalid branch name.
Reject old selected-arm results/sidecars and old round2/vmedia outputs because the runner
can restore artifacts from those files. Do not delete or overwrite another attempt.

Prepare the same local HMC environment as the runner (`_bootstrap_config`, then
`_load_dotenv`), capturing diagnostics privately, before `HMCConfig`-dependent prediction.
Read configuration with `LiveTestConfig.from_env_file` and predict each selected arm
through `arm_verdicts` without hardware. Derive baseline systems from the configured
system plus each arm's system_name. A configuration-unrunnable selected arm refuses.
The dry run prints ordered phases and a JSON authorization template with `authorized`
false, candidate commit, arms, mutation_boundary (each arm's existing preflight mutates
list plus the temporary round2 provisioning boundary below), and vios_access false. The operator reviews the runbook's named-arm semantics,
sets authorized true and explicitly grants vios_access when selected arms use VIOS.
The authorization file must exactly match candidate/arms/boundaries; it is bounded JSON
with duplicate-key rejection and closed fields. VIOS-bearing arms are round2, vmedia,
sriov, profiles, vios-backup, network, storage and lpar-power. When selected, capture
VIOS baseline coverage for the configured serving VIOS numeric ID; refuse missing or
ambiguous identity. The baseline command independently validates its selected VIOS.
Resolve that identity with read-only inventory only after authorization; do not infer it
from a stale result. Dry-run describes this deferred discovery without HMC calls.
The input is an operator attestation, not identity authentication or a live-run grant
from the development workflow. No credentials or configuration are printed by the summary.

The output directory must be an existing private mode-0700 physical directory outside
the checkout; use new mode-0600 logs, snapshots, evidence and summary. Dry-run creates
nothing and launches no child scripts. Run children with argv arrays and restrictive
umask; send raw stdout/stderr to private per-phase files, not public summaries or memory.
The named arms retain their normal ignored fixed result paths at checkout root.

Capture each independent baseline before any arm. For each arm call
`live_test_preflight.py --group ARM`, then its named wrapper, then
`live_test_recovery.py --results test-results-ARM.json`. Once an arm is attempted,
recovery runs even on nonzero exit, spawn failure or catchable interruption; missing
results make existing recovery fail closed. Recovery failure stops further arms.
A failed preflight never starts its arm; a failed arm stops after recovery.
On successful arms and clean recovery capture after-baselines and compare each pair;
any failed phase blocks evidence promotion. Generate each arm's evidence with
`live_test_evidence.py RESULTS` into a private Markdown file. Finally call the installed
`promote_live_observations.py --claims FILE --branch NAME --window-complete
--recovery-confirmed RESULTS...`, then `check_capability_inventory.py --require-current FILE`.
The promotion tool alone owns sidecar/provenance validation, branch creation, allowlist
commit and snapshot generation; do not duplicate or weaken its rules.

Catch SIGINT/SIGTERM around execution. Interrupt the active child process group, await
its exit (bounded graceful wait, then terminate/kill), and only then run recovery.
Protect recovery from repeated catchable signals and wait for it before exiting.
Never run recovery concurrently with a still-running child. On failure/interruption
write a private pass/fail phase summary and stop without retries, automatic rollback,
push, tag or writes to the original branch. Keep partial evidence for operator inspection.
After promotion failure or final gate failure, retain its unpushed branch for inspection.

## Round2 recovery and reversible producer boundary

Preserve the complete existing round2 dispatch and historical release claims. ST1 includes
an expected firmware refusal probe; ST4/5/6/7/12 are reads/plans and ST13 is the actual
provision dry run. These are classified from their implementations, not called pure GETs.
ST8 creates/modifies/powers/deletes a scratch partition; ST10 round2 changes/restores a
description and checks refusal paths; ST14 provisions; ST15 restores properties.

ST0 captures a new per-run independent snapshot through existing baseline.capture after
resolving the serving VIOS, before mutation, and retains it in artifacts. It additionally
retains the description/profile compatibility values already read by ST0. Missing or
malformed baseline evidence aborts round2 before mutations. Do not restore this new
witness from an old results document. Older results lacking it fail recovery closed.
ST8 first proves its configured scratch name absent, refusing unreadable inventory or a
pre-existing name before create. An ordinary failure or interrupt may leave residue;
recovery must report it, never infer successful deletion from producer status.

Replace ST14's deletion/recreation of the configured baseline partition and disk with
one run-unique temporary partition and volume. Retain the configured originals untouched.
Generate names with a per-run token, validate absence through complete partition/volume
reads, and persist names, ownership token, system, VIOS and volume group before mutation.
Use existing provision/create/ownership/decommission interfaces, without a new framework.
Provision still performs live creation, network attachment, storage mapping and power-on;
its resource inputs come from the captured baseline. Cleanup in a finally path discovers
any created partition by its fresh name, validates its ownership token before mutation,
then decommissions that partition, confirms absence, and deletes only the run volume after
an independent mapping read proves it unmapped. Missing response/identity, foreign owner,
partial inventory or failed cleanup yields FAIL/manual recovery; no force or ownership
override. Cleanup never deletes the configured baseline partition/disk. ST15 retains its
property restoration role against the preserved baseline partition.

Recovery consumes the fresh round2 snapshot, validates its shape and scope against the
result's configuration/HMC identity, and independently recaptures through the installed
read-only baseline interface. Compare complete normalized tables without dropping UUIDs,
rows or state. Read description/profile compatibility independently and compare the
captured fields. Report surviving scratch/provision fixtures and missing/mismatched
baseline data as unclean/unreadable. This supplies witnesses for ST8/10/14/15 and the
round2 dispatch; existing detailed checks for other round2 tasks remain active.
All recovery hardware reads remain read-only: existing MCP allowlist plus the baseline
module's read-only transport calls. No recovery mutation or automatic repair is added.
The outer release-window baseline is separate from producer artifacts and remains required.

ST14 previously replaced an existing resource. On ff9b020, provisioning.py deletes it,
operations/lpar/provision.py creates a new UUID, and baseline.mapping_rows preserves the
mapping's associated-partition identity. An offline transport replay through actual ST14,
ST15, mapping serialization and baseline comparison reports partition/mapping drift;
even with state restored it reports mapping drift. This is constructed evidence, not a
hardware observation. Preserving originals is the necessary producer repair; equality is
not relaxed and historical claims are not retargeted to another arm.

## Success and validation

Offline tests stub every hardware-touching script. A real CLI subprocess in a fixture
clone uses stub preflight/arms/recovery/baseline with real claim validation, evidence,
promotion, Git and final gate; it produces an allowlisted commit on a new branch while
preserving the candidate. Unit tests cover authorization mismatch/missing/duplicates,
dry-run isolation, ordering, failed phases, early stop, child interruption/reaping,
recovery errors, stale outputs, private files and prohibited branches/paths.
Actual producer/recovery composition tests use deterministic transport boundaries, real
baseline parsing/comparison, full round2 subtasks and real artifact serialization. Cover
ST14 success, collision, foreign ownership, missing baseline, lost response, interrupted
provision, failed cleanup and readonly recovery enforcement. Inject independent residue,
identity/state/property drift and malformed reads; recovery must fail. Controlled faults
prove authorization, recovery-on-failure, strict equality and no-promotion gates bite.
Python 3.11–3.14 and existing project dependencies only; no new dependency.

## Failure model

- Actors and deployments: one trusted local operator, one writer per checkout and managed
  systems quiescent during the window; no CI hardware access.
- Invariants and assets: explicit candidate-bound authorization, sequential arm/recovery,
  claimed evidence provenance, private raw hardware data, unchanged original branch.
- Accepted failure classes: SIGKILL/host loss cannot execute cleanup; retained private
  outputs support the existing manual recovery runbook. Disk failure may prevent summary
  persistence and is reported on stderr. A child which cannot be reaped stops the window
  without claiming recovery or starting another hardware process. Hostile concurrent
  file/repository/config writers and forged operator attestations are outside this model.
- Covered elsewhere: arm cleanup/recovery semantics and environmental admission are the
  existing named scripts, with the round2 repair above; claim routes #1511; baseline reads #1479; promotion/provenance
  and allowlist #1512; release gate #1478; CLI live assertions #1477; release docs #1514.

## Threat model

- New boundaries: authorization JSON/paths to hardware dispatch; subprocess outcome to
  later mutation/promotion; private child output to local files and safe phase summary;
  HMC inventory/ownership observations to scratch admission and fixture deletion; saved
  snapshot scope/identity to independent baseline reads outside the MCP allowlist.
  Existing underlying child transport implementations remain unchanged.
- Actors: trusted operator may supply stale or malformed local inputs; repository code,
  hooks, configuration and interpreters are trusted, single-writer inputs.
- Controls: bounded duplicate-rejecting authorization; exact derived comparison; committed
  validated claims; clean non-default checkout; absent fresh result paths; argv-only
  processes; sequential recovery; private exclusive files and redacted phase diagnostics;
  reuse promotion's provenance and commit allowlist instead of reconstructing them.
  Producer mutations require fresh absence evidence, exact ownership and complete mapping
  validation. Recovery validates saved scope and current HMC identity before invoking the
  installed read-only baseline transport, then compares its complete normalized tables.
- Out of scope: malicious local writers and credential compromise; hardware truth is
  supplied by existing arms, and crash recovery follows the retained-results runbook.
