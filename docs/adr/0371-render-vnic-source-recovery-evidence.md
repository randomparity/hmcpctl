# ADR 0371: Render vNIC source recovery records as public evidence

## Status

Accepted (2026-11-12), under the operator-confirmed campaign dispatch for #1934.

## Context

The `vnic-creation-policy` owner's version-2 `update-mode-source` procedure
(ADR 0312), which runs the six fixed sentinel cases (#1856) and the
`finite-update` case (#1857), writes private journals. `scripts/live_test_evidence.py`
had no route for them: they carry `expected_commit` instead of `run.tested_commit`,
so every one fell through to the refusal.

ADR 0312 keeps raw snapshots, submission errors and native text inside an approved
private evidence boundary. The issue asked whether `expected_commit` is weaker
provenance than `run.tested_commit`. It is not weaker in practice:
`_validate_execution_context` refuses to start unless `git rev-parse HEAD` equals
`expected_commit` and the checkout's `git status --porcelain` is empty. That check
runs before the document is created, so a runner-written document names the clean
commit that ran. Since `3bab17c82` the status check also passes
`--untracked-files=all` and strips `GIT_*` variables. The #1856 and #1857 records
predate it, and their plain status hides untracked files only when Git's display
settings do. Even so, it inspects the whole checkout, while `run.tree_clean`
inspects only `src/` and `scripts/`. Both fields are unsigned JSON that the runner
writes, so neither resists hand editing.

## Decision

Route `vnic-creation-policy` documents to a dedicated renderer. The renderer accepts
only a version-2 `update-mode-source` record at stage `recover`. The record must be
complete, read-only (no intents, no effect commands) and session-closed. Its case
must be one of the seven fixed cases, `expected_commit` must be 40 lowercase hex,
and `status` must be `CLEAN` or `MANUAL_RECOVERY`. The owning module provides
`validate_source_recovery(document)`, so the record vocabulary stays with its
writer.

The renderer emits only the case name, the commit, the record's validated 64-hex
`packet_sha256`, and fixed strings. The digest lets someone who holds the private
packet tell which run a row came from. It cannot reveal the packet's operands,
because the packet also carries random UUIDs. `CLEAN` is
written as submission `completed`, readback `verified`, preservation `unchanged`
and recovery `CLEAN`. Independent recovery reports `CLEAN` only when the capture
journal held exactly those values (`_source_history`), so the row restates a
predicate the owner already checked and invents nothing. Any other complete
recovery is written as `not established` for the first three columns and `HELD`
for recovery. Every other document in the group, including fixture,
version-1 creation, preflight, capture and incomplete records, is refused, and
none of them falls through to the generic matrix.

## Consequences

- A qualified source case can be cited publicly by its recovery record alone.
- A refused case renders only as `HELD`. Its HSCL code and HMC text stay in the
  private capture journal, so the prose outcome tables in `docs/live-testing.md`
  remain the only public account of a refusal.
- A `finite-update` row does not say which update ran, because its operands carry
  VLAN IDs and MAC addresses. Its packet digest can be matched to a private journal,
  but not to a public W label.
- Version-1 creation records and #1858 updater journals stay unrouted.

## Considered & rejected

- **Document the records as deliberately not public evidence.** judgment: the
  provenance objection does not hold (see Context), and a qualified case loses a
  citable, falsifiable artifact for no redaction gain.
- **Render the capture journal, which carries submission and refusal codes.**
  verified: `_capture_source` in `scripts/live_test/arms/vnic_creation_policy.py`
  at `8604b8a2` stores snapshots, effect commands and the HMC error message in the
  same document. Its recorded outcome has not been checked by the independent
  recovery, which the operator's acceptance criteria require.
- **Validate the record inside the evidence script.** judgment: that would copy
  the case and purpose vocabulary out of its owner, and the copies could drift.
