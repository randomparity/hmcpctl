# 0243 — System-profile job safety and observations

## Status

Proposed, #1725. Exact path/number reserved and published under q1725-d647765d before creation.
WORK:SCOPE6258196210 records the approved child allocation, writer-window and revised backend/
association contracts. Full design review and scope audit precede implementation/ADR acceptance.
The [full design](../workflow/specs/2026-10-18-system-profile-lifecycle-design.md) owns detailed
interfaces, source hashes, failure/threat models and the exact152-unit allocation.

## Context

System-profile actions reach multiple contained partition/profile pairs. No documented job
precondition fences independent writers. A source Get failed despite CLI code0; another captured
one two-member JSON row. Native Delete rejected its documented parameter, whereas separately
approved documented CLI cleanup succeeded. Source capture remains failed/partial; independent
cleanup state restoration is a different fact. No source grant approves product effects.

## Decision

Use the explicitly approved caller writer interval across namespace/membership, member names,
contained profile content/ownership and power from preflight through known terminal reconciliation.
exclusive_writer_window defaults false for mutations; list/get and validate-only omit it.
Require existing all-targets policy and EVERY member's ownership/containment, including old/new
union on update. Reread immediately before effects and independently afterward. Cache a contained
feed only WITHIN ONE observation phase; the next phase must perform a new transport read.
Timeout/interruption retains uncertainty and coordination, never cancellation/replay/rollback.

Use fixed REST Create/Get/Update/Activate and ALWAYS documented fixed SSH sysprof deletion, chosen
before effects. No REST fallback/retry/backend flag. Prove unique system CLI identity and quote
exact operands. Report command success/failure/uncertainty separately from post-read state, without
inventing a JobID/status. Preserve the same distinction for job/code/member observations.

Use the bounded observed JSON parallel columns. Require equal cardinality, current PartitionID/
name/UUID agreement and unique contained profile binding; validate the WHOLE inventory. Never
truncate/filter members, invent escaping or treat malformed data as empty. Multirow item reuse is
structural inference requiring tests/native proof. The approved exclusion-owner pairs are:

- Independent-writer fencing — operator coordination.
- Automatic replay/rollback after uncertain effects — caller reconciliation.
- Structurally unencodable or undocumented association encodings — operator-managed pending
  authoritative encoding support.

Activation returns per-member before/after state, available profile evidence and read errors;
running alone cannot establish requested-profile application. Job completion, validation and
complete/partial/failed/unknown execution classification and unverified application remain distinct.
No invented partial-result parser; no new exclusion or complete application proof is claimed.

## Consequences

All five rows/152source units, #638/#698 ownership and ADR0118 facade stay intact. No dependency,
generic proxy, persistence/recovery framework or automatic maturity promotion. Exact contracts
and test/live-arm ownership are in the reviewed spec/ignored plan. Product native proof requires
NEW scoped targets/effects/normal policy checks and exact deployed revision. Prior grants consumed;
failed source job retained. Use the repository live-testing procedure for applicable native hardware proofs.

## Considered & rejected

- CLI code0 as job success. verified: GetSystemProfiles at10905eaaa/HMC10.3.1060 returned
  FAILED_BEFORE_COMPLETION/code0; full design records its immutable terminal hash.
- REST-only Delete as proven on observed backend. verified: named source capture at5dfcf61f/
  HMC11.2.SP1120 submitted documented systemProfileName and received FAILED_TO_START.
- Guess another parameter or REST-then-SSH. judgment: no authority and adds effects after ambiguity.
- Local lock as fencing. judgment: it cannot coordinate independently operated HMC clients.
- Permissive escaping/filtered inventory. judgment: it substitutes unsupported identity evidence.
