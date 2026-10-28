# D03 / #1776 paging-VIOS switchover design

## Status and authority

Proposed for formal design review and scope audit; no implementation or hardware grant.
Landed base 7a74c4513cd13ab50b33fd16432faea0e8003d01. C07 merged at
decf4770515648e5f30248174cc8ad1c1572e196; #1777 merged at this base. Borrowed API hashes and
remote completion/redaction provenance were reconciled against these landed Git objects.
Root confirmed the existing0287 reservation, docs/adr/0287-lpar-paging-vios-switchover.md;
no new reservation is made. This design retains EMPTY exclusions/M250.
Compact contract-plan format approved call_65842dc5d84c4b14887170cb35930280/0.
Governing accounting approved call_5f8116ae70774f95a0057d07d16b0c9a/0: full set read,
exclude only unchanged673 ADR0092 lines numerically; no implementation/review grant.
No-override interface approved call_bce9f35688384d17b6309510046fed67/0.
Scope is frozen under q1776-6c7a309d; two design passes are terminal, A1 remains pending.
Both source prerequisites are landed; design disposition is not implementation authorization.

## Frozen scope charter

- interaction: operator human campaign (interactive root)
- scope identity: D03/#1776 part of #638; https://github.com/randomparity/hmcpctl/issues/1776;
  annotation token q1776-6c7a309d; Campaign identity
  0eb20b6e4f5c-82a5e9af-4308-49db-9115-9d82028f5e3e
- outcome: One documented redundant paging-VIOS mem so via typed library/CLI/MCP.
- completion criteria: C1 documented fixed operation/presentations; C2 contained positive
  AMS/running-shared/configured-pair/current-redundancy/native prerequisites; C3 affected
  authorization/audit/concurrency/honest partial outcomes; C4 independently proven effective
  provider; C5 reference/negative/postcondition tests, generated docs/maturity and reproducible
  baseline/recovery procedure, full design/shipping gates. Sources: all Expected requirements of
  #1776, grouped C1–C5 without dropping the final live-evidence/design/shipping obligation.
- provenance: Explicit user Start20 against landed core plus unchanged EMPTY scope approval,
  campaign allocation row67. Campaign identity 0eb20b6e4f5c-82a5e9af-4308-49db-9115-9d82028f5e3e.
  Expected-provider/writer-window approval call_c684efc23e894540a5951f1a63d0def5/0; bounded HMC-
  final-eligibility approval call_4010b3f98c944df89e741e189da1b85b/0; interface/result approval
  call_bce9f35688384d17b6309510046fed67/0; compact-format approval
  call_65842dc5d84c4b14887170cb35930280/0; unchanged-673-line governing-accounting approval
  call_5f8116ae70774f95a0057d07d16b0c9a/0. Preserve mandatory expected current provider and
  coordinated writer window, no ownership override, and HMC final validation only for undocumented
  release/RMC/paging-service limits; positive AMS/running shared target/contained authorized
  providers/positive configured pair/independent current redundancy/required readback remain
  mandatory. Positive configured P/S with provenance and independently observed current pair R must
  satisfy {P,S}=R; no effective runtime role-orientation assertion. Core #1724/#1725/#1726 and
  source prerequisites #1768/#1777 are landed; C07 decf4770515648e5f30248174cc8ad1c1572e196 and
  #1777 merge/base 7a74c4513cd13ab50b33fd16432faea0e8003d01 were reconciled against actual Git
  objects.
- exclusions: EMPTY. Existing quantity/move/pool/profile owners are unchanged; no required D03
  field/context is excluded.
- surface: Prepared existing fixed SSH memory boundary, domain observation/operation/results, direct
  reuse of C07 identity helpers, LPAR CLI/MCP, direct tests, named ST66/group lpar-paging-
  switchover/scenario st66-lpar-paging-switchover and read-only recovery, metadata/docs, ADR0287 and
  exact six-line ADR0092 delta. Assignment: docs/adr/0287-lpar-paging-vios-switchover.md=0287.
  ADR0092 Status amendment and PLANNED row retain every existing row and record only the already
  approved operation-specific no-override contract. No helper extraction, parallel paging-
  configuration model or stable-facade export.
- ambiguities: None normative unresolved. Optional runtime field availability refuses before effects
  or remains unverified after an attempt; no inferred runtime orientation.

Scope authority: https://github.com/randomparity/hmcpctl/issues/1776#issuecomment-6477167322; canonical claim q1776-6c7a309d, G1/G2 verified.

## Source and ownership evidence

S1 P10/P11 docs/refs/hmc-commands-p{10,11}/commands/chhwres.md:108–110, examples
P10:390–392/P11:380–382: mem so accepts system and partition name|ID, no destination.
P10:218–219 limits target flags to moves; P10:234–236/P11:237–239 do not license w/d/force.
S2 native mksyscfg P10:41/P11:49 and chsyscfg both:49 declare configured primary/secondary
selectors; secondary ID=none means no secondary. Positive two-member configuration is required.
S3 REDP-4470-01 June2011 §4.4.10 printed56–57/PDF69–70 separates configured roles/current
provider; Example4-6 observes curr_paging_vios_name; Example4-4 printed52/PDF65 joins assigned
lpar_id/name, provider IDs/names, device names and is_redundant. Historical field semantics,
not current positive hardware evidence or universal version floor.
S4 retained current-memory XSD sha3043acbf...:578–610 optional read-only current-provider
link; :1275–1279 extends profile-memory type. Pool XSD sha2f95cc34...:380–410 actual
PagingDevices, :712–777 pool providers. Device XSD shac61a1e10...:99–132 scalar consumer
association, :333–372 actual redundancy, :253–292/:493–532 state vocabulary fields.
Collection XSD sha63704445...:145–159 permits zero/many devices. Raw parent profile-memory
body was not found in named custody; native fields, C07 saved reader and current inheritance
are available; do not pretend a missing raw declaration or optional endpoint value was captured.
S5 new Enumerations.xsd sha6220061f...:6980–7052 has six StorageDeviceState values with
TODO/ItemDescription, no usable-state guarantee. BasePartition_Links sha219580c5... declares
0..unbounded generic links, not a provider/device edge. Full capture digests/custody remain in the private source packet linked by workflow state.
S6 A1 approval call_c684efc23e894540a5951f1a63d0def5/0; A2
call_4010b3f98c944df89e741e189da1b85b/0: HMC final validation only for undocumented
release/RMC/paging-service limits; identity/redundancy/readback guards remain client requirements.
S7 landed profiles.py:829–864 validates associated-profile route; client_profiles.py:83–119
reads fixed contained profile and validates parent; :39–47 supplies profile_path.
S8 landed creation_memory.py:33–37 identity, :94–237 bounded reads/links, :306–370
pool providers/dedicated mode, :520–555 configured P/S, :574–581 creation default-profile read.
That creation-specific default-profile choice is not D03's running configured reader.
S9 memory_move.py:227–310 one-attempt fresh/authorized peers; ownership.py:384–427 audit,
:471–497 precheck wrapper, :500–525 approved bounded authorize_lpar_mutation guard;
ssh/commands.py:275–311 permissive duplicate-key overwrite; tool_registry.py:169–187 all-targets.

## Interfaces and state

Proposed library, no new request model:
 switch_lpar_paging_vios(hmc:HMCClient,lpar_name_or_uuid:str,*,system_name_or_uuid:str,
   expected_current_paging_vios_name_or_uuid:str,exclusive_writer_window:bool)->PagingSwitchoverResult.
All selectors nonblank strings; name-or-UUID semantics, no numeric-string PartitionID coercion.
Window must be literal bool true before clients/effects. No ownership override is added; existing
unstamped acceptance and foreign/malformed denial/audit apply to consumer and both providers.
For each contained target call authorize_lpar_mutation(hmc,canonical_system_name,
canonical_partition_name,ownership_override=False,max_output_bytes=5*1024*1024).
Retain its existing audit; the operation locally replaces PermissionError denial text with
"Paging switchover has no ownership override; resolve ownership before a fresh authorized attempt".
For HMCCLIError raised by that guard, including its description-read wrapper, use safe precheck
text requiring restoration of readable ownership metadata before fresh authorization. Preserve
exception class, suppress the unsafe chained message, emit no raw cause/override advice, and send
no switch. Leave the shared helper unchanged; precheck errors are never submission evidence.
No stable facade export, generic proxy or configurable source predicate is added.

Proposed CLI: lpars paging-switchover PARTITION --system SYSTEM
 --expected-current-paging-vios NAME_OR_UUID --exclusive-writer-window [--yes].
--yes skips confirmation only, never supplies the window acknowledgment. Use asdict(result)
then existing print_json; non-completed result exits1 after JSON, invalid input uses usage_error.
Proposed MCP hmc_switch_lpar_paging_vios uses the four required selectors/window arguments
above plus profile:str|None=None. effect=mutate, operation=lpar.switch_paging_vios,
target_kind=lpar, automatic partition/system selectors and one extra expected-provider vios selector,
exhaustive_targets=False. Discovery of the other provider requires all-targets; a scoped table
cannot grant it. Domain ownership checks remain unconditional after dispatch authorization.

Proposed immutable records (fields stated completely; no incidental framework):
- PartitionIdentity(uuid:str,name:str,partition_id:int,kind:PartitionKind).
- ProviderIdentity reuses landed C07 PagingProvider(uuid,name,partition_id).
- ConfiguredPair(primary:ProviderIdentity,secondary:ProviderIdentity,plane:Literal[current-configuration,
  associated-profile],profile_uuid:str|None,profile_name:str|None,setting_id:str|None,etag:str|None).
- PagingObservation(system_uuid:str,system_name:str,target:PartitionIdentity,
  pool_uuid:str|None,pool_id:int|None,
  providers:tuple[ProviderIdentity,...],configured:ConfiguredPair|None,
  paths:tuple[tuple[str,str],...],unique_device_id:str|None,
  uom_redundant:bool|None,native_redundant:bool|None,
  current_uom:ProviderIdentity|None,current_native:ProviderIdentity|None,
  state:str|None,shared:bool|None,provider_dedicated:tuple[bool|None,...],
  states:tuple[tuple[str,str|None],...],unavailable:tuple[str,...],invalid:tuple[str,...]).
  paths entries are providerUUID/localDeviceName; states are labelled raw service observations.
- PagingSwitchoverResult(system_uuid:str,target:PartitionIdentity,expected_current:ProviderIdentity,
  expected_other:ProviderIdentity,submission:Literal[acknowledged,remote-error,unknown],
  observation:Literal[expected,unchanged,other,unavailable],
  preservation:Literal[verified,drifted,unavailable],outcome:Literal[completed,partial,rejected,
  unverified],workflow_completed:bool,before:PagingObservation,after:PagingObservation|None,
  remote_refusal_code:str|None,diagnostics:tuple[str,...]).
Prewrite ValueError/TypeError/PermissionError or existing HMCError propagate with no submission;
post-attempt boundary failures become retained unavailable facts. Programmer errors are not swallowed.

## Minimal record placement and direct reuse

Borrowed prerequisite contract: HMCCLIError.remote_nonzero_completed defaultsFalse;
only actual ProcessError with strict positive integer exit_status and no exit_signal setsTrue,
retained through declared-secret reconstruction, now landed via #1777 at7a74c451.
Map only explicitTrue to remote-error, coded or uncoded;
all opaque/absent metadata, nonpositive/bool/missing status, signal and timeout stay unknown.
Existing remote_refusal_code is a separate optional safe diagnostic, never completion proof.

Directly import landed C07 PagingProvider,_entries,_read,_detail,_identity,_inventory,
_provider_uuid from operations/lpar/creation_memory.py; keep that owner and helpers in place.
Necessary C2 containment/C4 readback prerequisites correct only UUID identity comparisons in
creation_memory.py::_provider_uuid and client/client_profiles.py::_validate_parent/read_native_profile.
Follow ADR0094:151–153, ADR0092 UUID identity policy and ADR0160: validate UUID segments, then
casefold only their comparisons. Preserve exact resource/path literals, origin/route checks,
either-case callers and existing returned-UUID policy; never decode links or lowercase arbitrary
strings, SettingID or preserved raw profile XML. No new helper framework or ownership transfer.
Cover these owners in tests/lpar/test_creation_memory_workflow.py and tests/unit/test_client_profiles.py;
D03 associated/configured/runtime joins retain the same UUID-only equality semantics.
PartitionIdentity,ConfiguredPair,PagingObservation live with their actual reader owner,
operations/lpar/paging_observation.py. PagingSwitchoverResult lives with the one-attempt
owner operations/lpar/paging.py, which imports observation records and never vice versa.
No documents module imports operations; no new paging_identity/documents utility is needed.
This matches landed memory_move.py:20–29 private sibling reuse and profiles.py:53–98
operation-owned records. C07 imports1–28 contain no D03 dependency; no extraction is
necessary to break a demonstrated cycle. Landed imports match the borrowed helper contract.

## Configured-pair reader and provenance

Read the exact contained target's raw PartitionMemoryConfiguration. If both configured P/S
fields are present, nonempty single valid provider links, normalize them as current-configuration;
no profile identity is implied. Partial/malformed/empty configured fields are unavailable/invalid,
not a reason to search another plane for more convenient values.
If both fields are absent, accept only the target's positive AssociatedPartitionProfile link:
validate its shape and exact same-HMC selected-system/kind/target profile route like S7, extract
UUID without fetching that returned URL, then call read_native_profile(hmc,kind,targetUUID,
profileUUID,systemUUID). Reuse existing parse_profile bounds32MiB/128depth/100000nodes;
require parent association, UUID/name/SettingID and
positive ProfileMemory P/S. Recheck the target's association binds that same profile in final and
post reads. Label evidence associated-profile saved configuration, never effective current proof.
No default_profile, DefaultProfileName, LastActivatedProfile, arbitrary chosen profile or guessed
controller mapping is a fallback. If neither positive plane exists, refuse; after effects unverified.
ETag is retained when available but no write concurrency is inferred from it; relevant configured
pair/provenance facts are re-read. Configured role ordering stays P/S; current path orientation is separate.

## Current assignment, intent and one-attempt algorithm

A complete observation uses positive selected AMS, contained target running/shared facts, no
positively reported conflicting operation, complete bounded selected inventory/pool/provider details.
Two pool providers resolve uniquely to distinct dedicated-memory VIOSes and are authorized.
Fixed native readers, each at most once per observation:
 lshwres -r mem -m QUOTED_SYSTEM --level lpar
 lshwres -r mempool -m QUOTED_SYSTEM --rsubtype pgdev
Critical CSV keys must be unique and typed before dictionaries can overwrite them; reject ambiguous
rows/truncation/duplicates. Unknown noncritical fields cannot extend a critical scalar as bare fragments.
Select exactly one pgdev assignment by positive lpar_id and exact contained lpar_name. Require
is_redundant=1, distinct provider IDs/names resolving to the same two pool VIOSes. Select exactly
one actual UOM PagingDevices child associated with target UUID/kind; require IsRedundant=true,
positive nonempty DeviceName/RedundantDeviceName matching native names as a multiset. Preserve
native canonical providerUUID/localDeviceName mappings; identical local device names are legal.
Free-device lists or RedundancyCapable do not establish current assignment. A missing/ambiguous
consumer association refuses. No native UDID field is guessed; retain UOM UniqueDeviceID if present.
Require configured {P,S}=current pairR. Require UOM current-provider link and unique native current
row by exact contained lpar_name to agree inR; native lpar_id, if reported, must also agree.
Expected current resolves uniquely withinR to that identity; derive the unique other member.
No state enum is converted into Active-only readiness; A2 delegates undocumented service limits.

Sequence: validate local request and finite-positive config.ssh_timeout before any new paging
SSH or ownership-description read; resolve system/target; observe complete before; authorize all3;
observe required facts fresh again and authorize all3; refuse stale/changed intent/invariants;
submit exactly one fixed chhwres -r mem -m QUOTED_SYSTEM -o so --id POSITIVE_ID;
independently observe after even on transport/native failure; reduce result. No retry/poll/fallback
writer/automatic reverse. Each independently callable new SSH entry (both fixed readers and
the switch submitter) rejects non-finite/non-positive config.ssh_timeout before transport.
Reuse the existing finite-positive value/transport; no global default or shared framework change.
Finite admitted timeouts/output limits bound local waiting, not remote completion.
After-attempt observation retains readable planes despite other required-plane errors. Cancellation
propagates; named live checkpoint and independent recovery handle interruption, not blind replay.

Invariant vector: system UUID/name; target UUID/name/ID/kind/running/shared; pool UUID/ID;
configured positive P/S ordering and provenance plane/profile identity; provider identities/dedicated
mode; actual consumer-associated redundancy and canonical provider/device path mappings;
UniqueDeviceID presence/value when originally observed. Ignore row order/counters/uptime/service
state changes for immutable-equality checks; report state changes separately. Missing required
facts are unavailable, contradictory valid facts drift. A changed configured plane is unavailable
for like-for-like preservation rather than silently accepting a different proof source.
Postcondition requires invariant preservation plus both current readers agree on expected-other.
Compare every readable invariant independently: any valid observed invariant drift sets
preservation=drifted even if other facts are unavailable; retain both drift and unavailable
diagnostics. Otherwise missing required preservation evidence sets preservation=unavailable;
only complete matching required facts set verified. Current-reader disagreement or a positive
current outsideR is valid contradictory evidence; missing current alone is unavailable.
Reduce in this exact order for every acknowledged/remote-error/unknown submission:
1. Fully verified expected-other and preservation=verified yields completed for all three
   submission values. Retain native error/unknown status and diagnostics; make no causal claim.
2. Any valid observed invariant drift yields partial, including drift plus unavailable facts.
3. Otherwise any required unavailable evidence yields unverified.
4. Otherwise preserved unchanged plus remote-error yields rejected.
5. Remaining acknowledged/unknown unchanged cases yield unverified; there is no natural noop.
workflow_completed is exactly outcome==completed. Rejected means the requested change was not
observed after definite native failure, never a promise of zero unobserved effects.
Only bounded safe phase/reason diagnostics and remote refusal code escape; no raw remote bodies.

## Failure model

1. Actors/deployments: authenticated library/CLI/MCP operators on supported HMC contexts,
   concurrent administrators and automatic failover; offline CI transports are mocks.
2. Invariants/assets: correct contained consumer/providers, ownership/access policy, paging
   availability, no double-toggle, honest configured/runtime provenance and retained partial facts.
3. Accepted classes: A2 undocumented release/RMC/service rejection by HMC; a final check/command
   race and unavailable verification are possible; report independent facts without causal claims. No
   atomicity, liveness, guest-health or rollback guarantee is made. Missing required client
   identity/configuration/redundancy/current proof is not an accepted prewrite bypass.
4. Covered elsewhere: credentials/host keys and admitted finite timeout execution by SSH transport;
paging-local finite-positive timeout admission remains this feature’s responsibility; REST auth by HMCClient;
   quantities/moves by D01/D02; pool lifecycle642; profiles1724; creation/config groundwork1768.

## Threat model

- Added boundary: selectors/window from authenticated callers; strict local validation,
  all-targets dispatch and existing three-resource ownership authorization govern effects.
- Widened reads: raw HMC XML/CSV/links and associated-profile evidence; bounded complete-input
  bounds (UOM/CSV5MiB; existing profile owner32MiB/128depth/100000nodes), hardened XML,
  exact namespace/multiplicity, same-origin fixed routes, critical CSV
  uniqueness, contained identity and independent joins prevent foreign/ambiguous evidence.
- Added writer input: only canonical positive integer targetID and shlex-quoted canonical system
  reach fixed so; no generic args, arbitrary href requests, secrets or raw response leaks.
- Trust: HMC observations may be incomplete/contradictory; independence detects inconsistencies,
  not a malicious trusted HMC's consistent lies. Credential theft/HMC compromise are held by
  existing transport/deployment policy; no dependency or permission broadening is proposed.

## AI-surface evaluation and success

The deterministic MCP tool serves an authorized operator/agent request carrying exact selectors,
expected-current and window; returns typed observed facts. Allowed sources are fixed contained
HMC reads. Disallowed behavior includes guessed current/provider identities, replay, rollback and
success from submission. Missing proof refuses/unverifies. Use existing client and admitted finite-positive SSH timeout budgets,
no extra model calls/judges/cost. Success is one correctly authorized command and independent proof.
Evaluate wrong expected, missing acknowledgment, scoped-only grant, foreign provider, malformed
source, timeout and incomplete postcondition with deterministic direct MCP/library tests; require
zero write on negatives and no replay on post-attempt errors. No model judge is warranted.
C1–C5 pass only when named direct tests/generators/guardrails succeed; unrun runtime contexts stay
unevidenced. Library/MCP serialize typed facts consistently; CLI returns1 on incomplete outcomes.

## Live scenario and recovery

ST66/group lpar-paging-switchover/scenario st66-lpar-paging-switchover uses already
configured redundancy; it creates/allocates nothing. Recheck collisions at adoption.
Preflight pins clean deployed commit/tree, destination, fixture ownership, positive before vector,
expected-current/window and private durable checkpoint. Run one forward attempt; independently
capture expected provider and all retained facts. Always run read-only recovery against original
baseline; changed current provider remains drift/held, never clean by adjusting the baseline.
A later reverse requires separate explicit operator intent, fresh expected-current/window,
authorization and its own attempt/journal. Never invoke reverse from the arm, finally or recovery.
Preserve original drift/recovery history; a later independent original-baseline confirmation is
separate evidence. No passing maturity is inferred from a forward change with unresolved recovery.
Procedure documents this deliberate hold/manual-restoration path; no hardware run is a merge gate.
Use existing preflight→named wrapper→recovery→filtered evidence generator; raw artifacts stay private.

## Validation and implementation bounds

Contract plan inventories domain/transport/presentation/source/live tests and exact commands.
D1/D2 findings are dispositioned within the frozen scope; A1 remains required. No passing
implementation or hardware evidence is claimed. Proposed implementation estimate
3000–4500 gross changed lines; M250 remains fixed. No tool/dependency floor or public facade change.
Required before delivery: focused meaningful red/green tests, source-shaped negatives, regenerate
metadata/tools/scenarios, just verify, pinned prek all-files and supported Python3.11–3.14 CI.
Normal full scope/design/audit/product/security/shipping gates and existing0287 assignment remain.
