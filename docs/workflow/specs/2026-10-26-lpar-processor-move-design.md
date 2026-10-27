# LPAR processor move design

Status: proposed for independent design review; not approved for implementation.
Issue #1779 (D06), frozen scope q1779-bcca232d, dependency #1778 landed at
2fa42542; reconciled source base 1aad137647baa771497870e01fbc875149666140. Full-spec, M250.
Authority is the unchanged external charter in issuecomment-6408808071; exclusions are empty.
The external charter governs scope; this specification proposes implementation contracts.
[ADR 0300](../../adr/0300-lpar-processor-move.md) remains proposed.

## Problem and ownership

Expose one native two-partition processor move through a typed async operation, CLI and MCP.
The existing documents/lpar_processors.py owns exact processor quantities; ssh/processors.py
owns fixed native processor grammar and bounded native reads; operations/lpar/processors.py
owns reconciled REST/native quantities, state, bounds and current/profile/runtime reporting.
A small operations/lpar/processor_move.py owns the coupled two-endpoint decision and outcome.
Existing single-partition setters, creation/provisioning, profile lifecycle and sharing policy
keep their owners. No facade export, generic move framework or arbitrary command proxy is added.
Memory move supplies an orchestration precedent; its memory rules are not processor evidence.

## Native source contract (C1)

Exact selectors: docs/refs/hmc-commands-p10/commands/chhwres.md:142 and
 docs/refs/hmc-commands-p11/commands/chhwres.md:133, with their complete processing subsections.
Both define resource proc, operation m, one managed system, source --id, destination --tid,
optional --procs / --procunits, and optional --force. Target is required for a move.
Quantity/applicability evidence: P10 lines 226–227; P11 lines 230–231.

| Source → destination | Native quantity | Established meaning |
| --- | --- | --- |
| dedicated → dedicated | --procs N | N whole dedicated processors transferred |
| shared → shared | --procs N and/or --procunits U | whole virtual processors and/or hundredth-exact entitlement transferred |
| dedicated → shared | --procs N only | whole dedicated capacity converted to shared capacity |
| shared → dedicated | --procunits U only, integral U | shared capacity converted to whole dedicated processors |

All four documented pairs remain in scope. Do not silently remove cross-mode moves.
No version floor is inferred from publication family, existing fixtures or absent evidence.
Decimal values must remain finite, nonnegative and exactly representable in hundredths;
use bounded_processor_number, retain StrictInt/StrictBool request semantics and bounded rendering.
An empty quantity request is rejected. Explicit zero follows the existing guarded no-op convention:
complete admission and fresh evidence, then zero submissions, rather than sending a native no-op.
Whether zero is supplied cannot make an otherwise inapplicable cross-mode flag valid.

## Identity, authorization and concurrency (C2–C3)

Resolve one explicit managed system, then both endpoints in that system's LPAR and VIOS feeds.
Reject blank, absent, ambiguous, cross-system, same-UUID or same numeric-ID endpoints.
Compare UUID identity case-insensitively while preserving validated spellings in URL arguments
(ADRs 0094 and 0160). Compare resource kind, numeric ID and CLI name against native readback.
Resolve the system CLI name back to the same UUID so a duplicate name cannot redirect effects.
MCP policy metadata declares source and destination target selectors; operation-level ownership
checks run independently for both names. An ownership override does not bypass policy,
containment, identity, quantity, state, capacity or readback checks.
Re-read decision evidence for both endpoints and system, compare identity/state/mode/current,
pending/runtime quantities, limits and relevant capability observations, and repeat ownership
checks immediately before the one native submission. A changed decision yields no submission.
No ETag is sent to chhwres: this comparison is optimistic observation, not a lock or transaction.
Other actors can race the final read; classify the actual poststate without causal certainty.
Submit at most one native command. No retry, paired add/remove emulation or automatic rollback.

## Admission and capacity (C2)

Reuse the D05 snapshot parser for bounded REST/native agreement and memory/profile observations.
Separate mode-specific target derivation from the single-partition _targets growth rule.
For a conserved transfer, destination capacity does not require preexisting unallocated units
of the same amount: source capacity funds the transfer. Check source floors, destination
current/pending maxima, system total, applicable OS/count limits and reported entitlement-per-vCPU
bounds. For mixed-mode shared endpoints, calculate a feasible integer vCPU interval from the
current/pending envelopes and OS/system limits; expected entitlement must admit at least one
count in that interval. Do not freeze the unselectable vCPU count at its current value.
For a zero current destination envelope, use the reported pending envelope and verify the
resulting current envelope against it; source reductions may not cross either applicable floor. Native firmware owns remaining pool reservations, IBM i aggregate licensing/capacity,
guest constraints and races; native refusal still requires dual readback.
A below-minimum shutdown source risks clearing CPU and memory; do not reinterpret this as a
successful conserved move. Keep source/destination memory and current envelopes in postconditions.
P10 chhwres:206/P11 matching description documents shutdown remove/reset coupling; it does not
by itself prove inactive move eligibility. HSCL29xx:7–45 independently identifies both endpoint
processor DLPAR capabilities and source/profile minimum/destination maximum limits.
P10 chhwres:236/P11:239 supports no-RMC force for AIX/Linux/VIOS move and requires a restart.
Do not claim runtime effect for forced no-RMC endpoints; retain a per-endpoint warning.
Running normal endpoints need reported dynamic processor capability and active RMC where applicable.
Force applicability is evaluated per affected endpoint; a mixed-OS pair must not lose the
unforced endpoint's capability checks. Stable inactive endpoints may reach native admission under the D05 current-configuration model;
this is not a promise that firmware accepts inactive moves. Native rejection is retained truthfully.

## Readback and result (C3–C4)

After an acknowledged command, exit failure, timeout or transport error, attempt both endpoint
reads independently. A failed source read must not skip the destination read, and conversely.
Each endpoint retains before, after-or-null, expected, read_error, submission_error,
change_location and warnings. Bounded error text must not replace the peer's usable evidence.
Compare identity/kind/mode/state, requested quantities, memory coupling and applicable runtime
quantities. Current/profile sync warnings are independent for each endpoint.
Use existing outcome vocabulary applied/noop/rejected/partial/unverified and workflow_completed.
Completed requires both endpoint contracts to hold; one matching endpoint is insufficient.
Observed incomplete changes are partial; missing/untrusted peer evidence is unverified.
An explicit native nonzero exit plus two unchanged complete reads can be rejected.
A transport failure plus unchanged reads stays unverified; it is not proof of rejection.
An error followed by two matching poststates can report applied with submission_error retained;
that means the state was observed, not that this command uniquely caused it.

## Selected deltas and observed quantities

For dedicated→shared --procs N, require source dedicated count minus N and destination
shared entitlement plus N. For shared→dedicated --procunits U, require source entitlement
minus integral U and destination dedicated count plus U. These are the documented conversion
quantities. Independently read and report the shared endpoint's actual current/pending/runtime
vCPUs, validate its reported envelopes and applicable per-vCPU capacity constraints, and require
runtime agreement with actual current quantities when the endpoint is not deferred.
The retained source does not specify a mixed-mode virtual-count delta. No unchanged or one-for-one
vCPU rule is asserted. Report processors in observed_only_quantities for that shared endpoint;
verified_quantities lists the selected dedicated count or shared entitlement whose exact delta
was checked. This preserves C4's dual actual-quantity verification without inventing an unselectable
input or guaranteeing unrequested virtual-count preservation. This remains a proposed
completion contract for ordinary independent design review and scope audit.
For shared→shared, selected count/entitlement deltas are exact and omitted quantities retain their
current values, matching the landed D05 independent-quantity convention. A native ancillary change
outside that proposed contract is reported as partial rather than silently called complete.
Known identity/mode/envelope/memory changes invalidate completion; a complete readable state with
wrong selected deltas is partial. Missing or internally inconsistent required evidence is unverified.
No additional native probe is required merely to infer a universal unselected mixed-mode vCPU delta.
The actual live scenario records precisely which modes/states it exercised; other combinations
remain implementation coverage without fabricated live verification.

## Failure model

1. Actors and deployments:
   - Authorized local CLI operators, authenticated MCP clients and async library callers using HMC.
   - AIX/Linux, IBM i and VIOS endpoints on one selected operating managed system.
   - Mocked CI validates application behavior; it is not firmware or running-guest evidence.
2. Invariants and assets at stake:
   - Both endpoint ownership/policy boundaries, stable target identity and CPU/memory availability.
   - Exact numeric quantities, truthful partial effects, endpoint runtime/profile distinction.
3. Accepted failure classes:
   - Remote refusal despite local admission: firmware/guest constraints remain native authority.
   - External races and uncertain effects: no native CAS or proven rollback; expose uncertainty.
   - Missing poststate: preserve known peer observations and require operator inspection.
4. Covered elsewhere:
   - SSH authentication, transport and response-byte enforcement: existing SSH transport owner.
   - Global policy evaluation and connection selection: existing tool/CLI policy machinery.
   - Profile lifecycle, pool changes and sharing policy: their existing owners; no D06 exclusion.

## Threat model

- Added boundary: CLI/MCP two-endpoint selector and typed-quantity request to processor move.
  Local callers can supply wrong identity, unsupported flags, extreme numbers or foreign targets.
- Widened boundary: typed native command submission to HMC; use numeric IDs, quoted system name,
  fixed switches, exact finite bounded numbers and one bounded response. No free-form attributes.
- Controls: policy checks both targets; ownership checks both independently; explicit containing
  system; native and REST identity agreement; bounded read/render and useful bounded errors.
- Trust: configured HMC transport and authenticated native/REST source are trusted observations,
  not an atomic snapshot. A compromised HMC is outside the existing client trust boundary.

## Validation and live proof (C5–C6)

Domain cases: four mode pairs; missing/invalid/zero quantities; extra fields; bool/type errors;
hundredth boundaries, huge/exponent values and low ambient Decimal precision; unsupported flags;
same endpoint with different UUID case; wrong system/kind/name/ID; independent source/destination
ownership and policy denial; drift at each read; bounds; no available-pool false rejection;
source reset hazard; capability/state/RMC/force; one send; lost acknowledgement; native rejection;
source-only, destination-only, wrong-size and coupled-memory changes; both read failure orders.
Presentation cases: typed argument flow, required system, escaped confirmation, --yes,
nonzero CLI exit for incomplete workflows, and registered exhaustive two-target policy metadata.
Generated capability, maturity, tool docs and declared scenarios must match actual handlers;
no live observation is fabricated and no entire mode/state matrix is certified from one case.
ST61/group lpar-processor-move/wrapper scripts/live_lpar_processor_move.py/scenario
st61-lpar-processor-move is reserved. The named arm must record two independent baselines,
checkpoint intent before effects, stop writes on uncertainty, and require read-only recovery.
A reverse move is a separately admitted planned effect after complete forward proof, not rollback.
Creation/provision behavior is not changed. A root source probe and the eventual product arm are
separate evidence with separate authority; the initial live arm uses two existing running AIX/Linux shared-processor fixtures, active RMC,
0.10 entitlement headroom in both directions and no force, with both actual vCPU vectors retained.
The arm performs one forward 0.10-unit transfer and a separately admitted reverse transfer only
after complete forward proof; both original full baselines must be restored for cleanup to pass.
This one reproducible case does not certify cross-mode, dedicated, inactive, force, IBM i or VIOS.
Final shipping requires just verify and uv run --no-sync prek run --all-files, plus root-owned CI.
No implementation, generated artifact, test, import, build, setup or live action ran in this lease.

## MCP tool-use evaluation

AI-SPEC: an authenticated MCP client supplies explicit source, destination, managed system and
one typed move. The tool uses only contained REST/native observations, returns both actual
endpoint results, and does not infer omitted identities or claim an unverified effect completed.
It makes at most one native submission and two post-observation attempts; no model call, LLM
judge, autonomous replay or agent loop is added. Existing transport timeouts/byte limits bound
remote work; a latency SLA is not claimed. Success is two satisfied endpoint contracts.

| Case | Severity | Observable pass / forbidden result | Gate |
| --- | --- | --- | --- |
| E1 four-mode request | 4 | exact selected deltas on both peers; no omitted-peer success | block |
| E2 ambiguous/missing system or endpoint | 5 | refusal before effect; no guessed target | block |
| E3 destination policy/ownership denial | 5 | zero submissions; source grant cannot replace destination grant | block |
| E4 stale decision evidence | 5 | zero submissions; stale snapshot cannot authorize mutation | block |
| E5 lost acknowledgement/read failure | 4 | dual attempts, one send, truthful uncertainty; no replay | block |
| E6 mixed-mode vCPU | 4 | actual counts/bounds disclosed; no invented unselected delta | block |

Task A's named domain cases and Task B's schema/policy/presentation cases provide deterministic
code-based measurements. No real observed regression fixture is available for D06 yet; none is
invented. A separately authorized ST61 run measures only its declared live case.
