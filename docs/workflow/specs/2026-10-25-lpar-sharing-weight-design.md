# D07: sharing and uncapped weight current configuration

Scope: #1780 / q1780-13ff64e1, canonical WORK:SCOPE comment 6401850618.
Full-spec; M / fixed denominator 250; root-selected A1/B1 design direction is
subject to independent review/audit. User authority is existing Start20 scope
with approved empty exclusions. This document adds no scope authority.

## Global Constraints

- Python requires-python = ">=3.11"; .python-version = "3.11".
- CI verification targets amd64 Python3.11–3.14; host arm64 is separate evidence.
- Existing stack: defusedxml>=0.7.1,<1; httpx>=0.28.1,<1; pydantic>=2.14.0,<3;
  fastmcp-slim[server]==4.0.10; typer==0.27.3; pinned development prek==0.5.5.
- Add no dependency, toolchain floor, generic proxy, stable facade export or static gate.
- Preserve containment, ownership authorization, audit, If-Match and exact zero/omission.
- No automatic replay, profile apply, activation, restart or compensation.
- No invented release/RMC/state matrix or default/previous transition weight.
- New live variants remain unevidenced until separately authorized matching-commit proof.

## Problem and ownership

Base 8167873b807d95923be8a51833c3111a1b2bb5a3 has six typed SharingMode values,
a protected asymmetric uncapped alias, and an ETag RMW which returns POST data.
Reuse LparResources, partition_updates, update_logical_partition,
set_lpar_processors and modify_lpar. No owner migration or new facade export.
A small operation-local sharing outcome helper may separate observation from
workflow orchestration; validation remains the document mapper's single policy.
The creation consumers and presentation callers listed below must migrate with it.

## Source and admission boundary

- Exact issue bindings: P10 chhwres synopsis153/attributes238 and P11
  synopsis144/attributes241; only partition proc s sharing_mode/uncap_weight.
- IBM Introduction to PowerVM SG24-8535-00 (March 2023), printed82/PDF95:
  integer 0–255, reference default128, zero receives no spare capacity.
  https://www.redbooks.ibm.com/redbooks/pdfs/sg248535.pdf
- IBM ansible-power-hmc immutable 8b10a3ffa50d5d30c2fc2b6278ab7355468b6a89:
  hmc_rest_client.py:1744–1773 maps six modes/inserts missing weight directly
  after SharedProcessorPoolID; powervm_dlpar.py:499–534 applies the joint
  transition then independently reads. Its truthiness skips zero and its
  configured-weight getter is insufficient for this completion contract.
  https://github.com/IBM/ansible-power-hmc/tree/8b10a3ffa50d5d30c2fc2b6278ab7355468b6a89
- No hard release floor, exhaustive state/RMC table or cross-release reset rule
  follows. A well-formed existing configured/current context and supported
  document structure admit an attempt; HMC may refuse. No guest proof is promised.

## Input and transitions

Add LparResources.uncapped_weight: StrictInt | None = None. Python entry points
also reject bool, float and string explicitly; zero is present. Creation rejects
this update-only field before any write through REST, native, all_resources,
fallback, VIOS and provisioning paths. Planning check_request rejects it through
reject_update_weight_on_create(request.resources) before planner reads; the MCP
handler retains delegation. Planning and provisioning agree; no new planning policy
or weight creation support is introduced.

Without weight preserve exactly: uncapped is True → uncapped; otherwise explicit
sharing_mode; otherwise uncapped is False → capped; otherwise no mode change.
With weight, supplied sharing_mode and uncapped must describe the same shared
mode. Subset/context validation applies independently to the effective mode:
shared admits capped/uncapped, dedicated admits the other four modes; dedicated
rejects either Boolean capping or weight. No dedicated/shared conversion.
Configured HasDedicatedProcessors and CurrentHasDedicatedProcessors must be
present, strictly true/false and equal; no caller fallback. Configured/current
modes must be valid members of that context before a sharing/weight change.
Their existing disagreement refuses before effects rather than guessing a base.

| Existing context | Mode request | Weight | Result |
|---|---|---|---|
| shared uncapped | omitted/uncapped | 0–255 | exact weight; omitted mode preserved |
| shared capped | omitted/capped | present | refuse; explicit uncapping required |
| shared capped | uncapped | 0–255 | one RMW sets both |
| shared either | capped | present | refuse, including zero |
| shared either | capped/uncapped | omitted | mode only; observe actual weight effects |
| shared | dedicated effective mode | either | refuse |
| dedicated | four dedicated modes | omitted | existing mapped mode update |
| dedicated | shared effective mode/Boolean | either | refuse |
| dedicated | either | present | refuse |

No default128, restored prior weight or synthetic zero is written. Capped and
dedicated weight is inapplicable, distinct from absent/malformed uncapped weight.

## Mutation and completion

Pure type/range/alias/common numeric validation runs before client use. After
existing containment and ownership authorization, validate the whole requested
resource mapping and exact XML edit structure on an Advanced pre-read before
rename. Repeat on the fresh ETag-bound RMW read immediately before POST. A race
can still cause a partial rename, which must remain visible with later legs
skipped. Common quantity validation is reused, without adding quantity semantics.

Only configured PPC/SharingMode and PPC/SharedProcessorConfiguration/UncappedWeight
are writable sharing paths. Missing weight is insertable only for effective
shared uncapped context, with exactly one configured shared container and direct
SharedProcessorPoolID anchor, no same-local-name wrong-namespace or duplicates,
and kxe=false/kb=CUD immediately after that anchor. No generic insertion API.
Other requested missing paths still refuse. Preserve untouched XML and If-Match.

Submit once. Distinguish pre-submit refusal, known 412 rejection, acknowledged
submission and unknown submission (transport or no proven successful response).
Known successful HTTP2xx acknowledges submission even with a malformed body; retain
that parse error in submission_error. Known412 is rejected. Never replay.
Independently GET the same partition after an attempt; HTTP202/POST echo is insufficient.
A single snapshot is bounded proof,
not a promise of eventual convergence or unique causality after an uncertain send.

Standalone sharing/weight resource results retain top-level UUID/resource and add precisely:
- completion_scope: "current-configuration";
- outcome: "applied" | "partial" | "rejected" | "unverified";
- workflow_completed: true exactly when configuration_verification is "matched";
- submission_status: "acknowledged" | "rejected" | "unknown";
- configuration_verification, runtime_verification: "matched" | "mismatched" |
  "unavailable";
- before, after: bounded observation dictionaries (after may be null);
- expected: requested configured path/value map plus effective REST mode and
  explicit weight target or null; submission_error/read_error: bounded string|null;
- existing change_location and warnings, derived from independent read when
  available; location classifies CurrentProfileSync, not independent profile proof.

Observations contain configured/current/runtime dedicated flag, mode, weight and
weight_status (valid/inapplicable/missing/malformed), raw_weight (string|null),
plus requested configured
path values. Exact current/runtime weight paths are PPC/CurrentSharedProcessorConfiguration/
{CurrentUncappedWeight,RuntimeUncappedWeight}; mode/flags are direct PPC children.
Identity must match selected UUID. Numeric configured mappings compare finite ASCII
Decimal values, preserving value equality across HMC formatting; mode/flags compare
exactly. Wrong namespace/duplicates/malformed observations
cannot establish a match. Requested configured paths must all match; mode must
match configured and CurrentSharingMode; uncapped configured/current weights must
be valid, equal, and equal any explicit weight target. Capped/dedicated weight
is not a numeric predicate. Runtime independently matches the verified effective
mode and applicable observed weight, or reports mismatched/unavailable.

With complete postconditions outcome is applied, even after uncertain submission
(with a causality warning). Otherwise changed observed requested state is partial;
known 412 with no observed change is rejected; remaining cases are unverified.
Unavailable/malformed identity is unverified. Unavailable context cannot prove
completion; known configured changes may still establish partial effects.
Runtime mismatch/unavailability adds a no-guest-proof warning but does not block
configuration completion. Profile apply/activation/restart are absent.

Mode/weight-free resource calls keep their existing result/quantity/memory path.
For mixed calls verify every requested configured mapping, with Current* checks
for sharing/weight only; this does not claim current/runtime quantity success.
Combined modify embeds the complete outcome in its resource step and lpar result;
on incomplete verification it sets workflow_completed=false, marks resources error
and skips PCIe. CLI exits1 and prints structured output without a green banner;
MCP standalone and combined return the same outcome fields. Memory-only filtering
continues to ignore processor fields under its existing documented contract.

## Failure model

1. Actors and deployments:
   - Authorized local Python/CLI operators and policy-controlled MCP clients.
   - HMC REST with concurrent administrators; CI uses synthetic/captured fixtures.
2. Invariants and assets:
   - Correct contained UUID, ownership/audit, bounded XML edits and If-Match.
   - Zero/omission and legacy mode compatibility; truthful partial completion.
3. Accepted failure classes:
   - One-read nonconvergence returns incomplete; operator may inspect later.
   - State/release/RMC-specific HMC refusal remains visible; no universal matrix.
   - Runtime/profile persistence beyond observed fields is explicitly unproven.
4. Covered elsewhere:
   - Quantity/move/pool/profile lifecycle: their existing domain owners/sibling issues.
   - Stable facade: ADR0118/public API gate; live authorization: docs/live-testing.md.

## Threat model

1. Boundaries: widen typed Python/CLI/MCP resource input and exact HMC XML edit/readback;
   add no command proxy, new credential handling or authority grant.
2. Actors: caller inputs and HMC responses are untrusted; existing authenticated
   access policy and operator-approved owner override remain authoritative.
3. Controls: strict integer/enum/alias checks, containment/ownership before reads
   that mutate, defused XML, exact namespaces/uniqueness, If-Match, bounded error
   strings, no replay and unchanged audit dispatch. Tests falsify each boundary.
4. Outside this change: compromised HMC truthfulness and independent concurrent
   later edits; TLS/auth transport guards retain their owners, with no bypass.

## Validation and live scope

Plan inventories every material contract with red/green commands and ownership.
All six modes and transition/subset errors have offline tests. ST59 is opt-in:
processor-sharing-weight, wrapper scripts/live_lpar_sharing_weight.py, scenarios
st59-lpar-shared-weight and st59-lpar-dedicated-sharing. Two separately owned
inactive AIX/Linux fixtures, no profile apply or activation: shared sequence
uncapped+64, explicit0, capped, uncapped+255; dedicated sequence four modes.
No compatible structural current context means SKIP without a mutation claim.
Invalid-weight/capped/dedicated requests must leave independent observations unchanged.
Reuse existing fixture creation/token/teardown/baseline recovery helpers. Explicit
partial-results checkpoints precede fixture creation and each submission; record()
alone does not persist. Failed checkpoints stop the next effect. Preflight
names resources, no RMC hypothesis; read-only recovery always follows. Arm tests
cover foreign token, missing identity/context, stale/mismatched post-read, failed
cleanup and default/all exclusion. No HMC run occurs in design or CI.
Ledger binds new variants as implemented only after build, never passed live from
old st39 quantity evidence. Retain exact unrun state/RMC/runtime/transition gaps.
