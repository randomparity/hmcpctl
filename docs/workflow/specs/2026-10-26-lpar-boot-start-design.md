# C09 partition boot/start configuration design

Status: D1/D2 and A1/A2 complete; implementation in progress. No product live credit.
Approved G4/raw/result/format and N1/N2/N3 contracts govern the normal build gates.
Issue: #1770, native parent #638; claim q1770-02d745af.
Landed anchor: b5ef629181a6b0c02585096558e2fe29e96c738c; tree equals qualified source d68a5c55.
Authority: [frozen six-field scope](https://github.com/randomparity/hmcpctl/issues/1770#issuecomment-6431981824).
Lane: full-spec; frozen complexity M and denominator 250. Exclusion/owner set: empty.
Decision record: [proposed ADR0301](../../adr/0301-lpar-boot-start-configuration.md).
G4 design authority: call_db59b5f315004029bc2f4cd5d9b4f063/0 and exact approved proposal.

## Problem and success

C09 closes five creation inputs/one update across twelve P10/P11 bindings through separate
LPAR/VIOS owners: source applicability, contained authorization, independent postconditions,
CLI/MCP parity, negative tests and truthful evidence. No binding is excluded; activation/path
lists are partial reuse, without profile/dependency/provisioning/stable-facade expansion.

## Global Constraints

Python >=3.11; local interpreter .python-version=3.11; CI ubuntu-24.04 amd64 on Python 3.11, 3.12, 3.13 and 3.14.
Host Darwin arm64 is distinct from CI targets; local checks do not supply hosted arm64 proof.
Add no dependency, provision input, immediate power action, profile CRUD or hmcpctl.api export.
Preserve path-list calls, boot-order clear refusal and legacy CLI positional yes arguments.
Use existing typed owners, fixed resource/command selectors, ownership audit and AllTargets policy.
Omission preserves HMC defaults; explicit false is retained; absent observations are not defaults.
No automatic retry, inverse write, activation or deletion after an uncertain external effect.
Use just setup only for environment bootstrap; use uv run --no-sync for project commands.
Before shipping, regenerate derived documents, run just verify and uv run --no-sync prek run --all-files.

## Source reconciliation

The table is twelve independent bindings: each row has one P10 and one P11 binding.
Creation references are `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`
and `docs/refs/hmc-commands-p11/commands/mksyscfg.md:49`.
Update references are both `docs/refs/hmc-commands-p{10,11}/commands/chsyscfg.md:49`.
Only field facts are summarized here; no reference prose is copied into this document.

| Field | Context and source-backed values | Existing owner and remaining gap |
|---|---|---|
| auto_start | partition-create; optional 0/1 | No creation input; saved-profile AutoStart exists. Configure policy, do not power cycle. |
| boot_mode | partition-create; norm/dd/ds/of/sms; AIX/Linux/VIOS | jobs.requests.BootMode already has these tokens; activation is a separate effect. |
| power_ctrl_lpar_ids | partition-create; alternative comma-separated numeric IDs | No input; IDs require selected-system resolution, not UUID parsing. |
| power_ctrl_lpar_names | partition-create; alternative comma-separated names | No input; exact names require unique contained resolution and authorization. |
| conn_monitoring | partition-create; optional 0/1 | No input; connection monitoring is not Service Agent monitoring. |
| boot_string | partition-update; AIX/Linux/VIOS | LPAR path-list RMW exists; raw input, independent result and VIOS transport remain. |

The creation sections supply no controller count, numeric limit, clear sentinel or new
release floor. The update sections supply no complete string grammar, size limit or
state restriction. A source omission is not a support prohibition or permission to invent one.
Both controller-selector names retain their binding even if normalized to one native ID list.

Retained LogicalPartitionProfile.xsd declares AutoStart and ConnectionMonitoringEnabled
as booleans, BootMode as PartitionBootMode.Enum and PowerControlPartitions as
BasePartition_Links.Type. Existing profile code preserves these fields and validates the
profile's parent association (`client/client_profiles.py:50`, `documents/partition_profile.py:32`).
The enum includes Normal, Diagnostic_With_Default_Boot_List,
Diagnostic_With_Stored_Boot_List, Open_Firmware, System_Management_Services, Default,
Unknown and Unavailable. The latter three are not aliases for a requested native value.

Retained BasePartition.xsd:508,533 declares optional IsConnectionMonitoringEnabled as
xsd:boolean (UOD at535); BootMode:2268,2293 extends PartitionBootMode.Enum. This is
current-resource vocabulary, distinct from saved ConnectionMonitoringEnabled. The newly
captured BasePartition_Links.Type:92,111,123 admits zero through unbounded AtomLink.Group
entries; no complete transitive XSD validator is needed for the bounded observer below.

Retained VirtualIOServer extends BasePartition directly; the retained LogicalPartition
schema owns BootListInformation. The available VIOS instance POST route and ETag protocol
do not establish a VIOS pending-boot XML leaf. Existing LPAR XML cannot be reused by changing
only the UUID. The source receipt holds the exact schema hashes privately.

The earlier 56-field empty native header remains historical. Source d68a5c55 now qualifies
eight fixed nonempty/punctuation values with full native/B1 preservation, owned deletion,
restored B0 and independent closed-session recovery. This is one release/fixture, not a
universal Unicode, controller, size or bootability claim; current/shadow/last-boot stay distinct.

## Existing owners and extension points

Existing LparCreation/create_and_stamp_lpar and workflows own validation, native selection,
confirmed identity/stamping and assignments. Preserve typed defaults, metadata, uncertainty
and every requested memory/affinity/serial/layout/hardware/policy/I/O/vTPM/settings barrier.
create_vios retains VirtualIOServer lookup, explicit resources and its separate family guards.

ssh/lpar.py composes one mksyscfg record from ordinary, physical/SR-IOV and serial pairs.
The C09 resolved numeric controller list remains last; shared scalar/list validation and
whole-record shell quoting stay separate and unchanged.

The existing LPAR boot owner uses set_pending_boot_string(self, lpar_uuid, boot_string):
Advanced whole-LogicalPartition ETag RMW with field presence/unrelated XML preservation.
C09 replaces write-response-only success in that owner with independent exact observation.

## Approved interfaces and application behavior

```python
@dataclass(frozen=True)
class LparStartSettings:
    auto_start: bool | None = None
    boot_mode: BootMode | None = None
    power_ctrl_lpar_ids: tuple[int, ...] | None = None
    power_ctrl_lpar_names: tuple[str, ...] | None = None
    conn_monitoring: bool | None = None

# Candidate concrete representation; only high-level direction is approved.
# Existing parameters retain their order/defaults; verification uses existing WorkflowStep seams:
LparCreation.start_settings: LparStartSettings
# create_vios(..., *, start_settings: LparStartSettings | None = None)
# create_lpar_via_cli(..., *, start_settings: LparStartSettings | None = None)

async def set_lpar_boot_order(
    hmc: HMCClient, lpar_name_or_uuid: str, devices: list[str] | None = None,
    *, system_name_or_uuid: str, ownership_override: bool = False,
    boot_string: str | None = None,
) -> dict[str, Any] | None: ...
```

`BootMode` is the existing `jobs.requests.BootMode`. The approved creation model is
creation-only; adding it to shared update/create LparSettings would widen unrelated updates.
A supplied false emits 0; true emits 1; None omits. Strict booleans reject numeric/string
coercion. Native mode excludes OS400; the source does not restrict the other four inputs
by OS. Both selector forms together, an explicit empty collection, booleans masquerading
as numeric IDs and duplicate selectors fail before effects; no undocumented sentinel is used.
Names are looked up, not interpolated into native attribute values. Numeric limits remain
HMC-final after lexical integer validation and unique contained resolution.

Approved presentation is `start_settings` on hmc_create_lpar/hmc_create_vios and
`--start-settings` typed JSON on the two existing CLI create commands. Existing arguments,
including the positional Python-call slot for yes, keep their positions. New keyword-only
parameters must not be inserted before that slot. This preserves the approved UX direction.

Boot inputs accept exactly one of devices or boot_string. devices retains existing slash-rooted,
printable non-space ASCII validation and one-space joining, including accepted punctuation.
For new raw input, accept printable Unicode with a non-whitespace character; preserve exact
text/spacing without strip, normalization or joining. Reject empty, whitespace-only, controls,
invalid Unicode and unencodable UTF-8 before presentation client construction or domain effects.
Raw UTF-8 length must be <=16KiB (16384bytes): an application resource budget, not a vendor
maximum. Do not apply this new raw limit to legacy path lists or impose an ASCII-only policy.
HMC retains remaining semantic/platform limits; literal none is text, not a clearing alias.
Source-qualified delimiter handling and whole-record shell quoting remain necessary. Refusal,
uncertainty, mismatch or unavailable/oversize exact readback blocks subsequent effects.
No Unicode acceptance on untested HMC contexts, bootability, retry/clear/apply/activation is implied.

Approved separate VIOS entry points are `read_vios_boot_order` and `set_vios_boot_order`
with vios_name_or_uuid replacing lpar_name_or_uuid, and the same system/input keywords.
They live with the boot owner and select VirtualIOServer containment/ownership,
with `hmc_read_vios_boot_order`/`hmc_set_vios_boot_order` and VIOS read/set-boot-order commands.
A generic caller-selected resource kind, attribute, URL or command is not proposed.

Approved compatibility direction: preserve the setter's successful dict-or-None result
and existing CLI --json shape. Return the original write result only after an acknowledged submission
and a fresh exact pending-text match. Unknown execution, mismatch or unavailable observation
raises a safe existing HMCError with bounded operation/status/recovery guidance. Independent
text equality proves stored pending text, not boot success or resolution of uncertain execution.
The read operation keeps its existing five-key raw-state dictionary. No new generic return
flag or compatibility re-export is introduced. CLI uses its existing error path on failure.

Creation verification uses existing ordered WorkflowStep/result and VIOS entry verification
metadata seams; preserve the outer LPAR creation result schema. An internal carrier may supply
that step; no new top-level public start_step is added. For separate VIOS boot reads and verified
writes return exactly vios_uuid, vios_name and pending_boot_string from bound observations.
On reads, null means a positively observed empty native field; missing/malformed/unbound data
is unavailable/error. Writes succeed only after independent exact requested-text equality.
Preserve existing LPAR boot API/results; use --boot-string as routine raw-input naming.

## Contained identity and authorization

Resolve the selected managed system before enumerating both LogicalPartition and
VirtualIOServer collections. Bind each controller to resource kind, UUID, exact name and
numeric PartitionID. Refuse unreadable feeds, ambiguous identities and missing/foreign
selectors. Resolve only existing controllers; an unresolved pending/self identity cannot
pass authorization. Do not infer a universal HMC self-reference prohibition from this guard.

Freshly read each selected resource, compare its tuple with the contained inventory, then
use the existing ownership-description authorization/audit owner before mksyscfg. A controller
is an affected target even though C09 changes the new partition's relationship field only.
Recheck selected identities at the pre-effect boundary and after creation before crediting
that relationship. This detects drift; it is not an atomic lock against another HMC writer.
C11's serial inventory has slot/profile-specific assumptions; do not invoke its private
helpers as generic power-control identity APIs or require a controller to have serial slots.

Both creation MCP tools already declare exhaustive_targets=False. Retain it: one grant must
cover the operation, connection and AllTargets; target tables cannot bound nested controller
collections. AllTargets does not replace per-controller domain ownership authorization.
No policy union, collection-selector framework or new bypass is needed. CLI and direct Python
calls still perform domain authorization, independent of MCP dispatch.

## Creation algorithm and readback planes

1. Validate requested creation settings with the existing family inputs before any write.
2. Resolve/admit the system and controllers; preserve existing memory/capability checks.
3. Select the existing native create path when a C09 creation field is supplied. Explicit
   keylock cannot be represented there and is rejected before effects, as other native families do.
4. Determine classical versus controller mode through the existing role capability owner.
   Controller mode omits profile_name; retain its no-profile-apply and incompatible profile-I/O
   admission. C09 must trigger this discovery even when other native families are omitted.
5. Compose one mksyscfg record. No separate chsyscfg repair after creation, retry or REST fallback.
6. Independently confirm the created UUID/kind/name/native ID under the selected system;
   identity uncertainty forbids ownership stamping, profile apply and assignments.
7. Observe only requested C09 settings on their actual plane. Retain a truthful outcome;
   completed creation can coexist with mismatched/unavailable settings and a surviving partition.
8. Join the candidate verification outcome and existing requested-family set in core apply and
   workflow assignment barrier. No requested family gets bypassed because C09 is present.

P is `read_partition_profile(hmc,system,partition,initial_profile,partition_type=kind)`
and its preserved raw element; require the unique contained initial/default profile.
C is fresh contained kind-specific `get_uom(kind,uuid,group="Advanced")`.
Observe requested fields only; omitted settings create no guessed-default postcondition.

| Requested field | Classic required observations | Controller/no-profile observations | Equality |
|---|---|---|---|
| auto_start | P:AutoStart | Unavailable; no established current reader | Strict true/false XML Boolean equals request; no coercion/native candidate |
| conn_monitoring | P:ConnectionMonitoringEnabled AND C:IsConnectionMonitoringEnabled | C:IsConnectionMonitoringEnabled | Strict requested Boolean equality on every required plane |
| boot_mode | P:BootMode AND C:BootMode | C:BootMode | Exact mapping below; Unknown/Unavailable/Default unavailable |
| either power-control selector | P:PowerControlPartitions AND C:PowerControlPartitions | C:PowerControlPartitions | G4 admitted canonical identity-set equality |

Saved evidence is explicitly inapplicable in controller/no-profile contexts; it never
substitutes for required current evidence. Controller auto_start is admitted but cannot
complete verification with an unavailable observer; this is no excluded field/context.
Missing/malformed/unreadable required observations are unavailable; contradictions mismatch.
Both outcomes retain surviving creation and block apply/assignments and subsequent effects.
LPAR pending text uses the lossless Advanced document below; VIOS uses qualified full native
header identity/text equality. Neither write replies nor current/last-used boot fields substitute.

Approved-for-design native-to-observed translation: norm→Normal, dd→Diagnostic_With_Default_Boot_List,
ds→Diagnostic_With_Stored_Boot_List, of→Open_Firmware, sms→System_Management_Services.
Enum spellings are source-backed; this translation must be tested against retained/live
profile observations before positive equivalence credit. Unknown/Unavailable/Default is
unavailable for the requested exact value. No change applies or activates the profile merely
to manufacture a successful observation. Native current and profile-saved values stay separate.

### Operator-approved bounded G4 readback

The exact six-item approved proposal governs this subsection; it replaces the prior positive
controller-context observation prerequisite, not the requirement for independent readback.
Read current resource and, in profile-bearing contexts, its contained initial saved profile.
Keep the planes separate; a no-profile context retains explicit unavailable saved evidence.
For requested conn_monitoring inspect exactly current IsConnectionMonitoringEnabled and
saved ConnectionMonitoringEnabled. Accept strict observed Boolean values only and require
requested equality in each applicable required plane. Alias/default/coercion and saved-to-
current substitution cannot satisfy the contract. Missing/malformed/unreadable required data
is unavailable; contradictory observed values mismatch. Retain other facts, but block apply,
assignment, activation and other subsequent effects; surviving creation is not workflow success.
Inspect only recognized PowerControlPartitions/link structure. Against already admitted
controllers, apply the existing same-origin HTTPS/host/port/no-userinfo/query/fragment and
exact resource-route policy. Compare canonical selected-system/kind/UUID/native-ID sets,
retaining UUID transport spelling; never follow provider URLs. Missing collection or duplicate,
ambiguous, foreign or malformed link is unavailable. Valid present empty collection is an
observed empty set and mismatches a requested nonempty set. No arbitrary XML copying or
complete AtomLink dependency closure is required. This does not expand accepted URL forms.
Enum spellings and strict requested equality are design inputs; review/tests must validate
translations. Current/saved Normal and monitoring true/false are retained classic observations,
not controller presence, requested-value causal equality, effective guest behavior or live credit.

## Boot write algorithm and qualified VIOS source

LPAR: validate one input, resolve contained identity and authorize ownership. New raw input
requires freshly bound AIX/Linux type; OS400 refuses with zero writes even with leaf/ETag present.
Legacy devices/path acceptance remains unchanged. Retain field-presence/ETag Advanced RMW,
submit at most once, then independently read `read_logical_partition_document(uuid)`.
For pending verification AND the pending field of `read_lpar_boot_order`, use exactly one
recognized BootListInformation/PendingBootString element's `.text`, without strip; existing
empty element is observed null, missing/duplicate/unbound/malformed is unavailable/error.
Bind fresh contained system/kind/UUID/name. Preserve five read keys and other four fields'
legacy semantics; shared element_to_dict/leaf_text trimming stays unchanged. XML entity
interpretation is normal XML parsing, not whitespace normalization.
412 is a rejected stale write; a missing ETag or leaf stops before POST. Bodyless/202/malformed
write responses cannot establish completion. The current client raises LparUpdateError with rejected/acknowledged/unknown submission
certainty (`client/client_lpars.py:274–339`); preserve that distinction. On uncertain transport,
independent observation may establish exact stored text but must retain unknown execution and
raise the safe operation error. Do not retry or restore.
Remove raw boot text from generic success logging and error chains; keep raw text only in the existing explicit read/write document surfaces, with terminal-safe rendering.
The default success message follows independent verification; unavailable is not printed as success.

VIOS: select and authorize through its separate owner; bind fresh contained UUID/name/positive
native ID/type and observed description. Form exactly two cells, lpar_id=<ID> and
boot_string=<validated raw text>, with csv.writer(delimiter=",", quotechar='"',
QUOTE_MINIMAL, doublequote=True, escapechar=None, lineterminator="\n"); remove only its
final LF and shlex.quote the WHOLE record for fixed chsyscfg -r lpar -m <system> -i <record>.
Do not relax build_attribute_record, import diagnostic runtime/nonce/Budget machinery, or retry.
Independently run fixed lssyscfg -r lpar -m <system> --filter <quoted lpar_ids=ID> -F --header,
using build_filter and run_hmc_command(max_output_bytes=65536). Parse full stdout with
csv.reader(strict=True): exactly one unique nonempty header and one equal-width data row;
require name,lpar_id,lpar_env,uuid,description,pend_boot_string and the bound identity tuple.
Over-64KiB, replacement/control text, malformed/truncated/lossy output or identity drift is unavailable/error;
only positively observed empty read text becomes null. One acknowledged write succeeds only
with fresh exact requested pending text; unknown/refusal/unavailable remains a safe error.
Source [d68a5c55](https://github.com/randomparity/hmcpctl/issues/1770#issuecomment-6479770844)
qualified all eight fixed literals, full B1/native preservation, deletion/B0 and closed recovery.
At landed b5, source history/counts stay separate; no universal support or product live credit.


## ST68 fixture ownership

VIOS creation alone permits MAX1 existing stamp_lpar_ownership(config, system_name,
lpar_name, *, agent_id=None, caller_token=None); no public creator or other-case stamp.
Require acknowledged current creation absent B0, fresh unique contained UUID/name/positive ID,
inactive/fixed saved resources, protected current/profile ETag/unknown I/O and foreign B0 equality.
Initial description must remain exact with no foreign/malformed marker; no name-only adoption.
Before stamp recheck candidate/import/connection, raw digest/current grant/exclusive invocation.
Use existing date.today local-calendar marker, no new date API/retry:
[hmcpctl owner:<agent> created:<local-date>] [caller <packet-token>].
Private vios_stamp is null if inapplicable/unarmed; otherwise exact keys identity, created_date,
expected_description, returned_description, execution, verification, observed_description.
identity has system_uuid/kind/uuid/name/partition_id, kind VirtualIOServer. Persist intent before
call: execution=unknown, verification=unavailable, returned/observed_description=null.
ANY successful string return, including empty/different/date-shifted, means acknowledged;
retain exact returned_description, never downgrade acknowledgment on failed/mismatched readback.
None/raised/lost remains unknown. Independent fresh contained Advanced GET requires exact full
expected==returned==observed marker and unchanged identity/inactivity/current facts for verified;
contrary observation=mismatch, absent/unreadable/unbound=unavailable. Record observed_description.
Only verified marker plus all fresh normal deletion guards makes delete eligible; otherwise
stop ALL effects and retain residue/certainty for read-only recovery, never reconstruct authority.
After load_profile/identity validation BEFORE any client/session/REST/SSH, use existing config
model_copy(update={"max_response_bytes": min(config.max_response_bytes,262144)}); recheck identity/
trust unchanged, record selected cap, use same deterministic cap in run/recovery. No live mutation.
All REST responses use this cap/any stricter operator cap; ordinary32MiB default is not admitted.
Reserve extra1SSH<=65536B/20s +1REST<=262144B/20s (327680B/40s) for stamp/readback, plus full
pre-stamp guards/delete/post-baseline/logout. Complete-case counts/bytes/remaining suffix must
fit original caps BEFORE create and again before stamp/delete; unproved route refuses zero effects.
Offline proof: unknown create/stale guard/no reserve=>no stamp; MAX1; None/error=>no delete;
other/empty/date-shifted return stays acknowledged but cannot qualify; failed GET never downgrades.
Prove bounded-stream overflow refuses, stricter cap preserved and no authenticated config mutation.

## Failure model

1. **Actors and deployments**
   - Local CLI/direct-library operator and authenticated MCP client against selected HMC systems.
   - Classic and controller-mode creation; AIX/Linux/VIOS boot input; OS400 source restrictions.
2. **Invariants and assets at stake**
   - Selected partition/controller identity, ownership, unrelated XML and creation resources.
   - Exact stored pending text, separate configuration planes and surviving partial effects.
   - Nested targets authorized before effects; no false success from a write response.
3. **Accepted failure classes**
   - HMC may refuse a source-valid value; report refusal/uncertainty without retry or grammar invention.
   - A fresh observation can become stale after return; no distributed lock is promised.
   - Runtime bootability/automatic restart execution is not proved by stored configuration; no implicit power action.
   - Controller or missing-field readback may be unavailable; this blocks success/follow-ups, not an exclusion.
4. **Covered elsewhere**
   - Profile CRUD/preservation: #1724 and operations/lpar/profiles.py.
   - Immediate activation: operations/lpar/activation.py; transport/session auth: existing HMC client.
   - C11 serial eligibility/reciprocal semantics: operations/lpar/serial.py; C09 preserves its barriers.

## Threat model

1. **Boundary inventory**
   - Widened: typed CLI/MCP inputs to creation and pending-boot owners; nested controller identities.
   - Widened: fixed native creation record and boot text XML/native transport; independent provider reads.
   - Added: separate VIOS boot presentation, blocked until its transport is established.
2. **Actor model**
   - MCP callers and local callers may supply malicious names, lists, Unicode or attribute delimiters.
   - HMC responses may be malformed, stale, foreign or unavailable; other authorized writers may race.
   - Trust configured credentials/destination and existing ownership policy, not caller-supplied identity claims.
3. **Control per boundary**
   - Inputs: strict types/enums, mutual exclusion and the approved raw Unicode/16KiB application budget.
   - Targets: selected-system feeds, exact tuple checks, AllTargets dispatch and domain ownership checks.
   - Writes: finite attributes/routes, numeric resolved controller IDs, existing shell/record/XML separation,
     ETag for LPAR RMW, at most one submission, and no provider-directed URL traversal.
   - Reads: exact resource/profile containment, field-presence checks and fail-closed equality; no default coercion.
   - Output: bounded reason codes and terminal escaping; no raw command/provider body or text in generic logs.
4. **Explicitly out of scope**
   - Compromised HMC/server credentials and guest firmware correctness remain existing platform boundaries.
   - Runtime boot and power-cycle experiments require separate existing owner and hardware authorization.

## Verification inventory and delivery

The ignored plan maps twelve source bindings, strict input, contained identities, AllTargets,
family composition/barriers, exact independent observations, CLI positional yes, legacy
results/clear and negative uncertainty contracts to named tests. Tests isolate HMC environment.
Controlled faults must bite. Regenerate metadata/tool docs/changelog from implementation;
legacy evidence stays commit-bound. No AI surface or pure-prose snapshot tests are added.

## Rollback, feasibility and remaining gates

Before effects, refusal leaves no resource. After creation or uncertain update, preserve the
known identity and observations, stop follow-ups and require inspection; do not auto-delete,
auto-clear or auto-restore. A later authorized replacement boot value is a new operation.
Code rollback is a normal revert; it cannot undo stored HMC state or remove created partitions.

N1/N2/N3 remain approved; call_f0891f005b554495a602c49aee0c6026/0 adds only the plan’s bounded
VIOS fixture stamp. A2 approved; build/proofs/guardrails/CI and separate hardware remain.
