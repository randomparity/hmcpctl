# 0252 — System-profile job safety and observations

## Status

Proposed, #1725. Exact path/number reserved and published under q1725-d647765d before creation.
WORK:SCOPE6273154252 records the approved child allocation, writer-window and revised backend/
association contracts. Direct “approve” of proposalSHAf3fe736017cea9742f5299c76f8d178020f3d012b0628ac38c81419da8e55b98
updates the contracts below; revised scope audit precedes implementation/ADR acceptance.
The [full design](../workflow/specs/2026-10-18-system-profile-lifecycle-design.md) owns detailed
interfaces, source hashes, failure/threat models and the exact152-unit allocation.

## Context

System-profile actions reach multiple contained partition/profile pairs. No documented job
precondition fences independent writers. A source Get failed despite CLI code0; another captured
one two-member JSON row. Native Delete rejected its documented parameter, whereas separately
approved documented CLI cleanup succeeded. The historical source capture failed/partial; later source calibration remains FAIL2. Independent
cleanup state restoration is a different fact. No source grant approves product effects.

## Decision

Use the explicitly approved caller writer interval across namespace/membership, member names,
contained profile content/ownership and power from preflight through known terminal reconciliation.
exclusive_writer_window defaults false for mutations; list/get and validate-only omit it.
Require existing all-targets policy and EVERY member's ownership/containment, including old/new
union on update. Reread immediately before effects and independently afterward. Cache a contained
feed only WITHIN ONE observation phase; the next phase must perform a new transport read.
Timeout/interruption retains uncertainty and coordination, never cancellation/replay/rollback.

Use REST Create/Update/activation-only; ALWAYS fixed documented SSH whole inventory, deletion,
validation and ONE combined validation/activation command. Choose BEFORE effects; no REST Get/
test=true attempt, fallback/retry/backend flag. Prove unique CLI MTMS and quote operands. Existing
transport includes stderr; exit0 diagnostics are uncertain, known nonzero failed, ambiguous outcome
uncertain. Never fabricate job facts or waive native warnings/ErrorData.

Whole CLI name:lpar_ids:profile_names rows require equal cardinality, positive unique current IDs/
name/UUID and contained profile binding, complete independent namespace and separate fresh phases.
Reject invalid/ambiguous output WHOLE; never truncate/filter or invent escaping. Source supports
bounded multiple rows, not universal encodings. The approved exclusion-owner pairs are:

- Independent-writer fencing — operator coordination.
- Automatic replay/rollback after uncertain effects — caller reconciliation.
- Structurally unencodable or undocumented association encodings — operator-managed pending
  authoritative encoding support.

Activation retains raw states/profile evidence and strict backend facts. Raw changed/unchanged/
unavailable labels do not classify profile application. Complete requires strict backend success
AND every member newly witnessed on requested contained profile; partial requires failed backend,
at least one new witness and another demonstrably different requested identity/resources. Failed
unchanged mixed state is not partial. Prior-matching, missing/contradictory/uncalibrated evidence
stays unknown; no universal field-application inference or new exclusion. Spec owns exact rules.
The source complete/N1/N2 observations calibrate these cases, not all member/resource/release shapes;
source capture stays FAIL2, independent restoration/window release are separate facts.

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
