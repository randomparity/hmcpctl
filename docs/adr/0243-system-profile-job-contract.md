# 0243 — System-profile job safety and observations

## Status

Proposed, issue #1725; not product implementation authority. Optional exact path/number reserved
by the campaign orchestrator and published under `q1725-d647765d` before creation. The original
draft was preserved/restored byte-exact before this source-backed revision.
The writer-window interface is approved by “Approve #1725 writer-window contract”,
`call_0b65189f319d4a9f96e1330bed7509c7`, item0. “Approve both revised contracts”, `call_c202111f34f34b6bb0db4158db909334`, item0, additionally
approves the fixed CLI deletion/backend-outcome and bounded association/encoding contracts plus
the exact additional encoding exclusion-owner pair. Current WORK:SCOPE6258196210 records the
complete approved set. Full design review/audit are still pending; this ADR remains Proposed.

## Context

Five managed-system rows own create, read/list, update/rename, delete, validate and activation/
continuation across multiple partition/profile associations. All five rows/152source units stay
in #1725; the M design denominator is250 changed lines. #638/#698 and ADR0118's facade stay intact.
Independent POWER10/POWER11 REST references document all five jobs from HMC10.3.1060.0.
An authorized empty Get returned failed terminal status despite returnCode0, so status and CLI
code cannot be collapsed. The source-backed full design records provenance and private hashes.

An independently approved source probe at exact revision5dfcf61f on HMC11.2.SP1120 successfully
created a two-member scratch system profile and captured nonempty Get output: a JSON array of
objects with SystemProfileName and comma-separated PartitionNames, PartitionIds and
PartitionProfileNames string columns. Only one simple two-member row was observed; quoting,
multiple rows, other releases and ID-to-UUID binding were not thereby proven.

Native Delete rejected the exact documented systemProfileName parameter before starting.
Its read-only template declared no parameters. No replacement parameter or REST retry was
inferred. A separately approved one-shot documented rmsyscfg sysprof deletion returned success.
Its immediate after-read failed, honestly leaving runner exit1; a separate independent read-only
witness subsequently proved the entire original namespace/member baseline restored and scratch
absent. Original capture remains failed/partial; failed own job retained. Those scoped grants are
consumed, authorize no further effects and do not approve the product architecture.

## Decision

Retain the approved caller-coordinated writer window over namespace/membership, old/new member
names and contained profile content/power from preflight through known terminal reconciliation.
`exclusive_writer_window: bool = False` gates mutations; list/get and validate-only omit it and
make no atomic-snapshot promise. This is operator coordination, not distributed fencing or a
lease. All-targets grants and EVERY member's existing ownership/containment checks remain
mandatory; update authorizes the old/new union. Immediate rereads reject observed drift.
Timeout/interruption is uncertainty, not cancellation or coordination release. No automatic
retry, rollback, force or fire-and-forget mode. Existing exclusion-owner pairs remain independent
writer fencing — operator coordination; automatic replay/rollback — caller reconciliation.

Use fixed REST jobs for Create/Get/Update/Activate and ALWAYS the documented fixed SSH
rmsyscfg sysprof equivalent for deletion. Choose the route before any effect, with no public
backend selector and no REST-then-SSH fallback. Bind the command to the already authorized
system UUID using a proven unique CLI identity; quote exact system/profile operands and expose
no arbitrary flags or command text. Keep complete old-member authorization, coordinated window,
immediate inventory/profile rereads and independent post-readback. No row transfer/exclusion or
automatic maturity promotion follows from using an equivalent backend.

Expose command terminal success/failure/uncertainty separately from observed absence and member
preservation; do not manufacture a JobID or successful REST status for SSH deletion. Preserve the
same distinction between submitted/terminal REST job, returnCode/diagnostics and observed effects
for other operations. A failed readback stays failed even if later explicit reconciliation sees
restoration. Use existing owning outcome/presentation patterns, not a general execution framework.

Use one strict bounded association parser for the observed JSON item shape. Validate all
records before any old-member mutation. Match equal-cardinality unique numeric IDs against
current PartitionID, exact partition name and UUID in the selected system, then uniquely resolve
each profile through that partition's contained feed. Neither position alone nor a globally
matched profile name is authorization. Multirow support repeats the item contract and is an
explicit structural inference needing tests/native scenario, not an observed multirow pass.
No undocumented escaping is invented. Ambiguous/delimiter-bearing or malformed input/output
fails closed with an actionable encoding diagnostic, never empty inventory or filtered members.
The approved exact additional exclusion-owner pair is: structurally unencodable or undocumented
association encodings — operator-managed pending authoritative encoding support. It is explicitly
approved by the later operator decision. One unsupported record invalidates the WHOLE inventory; no partial list or filtered
member mutation. The operator manages these profiles outside this surface pending authoritative
encoding support. All five rows stay allocated; maturity remains evidence-scoped and partial.
The existing two writer-window exclusion-owner pairs remain unchanged.

Observe every activation member's requested profile, before/after state, available profile link
and read failures. Running alone does not prove this invocation used the requested profile.
Aggregate job status, validation and member effects stay separate; incomplete postconditions mean
unverified/partial effects. Do not invent a partial-result parser or replay/rollback behavior.

## Consequences

The writer-window and later backend/result/encoding contracts are approved; the complete design
and ADR acceptance remain gated by full review/audit.
The full design maps every mode/parameter/source unit and records remaining release/controller,
association-format and native verification limits. Full product design review/audit precedes
implementation; source branch2/security3 reviews are not product approval and lifecycle budgets
remain0. Native Update/activation/validation/continuation have not been run. A final product proof
requires separately scoped named preflight/arm/recovery at exact deployed product HEAD.
Use the repository live-testing procedure for applicable native hardware proofs.

## Considered and rejected

- CLI code0 as successful job: verified: the earlier read-only GetSystemProfiles job at
 10905eaaa/HMC10.3.1060 returned FAILED_BEFORE_COMPLETION with returnCode0; terminal hash
 0116ea368a7a9ed4747435d246a200b45fae6e4d4860d5e2ba5636328b868147.
- REST-only deletion as proven on the observed backend: verified: named
 scripts/live_system_profile_source.py capture at5dfcf61f/HMC11.2.SP1120 submitted the documented
 parameter and received FAILED_TO_START; the full design records its immutable capture hash.
- Guess a parameter or REST-then-SSH retry: judgment: no source authority and violates bounded uncertainty.
- Move the Delete row or silently exclude a release: judgment: changes approved allocation without authority.
- Local lock as distributed fencing: judgment: cannot coordinate independently operated HMC clients.
- Invent escaping or pair fixtures from request examples: judgment: does not prove safe result association.
- Activate members to learn inventory grammar: judgment: adds unrelated effects; activation proof is separate.
