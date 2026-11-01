# Existing-vNIC update source qualification

Design for #1828 at `4d500831b1a197204cbb2a47cc08526a4bb95fbf`.
Authority is the issue's `WORK:SCOPE` token `q1828-eed0dad6`, recorded in
[the frozen scope](https://github.com/randomparity/hmcpctl/issues/1828#issuecomment-6557892997).
[ADR 0312](../../adr/0312-vnic-policy-updates.md) records the owner decision.
Status: source-version-2 implemented and offline verified at `07d8a360`.
Hardware admission, public updater implementation and publication remain held.
The separately approved [retained-fixture prerequisite](2026-11-01-vnic-retained-fixture-design.md)
extends the same owner without changing source-version-2 semantics.

## Problem and retained parent scope

The complete update design retains `port_vlan_id`, `pvid_priority`,
`allowed_vlan_ids`, `allowed_os_mac_addrs` and `auto_priority_failover`.
Its approved direction is a separate typed updater with CLI/MCP presentations,
contained system/partition/slot, coordinated writers, at most one submission per
complete request, independent submission/policy/preservation results, and no retry
or rollback. This document completes only the first source-qualification design.
It does not claim the complete product interface is ready to implement.

The first tranche tests six unresolved list-mode hypotheses, one per separately
admitted packet. Later finite replacement/add/remove controls, replacement from
observed all/none, sentinel-baseline deltas, nontrailing quoting, two-list and mixed
actions remain #1828 obligations. `all -= finite` has no justified positive oracle.
No finite-only exclusion, universe expansion, implicit reset or 27-case batch is
adopted. Their next design requires the observed source results and an explicit
coverage decision; individual samples do not prove an operator/mode cross-product.

Creation/mac_addr remain #1827; backing updates/failover #669; system defaults
#680; multi-backing and ledger allocation #638; profile CRUD #1724/#637.
No current fixture with this invocation's caller token has been admitted.
Root separately owns exact fixture and stage admission; the retained-fixture
prerequisite supplies preparation/cleanup only after its own quality gates.

## Global Constraints

Python >=3.11; local pinned Python 3.11 and CI Python 3.11–3.14 on amd64 Linux.
Local arm64 macOS verification is distinct evidence. Add no dependency or tool.
Use `just setup` and `uv run --no-sync`; preserve the six-name `hmcpctl.api` facade.
Keep plans ignored, use existing operation/transport ownership, and redact public
evidence. Version-1 behavior and limits remain unchanged. Implementation has
separate maintainer approval; this design grants no hardware invocation, push,
PR, merge or support promotion.

## Owner and interfaces

Extend `scripts/live_test/vnic_creation_policy.py`; keep `validate_packet(packet)`,
`preflight(hmc, packet, output)`, `capture(hmc, packet, output)` and
`recover(hmc, packet, journal)` signatures. Existing dispatchers remain
`live_test_preflight.py --group vnic-creation-policy`, `live_vnic_creation_policy.py`
and `live_test_recovery.py --vnic-creation-policy-packet`. Version 2 is implemented;
[the operator procedure](../../live-testing.md#source-only-vnic-update-qualification-version-2-1828)
documents these installed entrypoints and their separate admission requirements.

No owner move, new public API, generic driver or wrapper is needed. Reuse
`vnic.add_vnic`, `vnic.remove_vnic`, `creation_policy`, `observed_policy`,
`verify_policy`, the fixed inventory readers, `build_attribute_record` and
`transport.run_hmc_command`. The six single-attribute records need no nontrailing
quoted opt-in and no change to shared grammar or creation admission.

Keep separate internal source validation/capture/history branches. Version 1
continues through its current branch, including its result fields and budgets.
Version 2 cannot fall back to version 1 after an error. Existing creation-success
rendering and evidence/maturity projections must reject source-only history.

## Exact packet

Accept exactly these 20 keys. Strict integers exclude booleans. Reject duplicate
or unknown JSON keys, nonfinite values, null required cells and alternate spellings.
Preserve private regular-file/no-symlink checks, 1 MiB packet bound and the
entrypoint's caller-supplied SHA-256 of exact packet bytes. The result's existing
`packet_sha256 = digest(packet)` is a separate canonical-object binding, not the
raw-byte hash; retain both roles without relabeling one as the other.

| Key | Constraint |
| --- | --- |
| version | strict integer 2 |
| purpose | exactly `update-mode-source` |
| case | one fixed case from the table below |
| configured_profile | nonempty printable string; no credential in packet |
| hmc_identity | exact `{host, port}`; nonempty printable host, integer 1..65535 |
| expected_commit | 40 lowercase hex; exact clean source checkout/import |
| system_uuid, lpar_uuid | UUID strings; existing contained owned fixture |
| expected_lpar_id | strict positive integer |
| owner_token | nonempty printable caller token |
| expected_lpar_state | nonempty printable exact `PartitionState` observation |
| selector | exact string fields `vios_name`, `vios_lpar_id`, `adapter_id`, `physical_port_id`, `capacity_percent`; existing selector/granularity validation |
| port_vlan_id | strict integer 0..4094 |
| policy | exact four fields described below |
| baseline_sha256 | null only for initial preflight, otherwise 64 lowercase hex |
| expires_at | strict positive integer Unix expiry; no automatic extension |
| exclusive_writer_window | exactly true; coordination, not an HMC lock |
| allow_one_create, allow_one_update, allow_captured_slot_cleanup | each exactly true |

The four policy fields are `pvid_priority` integer 0..7,
`auto_priority_failover` strict bool, `allowed_vlan_ids` singleton integer list
1..4094 and `allowed_os_mac_addrs` singleton 12-hex MAC list. Reuse canonical
normalization; no duplicate/member-count rule is borrowed from Ethernet.
Do not accept `mac_addr`: HMC generates it and independent readback captures it.
No caller slot, command, oracle override, case array, retry, force or LPAR-setup key.

| Case | Fixed record | Expected raw selected cell |
| --- | --- | --- |
| vlan-replace-all | `allowed_vlan_ids=all` | `all` |
| vlan-replace-none | `allowed_vlan_ids=none` | `none` |
| vlan-remove-last | `allowed_vlan_ids-=<baseline singleton>` | `none` |
| mac-replace-all | `allowed_os_mac_addrs=all` | `all` |
| mac-replace-none | `allowed_os_mac_addrs=none` | `none` |
| mac-remove-last | `allowed_os_mac_addrs-=<baseline singleton>` | `none` |

The remove-last result is a hypothesis, not an established native rule. Positive
qualification requires the literal cell shown; empty/missing/malformed remains
unavailable, and another readable representation is mismatched. Keep raw cells.
The exact lower-case literal requirement is stricter than the reusable canonical
parser's case folding and is deliberate for these candidate source observations.

## Algorithm and preservation

1. Initial read-only preflight validates packet, candidate/import, destination,
   containment, owner, state and existing creation eligibility. Capture B0 and
   emit its digest. Root binds it into the final packet; final preflight must
   independently reproduce B0. No code rewrites packet or baseline.
2. Capture repeats B0 and eligibility. Journal one exact create intent at the
   physical boundary before dispatch. Reuse `add_vnic` with explicit singleton
   policies and scalars. Require dispatched, changed, no errors, exactly one new
   slot/backing and verified complete creation policies. A no-write match fails.
3. Take fresh B1 (reuse the current pre-cleanup snapshot). The existing creation
   cleanup proof must match the captured create result and B0 foreign projection.
   Additionally require complete observed PVID, all four baseline policy cells,
   generated MAC and one active Operational backing. Capture slot from this proof.
   Authorize the independently read ownership description using the existing
   `authorize_lpar_ownership_description(..., operation="lpar-mutation")` owner.
4. Derive the fixed pair from case and canonical singleton. Build it with
   `build_attribute_record`, default grammar guard, then quote the whole record
   and contained names/slot for one native `chhwres ... -o s ... -a` submission.
   Arm a single exact update token; persist intent, consume it and recheck expiry,
   count reserve and remaining time at actual dispatch. No alternative encoding.
5. Take B2 independently after completed, rejected or unknown submission when
   bounded reads remain. Classify submission, selected policy and preservation
   independently. Retain the contained selected row and selected-policy axis before
   backing/direct reads; a later preservation failure leaves policy evidence intact
   and preservation unavailable. Read failure cannot imply rejected or unchanged.
6. Only a qualified case may arm removal. Require B2 to equal B1 except the one
   selected raw policy cell, now equal to the fixed oracle. Validate the complete
   captured identity, generated MAC, backing/capacity/state and foreign projection.
   Derive expected post-update object from B1 plus precisely that cell; do not
   pass old creation policy unchanged into cleanup or mask the full policy object.
   Reuse `remove_vnic`, retaining its own contained ownership/environment/backing
   checks. A source-local scoped guard compares those already-performed fresh
   vNIC/backing, contained REST fixture identity/state and native ownership reads
   to qualified B2 before the physical remove token is consumed. Persist those
   bounded observations with the intent; recovery recomputes their agreement.
   An observed discrepancy disarms removal. No extra read is added. Compare the
   removal result's vnic_before, backing_before and selector to B2 in capture and
   recovery. The residual unobservable race after the last read remains accepted.
7. Require acknowledged complete removal, fresh B3 equal to B0, captured slot
   and backing absent, and acknowledged own logout. Persist final source history.
   Any failed predicate holds later effects and subsequent windows.
8. After process termination, independently run read-only recovery with original
   packet/history. It can run after expiry; it never writes hardware or retries
   cleanup. Root alone dispositions residue and admits another packet.

B0/B1/B2/B3 use the existing snapshot's target-partition raw vNIC rows,
managed-system backing rows and direct logical-port rows on the selected adapter,
with exact contained identity/owner plus version-2 `PartitionState` in identity.
The state must exist in both agreeing contained REST observations and equal the
packet. This is bounded preservation, not all foreign partition policy or whole
machine state. Exact row projections are compared, so changed raw unselected
cells, duplicate identities and incomplete required observation cannot be ignored.
The captured vNIC's PVID must parse in 0..4094, its policy cells must be available,
and its generated MAC must be a valid 12-hex observation before update.

## Results and recovery

Version 2 retains private lifecycle fields and adds `version=2`,
`purpose=update-mode-source`, `evidence_class=source-only`, and exact `case`.
Its `source` object contains captured_slot, before_update (B1), native_record,
expected `{field,value}`, submission, remote_refusal_code, selected_after (the
independently contained selected raw row), after_update (complete B2),
policy_verification, preservation and qualification. Missing evidence is retained
with an error; no success is reconstructed from requested data.

| Axis | Values and evidence |
| --- | --- |
| submission | `not_dispatched`, `completed`, `rejected`, `unknown`; zero native exit acknowledges completed; rejected requires structured `HMCCLIError.remote_nonzero_completed` and `remote_refusal_code`; all other dispatched errors are unknown |
| policy_verification | `verified`, `mismatched`, `unavailable`; independent B2 selected cell only |
| preservation | `unchanged`, `drifted`, `unavailable`; complete bounded projection with only the selected cell allowed to differ |
| qualification | `qualified` only completed + verified + unchanged; `refused` only structured rejection + full B2 equality to B1; `not_attempted` only no physical dispatch; otherwise `unresolved` |
| lifecycle | READY only valid preflight; CLEAN only qualified source + verified guarded removal/B0 restoration/session closure; otherwise MANUAL_RECOVERY; SKIP is preflight eligibility only |

Qualification remains factual if later cleanup fails; the lifecycle remains held.
An intent consumed before a failed expiry/journal check is not physical dispatch.
If only an intent survives a crash, recovery treats completion as uncertain.

Recovery recomputes the case/pair/oracle from the packet and captured creation
proof, checks exact discriminators, candidate and packet binding, complete history,
unique ordered intents `[create, update, remove]` and their persisted exact commands,
original acknowledged session
closure, qualified B1/B2 comparisons and captured identities. Re-run the creation
proof on B1; re-run the selected-cell cleanup and removal-before-state proofs on B2;
require selected_after to agree with B2 and require successful add
and remove result identities/flags/errors, B3/B0 equality and independent current
B0 equality/absence. Compare serialized derived evidence with recomputation.
Its own login/logout must also complete. A forged label, removed history field,
duplicated/reordered intent or current absence alone cannot yield CLEAN.

## Proposed finite bounds and admission proof

These are design candidates, not verified ceilings or operational grants.
Retain version-1 limits. Version-2 limits are per invocation:

| Stage | REST requests | SSH calls | elapsed seconds |
| --- | ---: | ---: | ---: |
| initial or final preflight | 24 | 24 | 120 |
| one capture | 48 | 56 | 300 |
| independent recovery | 24 | 24 | 120 |

Count at `HMCClient._request` and `transport._run_hmc_command`, including session
and failed attempts, before dispatch. REST permits GET and the one own login/logout
only; SSH writes require exact one-shot tokens. No REST write fallback, session
renewal, port fallback or mutation replay. Existing `_request` sends once and
`_get` does not reauthenticate. Read-only ManagedSystem null-property fallback
and empty-feed state checks remain bounded and counted, not generalized retries.

The source graph predicts 23 REST/50 SSH on ordinary capture: four snapshots at
3/4, two creation preflights at 3/11, add write/readback at 0/3, one update at 0/1,
remove including its checks/readback at 3/8, and login/logout at 2/0.
Each snapshot can add two REST requests for ManagedSystem null-property fallback
and one empty-feed check (then containment fails); the three ownership resolutions
can add one empty-feed check or one SSH name fallback. A conservative additive
envelope is 38 REST/53 SSH, beneath the proposed cap; unreachable combinations
must not be advertised as measured paths. Preflight/recovery inherit larger
count margins. Offline physical traces must verify each expansion and error path
against these candidates before root can accept a final budget.

Reserve one REST request and the final 15 seconds of each stage solely for its own
logout. Non-logout I/O uses the earlier body deadline. At a source write, require
at least 15 body seconds remaining after durable intent persistence. This is a
finite observation opportunity, not a promise that remote work finishes in time.
Require remaining physical budgets after consuming that write's count to cover
the proposed suffix reserves: create 24 REST/26 SSH, update 17 REST/18 SSH,
remove 7 REST/6 SSH. Recheck after persistence latency. An insufficient reserve
refuses that write; later cleanup remains disarmed. Recovery has its own complete
24/24/120 allowance and is mandatory even when capture exhausts its allowance.

Keep 1 MiB packet/per-response/SSH-output and 5 MiB atomic journal caps. Retain
bounded raw parsed observations privately; journal overflow/failure before a write
refuses that write and preserves the preceding durable journal. Large valid
inventories can therefore be ineligible. Final overflow is MANUAL_RECOVERY,
never CLEAN. No silent truncation or compressed-away preservation evidence.

## Failure model

- **Actors and deployments:** local authorized operator and root-controlled
  separately admitted HMC source window; offline CI transports. No public service.
- **Invariants and assets:** exact owned fixture/captured vNIC, foreign configuration,
  capacity, one attempt per effect, bounded sessions/output and truthful evidence.
- **Accepted failure classes:** remote uncertainty, timeout, concurrent external
  changes and insufficient evidence can prevent qualification and leave residue;
  bounded fail-stop plus independent read-only recovery is the required outcome.
  Writer acknowledgement does not serialize uncoordinated external actors.
- **Covered elsewhere:** fixture preparation/root; backing failover #669; system
  defaults #680; profile CRUD #1724/#637; platform secret handling and TLS existing
  clients; traffic effectiveness is outside stored-policy evidence; final product update design and later modes remain #1828.

## Threat model

- **Boundaries:** widened packet/history parser and source-case discriminator;
  existing local private journal, credential-bearing connection, REST identity and
  native observations, and derived HMC command record. No new network listener.
- **Actors:** local operator supplies packet/history; remote responses can be
  malformed or stale; an uncoordinated writer can change inventory. Trust the local
  process/code and authorized operator, not observations as mutation authority.
- **Controls:** strict schema/byte bounds/private paths; exact candidate/import,
  destination and owner binding; native+REST containment; fixed case grammar and
  shell quoting; exact physical tokens and durable intent; independent readback
  and suffix reserves. Keep raw errors/identifiers in private results. Procedure-owned
  output reports source-only stage/status and error type, never private payload.
  Inherited SDK audit/security diagnostics can contain private identifiers; retain
  all terminal captures privately and preserve mandatory audit emission.
- **Outside this design:** malicious same-user rewriting of code/journal, hostile
  kernel and uncompromised-HMC guarantees; existing platform trust. No claim of
  cryptographic journal authenticity or atomic HMC compare-and-swap is made.

## Success and verification admission

The six-case source owner must prove strict input rejection before effects,
one-case/one-create/one-update/qualified-only-remove behavior, complete independent
axes, unchanged v1 behavior and read-only historical recovery. Focused tests must
bite under controlled faults, including journal latency crossing expiry or body
deadline with a deterministic injected clock, not a host-clock race.

Offline verification covers every case and error boundary listed in the plan,
physical count expansion/suffix reserves, response/journal caps and original/own
logout failure. Run relevant guardrails and pinned hooks; no live evidence follows
from offline success. Before hardware, root approves exact prepared identity/state,
selector/values, implemented clean commit/import, proven caps, expiry and effect
window. No current packet or executable invocation is supplied here.

Local source evidence: `scripts/live_test/vnic_creation_policy.py:77-606`,
`src/hmcpctl/operations/virtualization/vnic.py:418-848`,
`vnic_policy.py:35-118`, `src/hmcpctl/ssh/commands.py:112-202`,
`src/hmcpctl/client/core.py:622-807`, `client_systems.py:138-200` and
`client_lpars.py:114-135`. A focused local reference search found the existing-vNIC
fields and VLAN `+=` nontrailing example at
`docs/refs/hmc-commands-p11/commands/chhwres.md:241,289-292`; it did not establish
update sentinel/clear or two-list semantics. No reference prose is copied here.

## Retained-fixture integration correction

The retained-fixture subcycle also corrects the existing source description
comparison boundary: extract attributed REST leaf text and remove at most one
native LF/CRLF record terminator. Preserve semantic ownership/caller checks,
raw transport return values and unknown resource fields. Malformed leaves,
multiple records and changed text still refuse. The fixture design and its
verification inventory cover bare/attributed and native-ending lifecycle controls;
this does not change source-v2 qualification semantics or limits.
