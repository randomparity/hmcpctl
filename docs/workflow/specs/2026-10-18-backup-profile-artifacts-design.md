# Backup-profile artifacts — proposed complete contract

Status: operator-approved contract (2026-10-18); native-derived diagnostic amendment approved.
Approval: “Approve #1726 contract”, call_b49de860eff2499198d8e0d1fbb04d1f item 0.
Optional root-assigned
ADR reservation: `docs/adr/0244-backup-profile-artifact-contract.md=0244`; see [ADR 0244](../../adr/0244-backup-profile-artifact-contract.md).
Issue #1726; full-spec lane; fixed M denominator 250.
The four allocated REST rows and all 78 source units remain in scope. Existing
backup/overwrite and restore types 1–3 are reused. #638/#698 keep their allocations.
The approved additional exclusion-owner map is empty. No hardware mutations granted.

## Proposed interfaces and data flow

Extend the existing profile domain/presentation boundary; no facade exports,
transport wrapper for parity, arbitrary command, generic proxy or dependency.
The new domain owner is `operations/lpar/profile_artifacts.py`; existing restore
option forwarding stays in `ssh/profiles.py` and `operations/lpar/configuration.py`.

1. `list_backup_profile_artifacts(hmc, system_name_or_uuid)` resolves one system,
   submits its documented `ListBackupProfileData` job and waits with existing
   bounded job lifecycle. Public MCP name: `hmc_list_backup_lpar_profiles`;
   CLI: `lpars list-profile-backups SYSTEM`. Return `BackupProfileInventory`
   with `system_uuid`, normalized `job: JobOutcome`, and `artifacts`, where
   `artifacts` is either a tuple of `BackupProfileArtifact(file_name, backup_time)`
   or `None` when the job did not complete cleanly. These new results project
   JobOutcome locally with raw `job=None`, retaining found/status/timeout and
   usable job identity. Job ID/href/status strings over 4,096 UTF-8 bytes fail
   explicitly; diagnostic/readback detail is capped at 2,048 characters with an
   explicit `[truncated]` suffix. Raw extra HMC fields are not returned. The shared
   job API signatures and models are unchanged. Shared diagnostics recognize the
   observed `errorData` spelling alongside existing `ErrorData`. An empty tuple means a proven
   empty list; `None` never means empty. `backup_time` is opaque source text.
   The successful job's lowercase `result` parameter is a JSON array containing
   `BackupFileName` and `BackupTime` strings. Reject missing/duplicate result
   parameters, malformed JSON, wrong item/value types and duplicate file names.
   Bound result text to 1 MiB UTF-8 and inventory to 1,000 entries; overflow fails
   explicitly rather than truncating an inventory used to authorize deletion.
2. `delete_backup_profile_artifact(hmc, system_name_or_uuid, file_name)` uses the
   same system resolution and a complete successful inventory to require one
   exact file-name match before submitting `DeleteBackupProfileData(file=...)`.
   MCP: `hmc_delete_backup_lpar_profile`; CLI:
   `lpars delete-profile-backup SYSTEM FILE`. Reject empty/padded, path-shaped,
   dot-only, leading-dash and control-bearing file identities before mutation.
   The delete tool uses existing destructive managed-system authorization and
   `exhaustive_targets=False` (all-targets); it makes no new containment promise.
   A successful terminal job gets a fresh list on the same system and exact name.
   Return `BackupProfileDeletion(system_uuid, file_name, job, postcondition,
   postcondition_error)`; the last field is optional actionable readback detail.
   `postcondition` is `absent`, `present`, `unknown`, or `not-checked`. Report
   `absent` only after successful job plus complete fresh inventory without that
   exact name. Failed/warning/timed-out jobs remain their original normalized
   outcomes and never become clean deletion success. Readback failures report
   unknown with actionable detail; never retry mutation or select another file.
   No promise of atomicity against an independent HMC writer is made.
3. Existing restore functions and MCP entry point gain keyword-only
   `force_keystore: bool = False`; true appends the documented `--forcekeystore`
   to existing `rstprofdata`. Keep existing `restore_type`, approval,
   `ownership_override` and audit behavior. Do not add a second restore transport.
   Preserve stdout and stderr warning output and label no keystore warning as
   clean success. Existing `ssh/transport.py::run_hmc_command` currently returns
   only stdout after a zero exit status; blindly adding the flag could lose a
   stderr warning. Proposed minimum correction: keyword-only
   `include_stderr: bool = False` on that existing SSH function, enabled only by
   restore, retaining stdout and appending labeled nonempty stderr. Other callers
   retain their existing return contract. Test a successful command with stderr
   warnings and a failed command separately; native warning behavior remains unrun.
   Initialization/type 4 stays denied before command construction. Its allocated
   source units remain explicitly dispositioned as restricted existing behavior;
   keeping that boundary needs no new waiver. No new acknowledgement escape hatch.

4. Operator approval “approval to prepare the shorter-name probe and a new review
   phase” authorizes one optional closed live setting:
   `LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH=12|16`, default 16 unchanged.
   The existing named arm uses that many unpredictable suffix hex digits with
   the same reserved prefix. Convert the optional value strictly; reject other
   setting values and invalid direct configuration before calls. Saved results
   retain an integer; older results default to 16 and malformed types/values fail.
   Preflight declares the chosen shape/length. No new script/group, arbitrary
   filename, product deletion fallback or hardware grant is added.

## Source and accepted boundaries

A read-only source job on HMC Version 11 Release 2 Service Pack 1120 completed
`COMPLETED_OK`, returning five existing artifacts. Its lowercase `result` value
was a JSON array of objects with exactly `BackupFileName` and `BackupTime` string
fields; names were distinct bare names. Its own job was cleaned up. No backup,
restore or artifact-delete operation was run. This is source-format evidence,
not candidate native verification, and does not establish every server's schema.
The source client revision was 10905eaaa79628d68bb79ce720a2bb6a7006906b.

A later candidate run at cfbdb5aee9f67132050f4b2aaca05e9d4aceb326 created and
listed its 34-character scratch backup, but the documented REST deletion returned
`COMPLETED_WITH_ERROR`/`errorData`/`REST019C` invalid file. Read-only job inspection
confirmed exact system/operation/file identity and an unpadded request. Separately
authorized CLI recovery restored the original five inventory entries/timestamps.
The backend rejection cause is unestablished; the 30-character diagnostic option
tests length while preserving prefix/character classes, not a claimed maximum.
Neither consumed effect grant authorizes another run or a new candidate.

Both P10/P11 list references require HMC 10.3.1061.0 or later and specify operation
`ListBackupProfileData`, group `ManagedSystem`, PUT and no parameters. Existing
serialization matches. An earlier Version 10 Release 3 SP1060/build2408210051
context was below that floor and returned HTTP400 `REST0006 No such Operation`;
it supplies no supported response contract. No new attempt on it is planned.

References: P10 list `054-listbackupprofiledata_managedsystem-job.md:24`, P11
list `055-listbackupprofiledata_managedsystem-job.md:24`, both under their
`docs/refs/hmc-rest-api-p*/jobs/managedsystem-jobs/` directories; corresponding
`049-deletebackupprofiledata_managedsystem-job.md:24` specifies `file`.
P10 `043-backupprofiledata_managedsystem-job.md:24` covers reused file/force.
P10 restore `071-restoreprofiledata_managedsystem-job.md:25` and P11 counterpart
`073-restoreprofiledata_managedsystem-job.md:25` document forceKeyStore/type4.
Both `docs/refs/hmc-commands-p*/commands/rstprofdata.md:33` specify the exact
SSH flag and warning behavior. Vendored reference prose is not reproduced here.

ADR 0044 establishes that arbitrary HMC `file_path` is not managed-system
contained. ADR 0092 requires the existing audited override for opaque backup
restore; current-partition inventory cannot authorize unseen backup contents.
These remain intact. Existing backup accepts explicit overwrite consent; no
competing `rmprofdata` wrapper or #698 transfer is proposed.

## Failure model

1. Actors and deployments: configured operators through Python domain calls,
   CLI and policy-authorized MCP; supported HMC jobs with the observed V11R2 shape.
2. Invariants/assets: exact artifact/system identity, recovery backups, truthful
   missing/error/warning/timeout/readback states, restore ownership and audit.
3. Accepted classes: independent HMC writers can race reads and deletion; the
   approved contract reports observations, not fencing or atomic rollback.
   Unsupported result shapes fail closed, rather than claiming universal support.
4. Covered elsewhere: #638/#698 retain their ledger rows; ADRs 0044/0092 hold
   existing path authorization and restore ownership; native effects require
   separate scoped authority and repository live-testing preflight/recovery.

## Threat model

- Added boundaries: untrusted HMC JSON result into typed artifact identities;
  caller file identity into documented delete job. Existing boundary widened:
  restore opt-in exposes SSH stderr alongside stdout.
- Actors/trust: local/domain callers control selectors; MCP callers are governed
  by existing policy. The selected HMC is trusted for observations, not for shape,
  size, duplicate identity, terminal state or clean postconditions.
- Controls: exact system resolution; file validation and XML escaping; complete
  bounded parsed pre-list; all-targets destructive MCP authorization/audit; fresh
  same-system readback; existing restore approval and audited ownership override.
  Return error/status without claiming success or automatically replaying writes.
- Out of scope: compromised HMC administrators and independent writers cannot be
  fenced here (approved non-atomicity); credentials/host trust remain existing
  transport/configuration responsibilities. No new host/file transfer mechanism.

## Validation and remaining design work

Use ADR 0244 and the local full implementation plan for the combined independent
design review; normative behavior traces to the frozen charter and approval.
Test parser boundaries and meaningful invalid/missing/collision/denied cross-target,
warning/failure/timeout/concurrent-change/readback outcomes. Preserve backup
consent and restore ownership/audit regression coverage. Establish red before new
behavior, then green focused tests plus relevant lint/format/type checks.
Update CLI/MCP discovery, generated docs, changelog and maturity bindings; no
automatic native promotion. Final candidate requires `just verify` and pinned
`uv run --no-sync prek run --all-files`. A separately authorized `profile-artifacts` arm is added through
`scripts/live_profile_artifacts.py`, runner subtask43, existing `profiles.py`,
matching preflight and independent read-only recovery. It requires a complete
clean inventory without its reserved scratch prefix, creates one unpredictable
`hmcpctl-live-prof-<16hex>` bare-name backup by default (12 hex digits only for
an explicitly authorized diagnostic configuration) without overwrite, proves listing,
and deletes only that exact scratch artifact once. An ambiguous create/delete
never triggers a retry or restore; recovery reports residue without deleting.
The broad existing profiles/st10 restore arm is not this proof. Negative restore
and cleanup outcomes are exercised offline; the artifact-only native arm performs
no restore. No native run occurs before separate scoped approval and exact
candidate deployment proof.
Use the repository live-testing procedure for applicable native hardware proofs.
