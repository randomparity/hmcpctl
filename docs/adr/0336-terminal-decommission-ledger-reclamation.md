# 0336 — Reclaim terminal decommission ledger rows after preserving provenance

## Status

Proposed for issue #1890; operator approved the provenance-preserving rule on 2026-11-04.
Supersedes ADR0323 Decision4's retained/partition-row lifetime only; its physical cleanup,
effect vocabulary and retain mode remain unchanged. Design review completed; scope audit pending.

## Context

Terminal delete_owned cleanup leaves partition and retained-item rows consuming capacity.
Its result normally records retained items, but unreadable rows currently report only kind
and reason, losing even available partial identity if the row is removed. Failed operations
may have no fresh result; abandoning an operation explicitly leaves its ledger untouched.

## Decision

1. On a fresh terminal completed/failed delete_owned transition, reclaim the guarded
   system/partition's partition, virtual_disk and installer_media rows only with a matching
   applied delete_lpar effect and a complete freshly persisted storage result covering the
   remaining storage rows. State, result and ledger deletion share one SQLite transaction.
2. Unreadable retained entries gain optional ledger_identity, their original JSON identity
   object without value coercion or discarded keys. Distinct unreadable records survive
   independently. Reconstruction matches readable disks by kind/VIOS/group/name and media
   by kind/VIOS/name, with normalized UUIDs and exact names; a deleted disk must not hide
   a retained same-name disk in another group. Existing readable projections and
   deleted/pending shapes remain unchanged.
3. Never use an older result retained by COALESCE as freshness evidence. Abandoned,
   nonterminal, retain, dry-run, unproven, missing/oversized-result and historical cases
   keep existing semantics; unknown ledger kinds are not garbage-collected.
4. Existing managed-system/partition ownership is the boundary, including rows whose
   producer operation has expired. Other partitions/systems are untouched; no attribution
   columns, database migration or producer reconstruction is introduced.
5. Existing result-size and 30-day retention limits stand. Operators own longer archival,
   retained physical storage removal and historical/manual recovery. No live HMC writes
   are introduced by reclamation.

## Consequences

Eligible rows stop consuming capacity while retained storage remains recorded for the
operation's existing lifetime. A missing complete bounded result conservatively leaves rows.
The result schema admits one optional nested object; old results remain readable. A failed
transaction preserves previous state/result/ledger together for existing recovery paths.

## Considered & rejected

- **Delete before saving the result.** judgment: a crash could destroy the remaining record.
- **Treat any terminal outcome as proof.** verified: store.operator_abandon explicitly
  preserves ledger, and engine._run can end failed with no result (base e00b81241).
- **Keep unreadable rows indefinitely.** judgment: incomplete closure; the operator chose
  preserving their original identity and applying the same eligibility rule.
- **Add an attribution schema or general ledger collector.** judgment: neither is needed
  for the existing system/partition ownership boundary or this issue's terminal transition.
