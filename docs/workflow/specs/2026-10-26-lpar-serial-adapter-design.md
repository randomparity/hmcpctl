# Virtual serial adapters at partition creation

Preparation against 1aad137647baa771497870e01fbc875149666140; #1772, claim q1772-6f1d1bb3.
Authority: [scope 6419243721](https://github.com/randomparity/hmcpctl/issues/1772#issuecomment-6419243721),
including explicit maximum interfaces and HMC-final unresolved client capability approvals.
Lane full-spec; complexity M; fixed denominator 250; exclusions/owners: empty.
Decision: [ADR 0303](../../adr/0303-lpar-serial-adapter-creation.md).

## Global Constraints

Python 3.11 is the local floor; CI targets amd64 Ubuntu 24.04 on Python 3.11, 3.12, 3.13 and 3.14.
No dependency, toolchain-floor or six-name stable-facade change. Bootstrap only with just setup;
use uv run --no-sync. Run just verify and the separate pinned prek all-files hooks before push.
Root owns final integration verification, publication and any separately authorized hardware work.
The implementation plan is ignored/transient and must not be committed. No live execution is a merge gate.

## Problem and bounded outcome

Implement virtual_serial_adapters in the allocated mksyscfg partition-create contexts,
P10 docs/refs/hmc-commands-p10/commands/mksyscfg.md:41 and P11 counterpart:49.
These two bindings retain seven positional components and six slash separators; they do
not add ledger source units. Supporting read/error references do not expand the allocation.
No dynamic serial CRUD, implicit peer mutation, activation, guessed OS matrix or profile
lifecycle replacement is introduced. The #638 C12 positive virtual-slot console/load-source
linkage gap remains separate, as do the existing C04 combination policies.

## Input and native encoding

Expose virtual_serial_adapters as a tuple of VirtualSerialAdapter records on LparCreation,
both direct create presentations, the separate VIOS operation and its CLI/MCP presentations.
Each record has local_slot, role, supports_hmc, remote_lpar_id, remote_lpar_name,
remote_slot and is_required. Integer/boolean/string types are strict for direct Python
and decoded inputs; unknown fields are refused. CLI uses bounded inline JSON (64 KiB).
An omitted or empty collection selects no serial attribute; it never means clear/delete.

A client has role client, no supports_hmc value, exactly one native remote ID or exact name,
and a remote slot. A server preserves independently optional supports_hmc, ID, name and
remote slot. Its only explicitly supplied HMC value is 0. Both supplied server selectors
must resolve to the same endpoint. Unbound server 14/server/0////0 remains valid.
All records require an explicit boolean is_required; zero/false is never lost by truthiness.
Names may not inject slash, comma, equals, quotes or controls into the nested/native grammar.
The complete encoded collection is bounded to 64 KiB; this is an input-size bound, not an
invented hardware slot/count maximum. No source-unspecified numeric upper cap is added.

Serial selection requires an explicit max_virtual_slots strictly greater than each selected
local slot. Local slots 0 and 1 are reserved (HSCL07DF); duplicate local slots are refused.
Remote slots are checked against their concrete endpoint's configured maximum when a peer
is selected; an unbound server has no fictitious peer/max to read. Compare virtual slot
numbers only to virtual slots, never physical DRC or SR-IOV logical identifiers.
The current creation request owns no other explicit virtual-slot family: PCIe assignments
carry physical/logical DRCs and vNIC automatic placement. Preserve those existing owners;
there is no generic all-family occupancy validator to claim. Native postcondition parsing
refuses to certify local slot occupancy when an unexpected nonempty Ethernet/FC/SCSI/vNIC
family is observed. It reports unavailable; it does not pretend to parse those families or
classify a collision by comparing unrelated identifiers.

The approved optional maximum reaches LPAR CLI as --max-virtual-slots and the VIOS operation,
CLI and MCP. Non-serial omission remains unchanged. Client eligibility alone is HMC-final
in AIX/Linux, OS400 and Virtual IO Server; other source-established restrictions remain
before effects. Explicit keylock is incompatible with serial's native-only route.

Compose one finite serial clause with existing C12 clauses in the same native record.
Preserve scalar record validation and shell quoting separately. Do not weaken the generic
build_attribute_record trailing-list contract or split creation into separate effects.

## Identity and authorization

Before effects, resolve the selected system and inspect its bounded LogicalPartition and
VirtualIOServer feeds. Native PartitionID, REST UUID and exact name are separate namespaces.
A numeric-looking name remains a name. Every explicitly selected existing peer must resolve
to exactly one contained canonical UUID, correct resource kind, exact name and native ID.
Re-read that resource through its correct kind; require identity agreement and its configured
maximum. Compare UUIDs case-insensitively but preserve the provider spelling for URLs.
Missing, ambiguous, contradictory, foreign or malformed selected identity fails before create.
Use existing authorize_lpar_ownership_description on the confirmed peer resource, retaining
its malformed/foreign-owner refusal and audit behavior. Do not restamp or modify a peer.

A selector matching the pending local name, or the explicitly requested local native ID,
refers to the pending local request. Check duplicate/existing-name/native-ID conflicts first.
Do not invent an auto-assigned native ID. A server supplying the pending name and an ID
without an explicit matching local ID is ambiguous and refused before effects. Self-reference to a different slot is retained;
reject only the same local partition AND same local slot (HSCL1555). Resolve the pending
identity after create, before reporting a pair. A missing self counterpart is unverified,
not an excuse to create another adapter. Server slot-only input selects no existing peer.

LPAR MCP already declares exhaustive_targets=False. Apply that declaration to VIOS MCP
when widening it to serial peers. One all-targets grant is required; it does not replace
operation containment/ownership. No collection extractor or scalar-grant bypass is added.

## Creation and result ownership

Keep create_and_stamp_lpar and create_vios as their separate domain owners. Both feed the
existing create_lpar_via_cli writer, preserving resource completion and D07 update-only
weight rejection. Serial requests trigger controller-mode discovery even without role/layout
fields. Preserve C05/C06/C10 and physical/security/reporting/vTPM admission/readback ordering.
Controller mode omits profile_name/sync_curr_profile and cannot apply a named profile.

Authorize all selected existing peers before the one creation attempt. Submission failure,
including capability refusal, has an unknown outcome unless independent scoped identity
reconciliation establishes more. Do not replay, compensate or stamp an uncertain identity.
After acknowledged creation, prove the returned UUID/kind/name/native ID through the selected
system feed and correct resource read. An unconfirmed identity produces an incomplete outcome.

LPAR adds serial_step to LparCreationResult and the workflow's ordered steps. Preserve its
existing best-effort/required stamp policy; serial follow-ups require a confirmed stamp and
requested serial/other prerequisite verification. Serial delays apply and PCIe assignments.
VIOS retains its existing no-stamp behavior, returns its entry with serial_verification on
verified configuration, and raises a sanitized partial/unknown HMCError otherwise. It is
not routed through LogicalPartition and gains no implicit ownership-description mutation.
Omitted serial input preserves both owners' prior result shapes and routing.

## Independent observations and completion

Classic local readback uses the exact initial default_profile created by the native writer,
binding profile name, local name/native ID and configured maximum to confirmed identity.
For an existing peer, read its independently observed DefaultProfileName, not a guessed
constant. Bind that profile to the peer native ID/name. Label this plane saved-default-profile;
it is evidence of intended saved configuration, not the peer's active/current configuration.
Missing/ambiguous profile identity, maximum, record attributes or remote observation is
unavailable, not a successful pair. No current_profile/default_profile equivalence is assumed.

Parse complete native CSV records with strict quoting, duplicate-attribute refusal and a
bounded result. Keep request and observation grammars separate: recognized implicit 0/1
HMC defaults containing supports-HMC=1 and any are observations, never CREATE authority.
Check requested explicit slots and required/role/explicit HMC fields, resolved remote identity
and selected slot constraints. Reject unexpected non-default serial records; unexpected other virtual-family
configuration makes local slot certification unavailable. Supplied peer ID/name must agree independently in the observation.

Report local configuration, each peer relationship and pair status separately. A fully
unbound or slot-constrained server without a selected partition reports configured/unbound,
never paired. For a selected peer, a verified saved pair requires opposite roles and reciprocal
endpoint/slot agreement, or the documented unbound-server acceptance pattern. Missing,
inconsistent or unavailable counterpart evidence keeps paired verification incomplete.
Same-partition pairs use the independently read local profile, not merely the submitted list.
Recognized HMC-console defaults alone do not certify a custom serial pair.

Controller current serial mapping is unavailable in the bounded sources. Return that status
truthfully without reading a named profile; it blocks dependent effects and paired success.
The public result cannot claim running serial traffic, boot or console success.

## Failure model

1. Actors and deployments
   - Direct library callers, local CLI operators and authenticated MCP callers on classic
     and controller HMC creation paths; offline CI uses explicitly synthetic fixtures.
2. Invariants and assets at stake
   - Contained local/peer identity and ownership; one creation effect; native record integrity;
     preserved existing policies; honest saved/current/pair status and stopped follow-ups.
3. Accepted failure classes
   - Unresolved client capability may be rejected by HMC after submission, by explicit approval;
     its outcome remains unknown/partial and earns no retry/rollback privilege.
   - Concurrent external mutation may invalidate observations; mismatched evidence is incomplete.
     No atomic multi-resource snapshot or active-connectivity guarantee is made.
   - Controller current observation can be unavailable; dependent effects remain blocked.
4. Covered elsewhere
   - #1724 owns profile lifecycle; #638 retains C12 positive virtual console/load-source linkage.
     Existing credential/transport protections and C04/C05/C06/C10/D07 policies remain operative.

## Threat model

1. Added/widened boundaries: structured serial inputs enter both domain owners; peer selectors
   widen managed-system authorization; native record and shell encoders receive caller values;
   REST/native responses enter identity and relationship verification.
2. Actors: callers may submit malformed types, delimiters, foreign/ambiguous selectors or
   unauthorized peers. HMC responses and concurrent administrators can invalidate observations.
   Trust the authenticated transport and existing grant/ownership policy, not caller assertions.
3. Controls: bounded strict decoding; source-shaped finite serialization; unique contained
   identity and ownership checks before effects; non-exhaustive grants; independent bounded
   readback with duplicate/multiplicity checks. Sanitize partial-error chains and output.
4. Outside this change: credential issuance and transport compromise remain with existing
   security owners; guest traffic/boot proof and cross-resource atomicity are not promised.

## Success and verification

For the bounded creation paths above, prove the two reference bindings, complete grammar,
optional forms and strict negatives; explicit maxima/reserved/duplicate/self-slot handling;
all affected endpoint identity/authorization failures before writes; one native attempt;
LPAR/VIOS caller forwarding and non-exhaustive grants; C12 multi-list preservation and
C04/C05/C06/C10/D07 composition; independent configured/unbound/pair/unknown distinctions;
blocked apply/assignments after incomplete evidence; and no accidental profile CRUD changes.
New safety tests need controlled faults that demonstrably fail, then restore the source.
Run focused guards first, then root-owned full verification and pinned hooks/CI.

Add a reproducible opt-in ST63 definition/procedure with explicit targets, prerequisite and
baseline capture, deployed-HEAD check, finite creation calls and exact recovery conditions.
No source/hardware authorization is implied. Recovery never deletes an unconfirmed identity
or a pre-existing peer; retain unknown residue for operator reconciliation. All live arms
remain unevidenced until separately run and recorded at the matching commit.
Update generated tool documentation, operation signatures/tests, maturity constraints and
changelog. No successful old live record certifies the new serial variant.

## MCP evaluation contract

AI-SPEC: authenticated tool callers submit the typed collection to existing creation tools;
the tools return configured/unbound/paired or incomplete facts through the existing owners.
Only frozen P10/P11 bindings and contained HMC observations support these facts. No model call,
prompt or model cost is added. Existing transport deadlines and the finite input/observation
bounds apply; there is one creation attempt and no retry loop. Unavailable facts stop dependent
effects. Success is a deterministic schema/authorization/operation assertion, not an LLM grade.

| Case | Severity | Setup and required observable | Gate |
| --- | --- | --- | --- |
| Valid unbound server | 4 | Both presentations forward all explicit fields; unbound is never paired | block |
| Ambiguous dual identity | 5 | Conflicting/pending selectors fail before create | block |
| Forbidden HMC flag or injected delimiter | 5 | Strict input refusal; zero native writes | block |
| Stale peer read | 5 | Changed UUID/name/native ID cannot authorize a peer or pair | block |
| Scalar grant or foreign owner | 5 | Non-exhaustive policy/ownership denies before effects | block |
| Oversized input or lost response | 5 | Bounded refusal or one attempt; no retry/cleanup/stamp | block |
| Separate VIOS owner regression | 5 | VIOS entry/native owner retained; no LPAR stamp or scalar-grant bypass | block |

The plan maps these cases to deterministic parser, endpoint, actual-owner and MCP tests with
controlled faults. Product tests, fault runs, model evaluations and live observations are all
unrun in this preparation. A later run must report its real findings rather than inherit credit.
