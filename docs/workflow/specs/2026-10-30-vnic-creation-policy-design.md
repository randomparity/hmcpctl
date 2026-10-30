# Single-backing vNIC creation policy (#1827)

## Problem and authority

The existing ensure-one vNIC add route omits five documented adapter policies and drops
those inventory observations. #1827 adds them under scope q1827-682f114b, preserving
ADR 0057 admission, ownership, topology and uncertainty. Operator decision 6fab approved
five optional named arguments and additive selected-policy verification. The operator subsequently approved the bounded application validation and default-off
grammar exception (decisions call_2b7514702d4c4d089a3e7c3609b598a2/0 and
call_6b425adf43894b29a72cd9cb2f91dd36/0). Decision call_8fed2195f6be454d8b38122fe850b8f1
approves the bounded named live arm and this issue’s contract-plan format; M250 stays fixed.

## Scope

Extend the existing operation, SSH encoder and CLI/MCP presentations. No ownership move
is needed. Existing-vNIC updates belong to #1828; backing updates/failover to #669;
system defaults to #680. Multi-backing/degraded removal and allocation accounting stay
with #638; profile lifecycle with #1724/#637 and creation linkage with #638. No live run,
traffic proof, runtime-admission widening or evidence promotion is authorized. The
concurrent allocation page belongs to #1831 and is not edited here.

## Interfaces and values

Append keyword-only Python add_vnic parameters, all defaulting to None:
pvid_priority: int; allowed_vlan_ids: list[int] | Literal["all", "none"];
allowed_os_mac_addrs: list[str] | Literal["all", "none"];
auto_priority_failover: bool; mac_addr: str. MCP exposes matching value schemas, with
field-local Pydantic StrictInt for priority/VLAN members and StrictBool for failover, so
framework validation cannot coerce malformed values before the operation sees them.
CLI uses --pvid-priority, --allowed-vlan-ids (comma-separated integers/all/none),
--allowed-os-mac-addrs (comma-separated addresses/all/none),
--auto-priority-failover/--no-auto-priority-failover and --mac-addr.
None omits an attribute; zero and false are selected. Empty lists fail with advice to
use none. Priority accepts strict integers 0..7. MAC addresses require 12 ASCII hexadecimal
characters and normalize to lowercase; no colon/dash grammar or multicast inference.
Approved application policy: VLAN members are strict integers 1..4094; duplicate VLANs
or normalized MACs fail. Lists compare as sets, represented in canonical sorted order.
No numeric list-count limit is claimed. Limit complete encoded UTF-8 creation attribute
records carrying optional policies to 65,536 bytes, before preflight/dispatch. Omitted
policies retain the previous command and behavior, including generated MAC choice.

## Source boundary and encoding

P10 chhwres:238 and P11 chhwres:241 in docs/refs document all five creation fields,
priority bounds, MAC shape, comma lists and all/none. The official V10R3 M1060 manual,
CHHWRES printed pages 13–15 and 25, corroborates this contract and gives a nontrailing
quoted VLAN-list example. Retained V9R2 SP953 VirtualNICDetails.xsd:147 references
AllowedVLANIDs.Union; inc/Unions.xsd:139 references VLANID20.List, whose maxLength is 20.
The shared VLAN primitive prose says 1..4094 but numeric facets admit 0..4096. Its MAC
union references MACAddress4.List, whose definition is absent from the retained corpus.
These older REST types do not establish current CLI count limits or optional-mode success.
The chosen VLAN range and byte limit are application policy, not hardware qualification.

Approved amendment to ADR0061: the shared build_attribute_record gains an explicit
allow_nontrailing_quoted=False keyword. Only vNIC creation opts in; all existing name,
duplicate, delimiter and control-character checks remain. Both selected comma lists
are individually quoted by the shared builder, followed by shell quoting of the complete
record. The default positional refusal remains. No split writes or alternate encoder.
The complete record is prepared before preflight so local encoding refusal cannot be
misreported as a dispatched mutation. ADR0311 records this source-backed, unrun boundary.

## Observations and reconciliation

Append an immutable policy snapshot to VnicSnapshot, preserving its existing fields.
Each of the five fields independently holds a canonical typed value or None for missing,
blank, malformed or unsupported observation. Native comma lists, 0/1 booleans, integer
priorities and 12-hex MACs are parsed independently. No defaults are fabricated. Invalid
optional evidence must not erase otherwise usable backing/slot observations.
Append policy_verification to VnicChangeResult: status not_requested/verified/mismatched/
unavailable; checked_fields, mismatched_fields and unavailable_fields name the selected
five-field subset. None-requested fields are excluded, including HMC-generated MACs.
A unique existing topology match returns unchanged only if selected policies are verified;
mismatch or unavailable policy evidence raises VnicCapabilityError with no write.
Do not filter conflicting candidates away and create a duplicate. After one attempt,
retain topology reconciliation and changed independently; compare policies only on exactly
one newly observed matching slot. Mismatch/unavailable becomes VnicPartialError carrying
both axes; command failure remains an error even if later state matches. No retry/rollback.
Removal keeps its contract and reports not_requested policy verification.

## Failure model

- Actors and deployments: authorized Python/CLI/MCP and named-arm operators, malformed supplied values,
  partial HMC observations; existing POWER9 8375-42A / HMC V10R3 M1060 admission only.
- Invariants/assets: selected LPAR and VIOS authority, no duplicate ensure-one mutation,
  backing capacity/topology, one attempt, no false policy-success claim, command grammar;
  live cleanup targets only a newly captured slot after fresh identity/baseline guards.
- Accepted classes: existing cross-process races remain bounded by readback, not a lock;
  HMC may reject source-supported inputs; optional policy success remains unevidenced
  until a separately authorized exact-revision live run. No traffic claim follows readback.
- Covered elsewhere: #1828 updates; #669 backing/failover; #680 system defaults;
  ADR0057 admission/topology; ADR0061 default record grammar; release workflow live proof.

## Threat model

- Boundaries widened: Python/CLI/MCP request to policy validator; typed fields to shared
  HMC CSV encoder and remote shell; native inventory to independent policy observations.
- New boundaries: private operator packet to the standalone arm, durable effect journal and
  fresh inventory to cleanup authority. Trust the operator’s exclusive-writer declaration;
  verify packet hash, private owner/modes, exact revision/destination and contained LPAR identity.
- Actors: authorized callers may submit malformed or adversarial values; HMC inventory may
  be missing/malformed. Existing authorization trusts configured policy and ownership checks.
- Controls: strict value grammar, complete-record byte cap, unchanged name/delimiter checks,
  shell quoting, existing LPAR/VIOS authorization, one attempt and selected-field readback.
  Validation errors name inputs, but no extra host data or credentials are emitted.
- Outside scope: a compromised HMC or authorized concurrent writer; transport authentication
  and cross-process capacity races retain their existing owners and no lock/rollback promise.

## Validation and success

Executable tests must reject malformed/duplicate/empty/out-of-domain policy inputs before
preflight; prove omission, false, zero, sentinel and list encoding; preserve authorization;
prove no-write mismatch/missing refusal; independently retain valid observations when another
field is malformed; distinguish new-topology success from policy mismatch/unavailable;
prove one attempt after dispatch failure and both readback failures; and cover CLI/MCP/schema
forwarding plus actual framework-dispatch refusal of bool/string/float integer coercions
and nonboolean failover inputs before invoking the operation. Controlled fault tests remove comparison and encoding guards and must fail.
Regenerate tools with just tool-docs, run focused tests/linters, then final just verify and
separate pinned prek all-files, followed by the Python 3.11–3.14 Linux CI matrix.

## Bounded live procedure (not run)

Add the named live_vnic_creation_policy.py arm with special preflight/recovery dispatch,
never default/all runner dispatch. Invocation uses existing uv run --no-sync python prefix:
- scripts/live_test_preflight.py --group vnic-creation-policy --packet P --packet-sha256 H --output O
- scripts/live_vnic_creation_policy.py --packet P --packet-sha256 H --output O
- scripts/live_test_recovery.py --vnic-creation-policy-packet P --packet-sha256 H --results R --output O

The exact version 1 JSON packet contains: version; configured_profile; hmc_identity {host,port};
expected_commit; system_uuid; lpar_uuid; expected_lpar_id; owner_token; selector {vios_name,
vios_lpar_id,adapter_id,physical_port_id,capacity_percent}; port_vlan_id; policy (the five
optional inputs); baseline_sha256; expires_at; exclusive_writer_window:true;
allow_one_create:true; allow_captured_slot_cleanup:true. Reject unknown keys, duplicate JSON
keys, invalid types and nonfinite numbers. Secrets stay in the configured connection.
Initial read-only preflight may accept null baseline and emit its digest; capture requires the
operator’s updated packet/hash and matching fresh baseline. Never rewrite a packet implicitly.

Use an already owned eligible LPAR and one backing. Verify clean exact expected HEAD and
loaded module paths, destination/profile/agent binding, contained UUID/numeric ID/owner,
existing admission, VIOS identity and backing feasibility. Retain bounded complete target
vNIC/system-backing/direct-port projections privately. An ineligible platform yields SKIP,
not live credit. New private output and atomic journal intent must precede every effect.

Call real add_vnic once. Cleanup requires a successful complete result, changed:true,
verified selected policy and one newly captured slot with its correlated active Operational
backing. Only when no policy is selected, not_requested also satisfies that policy check;
it provides no optional-policy live credit. All remaining cleanup guards still apply. Fresh contained identity/owner, exact captured slot/backing/policy and unchanged
foreign baseline are required before one real remove_vnic. Unknown completion, unreadable
state, journal failure or drift leaves MANUAL_RECOVERY; never guess, retry or roll back.
Do not repeat ensure-one in this arm; its no-write contract is proved offline. Independent
recovery is read-only and requires complete journal plus original projection equality for CLEAN.

Stage ceilings are preflight 120/capture 300/recovery 120 seconds, response/output 1 MiB and
journal 5 MiB. Enforce actual physical calls at REST _request and SSH _run_hmc_command with
finally restoration in this standalone process: 24 REST + 24 SSH for preflight/recovery and
48 REST + 48 SSH for capture, including own Logon/Logoff with one logout reservation. Only reads,
own logon/logoff and exact armed once-only create/remove commands may pass. Measure the
exercised call graph offline before requesting a root live lease. Bound exhaustion preserves
uncertainty after dispatch. No LPAR creation/activation, backing reconfiguration or policy update.
The root orchestrates any separately admitted execution and mandatory recovery; this design
confers no worker hardware authority or traffic/maturity evidence.
