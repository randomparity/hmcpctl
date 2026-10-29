# N1 client Ethernet creation specification

Status: accepted design for #1821; implementation authorized.
Baseline: b305024c6f5172be7a86e3d2a8a69999cf4367e5.
Authority: https://github.com/randomparity/hmcpctl/issues/1821#issuecomment-6494394191.
Lane: full-spec. Complexity: M. Fixed design denominator: 250.

## Problem

The existing client Ethernet creator omits six documented creation inputs.
It also returns the PUT response without independently observing the selected values.
This change extends that coherent owner, rather than adding a native proxy.

## Scope

Add addl_vlan_ids, allowed_os_mac_addrs, qos_priority, vsi_manager_id,
vsi_type_id and vsi_type_version to existing client Ethernet creation.
Preserve existing parameters, positional calling contracts and omitted-input behavior.
Retain top-level resource identity and change_location in existing presentations.
Running updates remain N2; trunk/server contexts remain retained #638 work;
vNIC adapter policy is N4, backing updates/failover #669, multiple-backing creation N5,
and system defaults #680/A9. These are separate owners, not completed requirements.
C04 and C12 gaps and ledger totals remain unchanged.

## Global Constraints

Python >=3.11. No new dependencies or toolchain floors.
Use existing ownership, target authorization, XML escaping and UUID conventions.
Implementation and normal shipping gates are authorized; hardware execution is separate.
Public evidence must redact machine, network and account identifiers.
Do not change the stable hmcpctl.api facade exports.

## Inputs

Expose the six inputs as keyword-only additions to the operation and builder/client seams.
MCP and CLI adapt to the same operation contract; no new settings object.
CLI list inputs use JSON arrays; all/none policy tokens remain literal strings.
addl_vlan_ids is list[int] | None: None omits; [] is an explicit empty list.
Selected VLANs must be non-bool integers 1–4094, at most twenty entries.
Require tagged=True for a nonempty additional VLAN list; do not silently enable it.
Preserve caller order and do not silently remove duplicates or PVID overlap.
HMC validates remaining topology relationships, including duplicates and PVID overlap.
allowed_os_mac_addrs is list[str] | Literal["all", "none"] | None.
A selected MAC list has one to four 12-hex addresses without separators.
Normalize new policy MACs to uppercase for XML and compare MAC values case-insensitively.
All/none serialize as uppercase policy tokens. None omits, not implicit all.
qos_priority is int | Literal["none"] | None: integers 0–7, bool rejected.
None omits; numeric zero is enabled priority zero; explicit none disables QoS.
Each VSI input is int | None; reject bool and values outside signed 32-bit representation.
Do not invent narrower semantic ranges or require all three fields together.
HMC validates undocumented VSI semantics and creation eligibility.
Validate selected inputs before ownership/resource reads or mutation.

## XML mapping

Reuse build_client_network_adapter_document and its escaping decorator.
Preserve existing omitted-field output bytes when six new inputs are absent.
AllowedOperatingSystemMACAddresses follows inherited adapter fields, before MACAddress.
QualityOfServicePriority follows PortVLANID; its enable flag follows priority.
TaggedVLANIDs precedes TaggedVLANSupported.
VSI manager/type IDs follow tagged support; VSI type version follows VirtualSwitchID.
Numeric QoS emits priority plus enabled=true; none emits enabled=false without priority.
VSI fields serialize integer text. VLAN/MAC collections serialize space-separated values.
Use captured create annotations; no inferred native-field-to-XML spelling.
Schema evidence is retained V9R2; published IBM implementation corroborates encoding.
Neither establishes revision-matched creation qualification on other HMC levels.

## Creation and independent observation

The legacy path remains unchanged when six new inputs are omitted.
For a selected-input request, resolve/authorize the partition through the existing owner.
Read change_location through its existing owner and list contained client adapters once.
Before mutation, require baseline entries to have valid unique canonical UUID identities
and expected ClientNetworkAdapter type; unavailable baseline refuses before effects.
Submit one existing create_child PUT. Preserve actionable backend exceptions and uncertainty.
Do not retry, infer idempotency, or delete to compensate for failure or uncertainty.
After an acknowledged create, validate response UUID and resource type.
List ClientNetworkAdapter under the resolved partition once, independently of the PUT.
Require exactly one matching canonical UUID, absent from baseline, for verified creation.
Compare UUIDs case-insensitively; never select by VLAN/MAC alone or first-list position.
Do not follow a response-provided URL for verification.
Missing/malformed identity yields unavailable verification; no new-child inference.
Use leaf_text and strict field parsing. Repeated/malformed fields are unavailable.
Compare only selected new fields; no defaults or omitted-field assertions are invented.
Compare VLAN/MAC sequences in supplied order after lexical normalization.
Enabled QoS requires true plus exact numeric priority; disabled requires false.
Disabled verification makes no assertion about any retained priority scalar.
VSI fields require exact selected integer values.
A failed post-create GET does not imply a failed create or absence of effects.

## Results

Retain the create response resource and change_location at their current locations.
For selected-input requests, add creation_verification with exactly four fields:
status: Literal["verified", "mismatched", "unavailable"],
checked_fields: list[str], mismatched_fields: list[str], unavailable_fields: list[str].
Field lists contain only the six fixed input names, in interface declaration order.
checked_fields lists selected inputs, including unavailable fields; it does not assert comparison succeeded.
For unavailable identity, mark selected fields unavailable; do not fabricate mismatches.
Known valid field mismatch outranks unavailable evidence; both inventories remain visible.
Verified requires new contained identity and empty mismatch/unavailable inventories.
No selected new fields: omit new metadata and preserve historical behavior.
Keep vSCSI/vFC results unchanged. CLI must not print unqualified success for unavailable
or mismatched selected values; show creation acknowledgement and verification separately.
For selected requests, CLI exits 0 only when verified and 1 when mismatched/unavailable,
with an explicit warning that creation may have occurred and must not be retried blindly.
The metadata claims stored configuration, not traffic delivery, runtime QoS, external VSI
registration or independently verified saved-profile content.

## Success

C1: six selected inputs reach the existing creation owner with approved validation/mapping.
C2: omitted-input calls preserve existing document, invocation and result behavior.
C3: ownership, containment and ordinary authorization remain enforced.
C4: selected-field verification distinguishes verified, mismatched and unavailable evidence.
C5: backend refusal/uncertainty and observation failure trigger no retry/automatic cleanup.
C6: CLI/MCP describe and render the contract truthfully, with generated docs refreshed.
C7: negative/postcondition tests, guardrails and an exact future live procedure are delivered.
Unrun hardware behavior remains unevidenced; live execution is not a merge gate.

## Failure model

Actors and deployments:
- Local Python/CLI operators and authorized MCP clients using an existing HMC connection.
- HMC responses may be incomplete, malformed or concurrent with another administrator.
Invariants and assets at stake:
- Partition/network configuration, ownership authorization and truthful effect reporting.
- Existing Python invocation and top-level result contracts.
Accepted failure classes:
- HMC rejects undocumented topology/VSI/release eligibility: actionable native refusal.
- Concurrent writer or missing identity prevents a verified result: report unavailable.
- Post-read cannot attribute unrelated changes: no global preservation or exclusive-writer claim.
- Unrun hardware arms establish no live qualification, under ADR 0212.
Covered elsewhere:
- Connection/TLS/HTTP bounds: existing HMCClient transport and configuration owners.
- Saved-profile lifecycle/equality: #1724; adapter running updates: N2.
- Broader retained families: approved #638 routing packet.

## Threat model

Widened boundaries: typed caller fields into authorized creation; HMC XML into verification;
new metadata into CLI/MCP output. No new endpoint, credential or permission is introduced.
Untrusted actors: malformed local/MCP inputs and server-controlled resource content.
Trust existing credential configuration and operator-approved ownership override policy.
Controls: strict field types/counts; existing XML escaping and target authorization;
parent-anchored reads; UUID shape/case convention; leaf normalization and strict parsing;
fixed-name public metadata with no raw exception/response echo added.
Out of scope: compromised credentials/HMC and malicious authorized administrators; existing
transport/authorization policy owns those risks. No new global session journal is proposed.

## Validation

Focused tests cover C1–C7, including omitted/empty/zero/sentinel distinctions,
VLAN and MAC bounds, bool rejection, VSI representational limits, schema ordering,
QoS paired fields, authorization denial, baseline identity failures and no-write proof,
UUID case/foreign/preexisting/duplicate identities, missing/repeated/attributed fields,
known mismatch mixed with unavailable evidence (including checked_fields membership), failed GET and native error propagation.
Prove new high-risk regressions with controlled faults, reverting only those faults.
Run relevant focused tests and static gates per task, then just verify and pinned hooks.
CI must pass supported Python legs; do not infer CI success from local Python alone.
Prepare live procedure through existing preflight/named-arm/recovery workflow, with bounded
fixture/selected values and cleanup authorization stated separately before a future run.

## Approved ST9 coverage amendment

The operator approved the bounded existing-ST9 extension recorded below.
Its six fixed request values, independent field/identity proof, strict metadata and existing
manual/_Stop control flow are part of C7. Only network.py, its existing offline test module
and the runbook are added to the file map. No new arm/operator interface, activation,
second adapter create, retry, automatic cleanup after uncertainty or hardware run is granted.

## Bounded proposal

Extend the existing ST9 client-network creation round trip, not add a new arm or wrapper.
Keep one existing inactive-partition adapter create, the same owned PVID/switch selection,
and current preflight/named-arm/read-only recovery workflow. Select the six fields in
that one request: addl_vlan_ids=[], allowed_os_mac_addrs=all, qos_priority=none,
vsi_manager_id=0, vsi_type_id=0 and vsi_type_version=0. These are explicit test values,
not asserted defaults or a claim HMC accepts this combination.

Require independent contained adapter identity plus product creation_verification=verified
and all six checked_fields before crediting the positive row. A valid refusal, absent field
or unsupported combination is failed/unevidenced qualification, not success. A missing or
uncertain create/verification result stops later effects and automatic adapter deletion;
record manual-recovery-required residue and still run mandatory read-only recovery. No
retry, activation, second create, extra network creation or VSI service configuration.
For verified known fixture creation only, carry that canonical create-response UUID into
network-only cleanup. Do not call the generic baseline-difference remove_new_adapters helper
for this scenario and never discover additional deletion targets from the delta.
Before deletion, independently require the baseline placements plus exactly that fixture,
with unchanged fixture placement and selected six fields. Unexpected drift or unreadable
evidence takes manual/_Stop before further writes. Delete only that verified UUID once.
Require acknowledged deletion and independent restored baseline before later mutations;
unknown deletion or unexpected residue takes manual/_Stop. A concurrent unrelated UUID
that appears before or after this read is never a delete target. Leave vSCSI/vFC cleanup
unchanged and report its adjacent risk separately. Hardware remains separately authorized.


## Approved completeness and switch correction

Existing ClientNetworkAdapter listing uses the same strict complete Atom inventory reader
as selected-input creation proof. A positive empty feed returns []; missing bodies,204,
pagination or ambiguous raw Atom entries raise HMCError. No new selector or result shape
is added. Other adapter kinds retain list_child behavior; omitted-field creation is unchanged.
ST9 requires readable VirtualSwitchID equal to its selected switch during initial fixture
proof and all later pre-delete observations. Switch-only drift or unavailable switch evidence
records manual recovery and stops before deletion or subsequent writes. Preserve complete
listing failure through the existing tool result into ST9; never equate missing HTTP bodies
with restored empty inventory. Positive empty complete inventory can prove restoration.
Use actual HTTP transport fixtures and offline harness boundary regressions, including empty
initial inventory, post-delete204/blank200, and switch-only changes. No hardware run.
