# LPAR creation memory layout

## Problem and authority

Issue #1767 owns six CREATE fields and twelve P10/P11 bindings: minimum, desired,
and maximum huge-page counts, HPT ratio, PPT ratio, and KVM reservation percentage.
The [frozen scope](https://github.com/randomparity/hmcpctl/issues/1767#issuecomment-6399316454)
has empty exclusions and retains M/full-spec/250. Tasks1–5 are implemented at
e28fd0eb against integrated main816. Proposed revised design pending normal
D2/A2 and root implementation release; no implementation or hardware is granted.
[ADR0295](../../adr/0295-lpar-creation-memory-layout.md) records the retained architecture boundary. Its historical
status line is not current phase authority; the proposed-design hold governs work.

## Source contract

The exact six selectors occur in `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`
and `docs/refs/hmc-commands-p11/commands/mksyscfg.md:49`. Huge-page counts apply to
AIX/Linux. HPT/PPT values must belong to the selected system's respective
`possible_hpt_ratios` / `possible_ppt_ratios` advertisements. KVM reservation is
Linux-only and accepts at most two decimal places. Selecting it, including zero,
declares Linux intent; there is no extra intent parameter or installed-OS claim.

The operator explicitly approved finite-number/two-decimal validation while leaving
undocumented KVM numeric limits to HMC final admission. Do not invent 0..100 or a
`kvm_capable=true` dependency. The captured profile schema's optional CUD float
without numeric facets establishes vocabulary, not acceptance of NaN/infinity.
Huge pages occupy a separate configured system pool; their bytes are not compared
against ordinary desired partition memory. There is no pool reconfiguration here.

## Architecture and data flow

Add one creation-only `LparMemoryLayout`, carried by `LparCreation.memory_layout`
and the existing VIOS creation owner for HPT/PPT fields only. HPT/PPT are unrestricted
by OS in the exact source rows; huge counts are AIX/Linux only and KVM reservation
is Linux-only. OS400 therefore permits ratios but rejects huge counts/reservation;
VIOS permits ratios through its existing create operation/MCP/CLI seams.
Reuse existing command record encoding, native creation, containment, ownership
stamping, profile application and workflow result mechanisms. Do not extend
`LparResources`, whose update consumers would silently widen this creation slice.
No ownership transition or compatibility surface is needed.

The model preserves omission and explicit zero. Counts are strict nonnegative
integers; ratio values are strict advertised scalar tokens. Reservation is a finite
Decimal value with at most two fractional decimal places. CLI tokens remain text
until decimal validation and use no binary-float conversion before serialization;
values that would round to two decimal places as floats are rejected.
Compare each supplied pair of min/desired/max counts without inventing omitted
HMC defaults. Any supplied field selects a single native `mksyscfg` create record;
explicit keylock is refused because that native path cannot represent it.

Before creation, resolve the selected system and retain existing duplicate-name,
authorization and ordinary-memory checks. Read ratio advertisements only when
needed; refuse missing/malformed required advertisements or an unadvertised ratio.
`docs/refs/p10jau.md:2486` establishes that huge-page shortage prevents activation,
not profile creation. Its system-pool display at :1815 does not establish a create
reservation. Do not reject a saved profile because CURRENT free huge pages are low.
Integral page counts supply alignment; changing the system pool requires power-off
(:1817) and is outside this operation. Do not estimate hypervisor overhead or infer
a processor generation's allowed ratios.

After classic-mode creation, independently read exactly the selected
system/partition's `default_profile` and the six fixed native columns. Match partition/profile names,
require one well-formed CSV row, parse counts/percentage strictly, and compare only
supplied values using numeric equivalence for percentage. Missing identity/readback, malformed output, or a mismatch produces an error
memory-layout workflow step and stops later assignments. A typed verification
result distinguishes `verified`, `mismatch` and `unavailable`; observed contradictory
values in a strictly valid row remain mismatch when other requested fields are null
(missing); unknown tokens or malformed rows make the whole observation unavailable.
Existing profile application semantics stay explicit;
readback verifies saved configuration, not guest behavior or bootability.

Controller mode is admitted: P11 `mksyscfg.md:39` forbids profile_name and
sync_curr_profile, configures the specified attributes immediately and requires
current HMC controller authority. Reuse C05's reviewed controller context; do not
invent profile names, apply a named profile, or reject layout fields for mode alone.
The landed C05 mode-only getter is reused with need_kvm=False and
need_management=False for layout-only selection.
The current schemas establish huge-page and HPT vocabulary, but do not establish a
complete equivalent getter for the six supplied configured values. In particular,
CurrentHugePageCount can concern allocation rather than the requested desired count,
and KVMReservedMemory is proven only on the profile root. No exact positive current
getter mapping is established by this design. Controller creation therefore reports
requested fields as `unavailable` after its single write and stops assignments; it
never reports complete verification or queries default_profile. Add a positive
current-field comparison only when its exact configured-value mapping is established.
This preserves admitted inputs and honest unavailable outcomes, not guaranteed
visibility. No controller, guest or maturity support is inferred from schema names.

## Results and failure model

- Prevent: malformed inputs, nonfinite/overprecision values, unsupported partition
  field context, supplied ordering conflicts, unadvertised ratios,
  and native keylock conflicts before creation effects.
- Detect/report: native refusal or transport uncertainty; partition created with
  absent readback, mismatch, or stamping/application failure. Preserve the existing
  partition and identity; do not retry a create or silently delete it.
- Accepted: huge-page capacity is checked at activation; HMC owns undocumented KVM limits;
  saved-profile equality does not prove activation, installed Linux, or KVM runtime.
- Outside deployment proof: no HMC window is authorized. The available campaign
  target advertises KvmOnPowerVM=false; it cannot prove positive KVM semantics.
  This observation is target-specific, not universal absence of support.

Creation is for a new powered-off partition; classic mode also creates its profile. No dynamic update operation
is introduced; later activation consumes the profile under existing behavior.
Native errors remain unknown unless scoped reconciliation proves no partition
exists; a failed command alone does not prove absence. Successful create with
unavailable verification is incomplete, with no automatic rollback.

## Presentations and validation

MCP adds optional typed `memory_layout`. CLI adds six optional flags matching native
field names with hyphens. Both construct the same model and pass it unchanged.
Existing VIOS create adds ratio-only inputs and uses its existing native-create path,
retaining C04 policy/vTPM composition, explicit-resource prerequisite and scoped
identity/profile verification. It introduces no ownership mechanism or profile apply.
Other creation/provision callers retain the empty default. No stable-facade export.

Focused tests cover strict values, omission/zero, partial ordering, decimal precision,
quoted advertisements, missing advertisements, low/zero pool capacity without invented
creation rejection, malformed/duplicate
CSV rows, foreign profile identity, one native submission, no retry, readback mismatch,
unknown outcomes and workflow assignment suppression. A controlled fault must turn
each new contract test red before its green result is accepted.

Bind all twelve source selectors in the existing capability ledger without claiming
live verification; regenerate runtime metadata/tool docs/scenarios by their recipes.
The reproducible live procedure uses preflight, a fresh owned partition, scoped
post-create readback, and recovery even after success. It records unsupported target
prerequisites and cleanup disposition; no live command runs during design.

Run focused tests during implementation, relevant static gates before commits,
`just verify` and `uv run --no-sync prek run --all-files` before push. Shipping also
requires the existing product/security review workflow and CI version matrix.

The operator approved a software representation boundary on 2026-10-25:
canonical fixed-point KVM reservation encoding must be at most 65536 ASCII bytes,
checked before expansion. Canonicalize signed zero and coefficient trailing zeros
without ambient Decimal context rounding. This is a software resource policy,
not an HMC numeric interval or permission to use native scientific notation.
Authority: issue #1767 comment 6397876848. Existing D1/A1 and M250 remain retained.

## Retained implemented interfaces and validation

The following completed-task contracts remain operative; their old sequencing and
phase holds do not. LparMemoryLayout is frozen, extra keys forbidden, six fields
all default None: three strict nonnegative ints (bool refused), two strict strings,
one finite Decimal. Supplied counts are ordered without invented omitted bounds.
Ratio lexical grammar is [A-Za-z0-9_.:/+-]+, excluding null/none; selected-system
advertisement membership is independently mandatory. Native pairs omit None,
preserve zero/signed zero and exact Decimal canonical fixed strings. The65536-byte
limit is checked before expansion; coefficient zeros are stripped without context
rounding. There is no borrowed SR-IOV interval or scientific native grammar claim.

Existing interfaces: LparMemoryLayout.pairs()->list[tuple[str,object]] and
validate_creation(partition_type)->None; admit_memory_layout(config,system_name,
layout)->None; get_lpar_memory_layout(config,system_name,name)->LparMemoryLayout
is a PRODUCT classic-only getter. Advertisement reads occur only for requested
ratios; require one row, distinct nonempty valid tokens and requested membership.
The product getter uses exact name/default_profile identity and eight fixed columns,
strict ASCII counts, Decimal finite/precision validation, null as missing and strict
observed model validation. No pool reservation inference or new transport.

LparCreation.memory_layout defaults empty; LparCreationResult.memory_layout_step
is optional. Native create receives optional layout and emits one existing record.
MemoryLayoutVerification retains status verified/mismatch/unavailable, requested,
observed, unavailable and warnings; contradictions in a strictly valid row take
precedence over null (missing) requested fields. Unknown tokens or malformed rows
make the whole observation unavailable. Empty layout adds no reads/steps/native
selection. Mode-only context never
borrows KVM/management prerequisites; real roles retain independent admission.
Controller apply/profile-I/O/profile-assignment denials are preserved. Ownership,
C04/C10/vTPM/reporting/settings/I/O verification independently gates application and
assignments when mixed. Selected-layout failed-string diagnostics are sanitized.
Only the native WRITE failure enters one scoped reconciliation: coded refusal with
absence => rejected; valid created identity => partial; transport/unreadable scope
=> unknown. Admission errors stay pre-write errors. No retry, stamp or deletion of
unconfirmed partial resources; malformed returned identities cannot be stamped.

CLI constructs/validates before confirmation/client effects, parsing reservation
text directly with Decimal; 0.0099999999999999999 and1.00000000000000001 are refused.
MCP keeps typed strict/extra-forbidden values. VIOS owner permits ratios only,
requires explicit resources, canonical system and mode-only context, preserves
security/vTPM/hardware composition, and returns scoped verified entry or honest
incomplete HMCError without replay/rollback. Existing update/provision/facade
surfaces stay unchanged. Metadata adds only exact C06 contracts/test/row links;
prior states/missing scopes/evidence and shared-family accounting remain intact.

## Proposed ST58 reproducible arm

Tentative root assignment: SUBTASK58, GROUP creation-memory-layout, scenarios
st58-lpar-memory-layout and st58-vios-page-table-ratios, named script
scripts/live_creation_memory_layout.py. Recheck visible registrations/live claims
immediately before implementation; do not choose another ID silently. Preserve ST55
and exclude58 from default/all. Reuse existing runner bootstrap, served policy,
checkpoint/results/observation machinery and ST55/lpar_config lifecycle helpers;
no generic framework, product tool/facade, arbitrary proxy or new transport.

The wrapper requires --results-file, using the existing live_system_profiles
fresh private outside-checkout owner/permission checks and refusing existing result
or observation sidecars BEFORE bootstrap. Help must never start a hardware session.
Three optional .env-only LiveTestConfig fields use existing parsing/default rules:
creation_memory_layout_hpt_ratio:str="", creation_memory_layout_ppt_ratio:str="",
creation_memory_layout_fresh_vios_delete_authorized:bool=False. LIVE_TEST_ keys
are uppercase field names. Invalid/empty ratios or false separate fresh-delete
permission SKIP the full arm before writes; actual advertisements still gate each
product creation. These are test settings, not product intent/override parameters.

Saved-document compatibility: decode_saved_config backfills ONLY missing new keys
with empty HPT, empty PPT and False BEFORE its exact-key comparison; constructor
defaults alone are insufficient. decode_artifacts defaults a missing journal to{},
a valid unarmed empty record. Explicit malformed values/types or unknown keys still
refuse; never replace explicit values with defaults. Preserve valid non-ST58
round2_inputs_from_document, runner restoration and read-only recovery. ST58 prior
output/current-or-old artifact restoration remains refused before effects; a saved
boolean never grants current VIOS deletion authority. No migration engine/new API.

LiveTestArtifacts.creation_memory_layout is a closed dictionary. Keys:
invocation_id,candidate_commit,system_uuid,hmc_identity,phase,lpar_name,lpar_token,
lpar_uuid,vios_name,vios_uuid,preabsence,create_returned,
fresh_vios_delete_authorized,requested,baseline,outcome,deletion.
Validate canonical UUIDs, existing HMC identity shape and types. preabsence and
create_returned map exactly lpar/vios to strict booleans; requested maps typed
six-field LPAR and ratio-only VIOS inputs. baseline holds the existing pool/LPAR
snapshot plus a sorted unique VIOS UUID/name list. outcome values: not-attempted,
rejected,created-verified,partial,unknown,unavailable,mismatch. deletion values:
not-attempted,pending,confirmed-absent,unknown,refused. Both map exactly lpar/vios.
Phases: preflight,lpar-create-pending,lpar-create-returned,lpar-delete-pending,
lpar-deleted,vios-create-pending,vios-create-returned,vios-delete-pending,
vios-deleted,complete,stopped-uncertain. Checkpoint BEFORE every mutation; failure
stops writes. Refuse prior ST58 output/restored artifacts, not resume/adopt them;
do not import round2/vmedia saved context for this isolated invocation.
Only acknowledged returned VIOS UUID enters vios_uuid; diagnostic name-read UUIDs
stay ordinary private rows. Saved permissions never authorize recovery mutations.

## Finite matrix, identity and deletion

Run two sequential fresh fixtures, at most one live at a time, with the existing
explicit lpar_config.RESOURCES (2GiB desired/0.5 shared units). LPAR uses existing
scratch prefix/eight-hex suffix and matching caller token. VIOS uses
hmcpctl-live-c06-vios- plus its own fresh eight-hex suffix, no stamp. A separate
128-bit invocation_id identifies the journal. Refuse collisions/stranded scratch;
record both names absent, selected-system UUID, LPAR/pool and VIOS feed baselines.

LPAR: AIX/Linux, count0/0/0, both supplied advertised ratios, Decimal0 reservation,
apply_partition_profile=False, no added roles/hardware/security/vTPM/I/O/storage.
VIOS: the same two ratios only and explicit resources through hmc_create_vios.
OS400 remains supported/offline-tested but unevidenced by this minimal physical
matrix; any later native OS400 window needs its actual prerequisites/grant. Huge
activation and positive KVM runtime remain separately unevidenced. KVM=false is
not a layout admission prerequisite or proof of universal lack of support.

Require acknowledged canonical UUID plus independent selected-system containment,
exact fresh name/type/inactive identity; LPAR also requires current ownership/token.
No next fixture after rejection, uncertainty, unavailable/mismatch, failed cleanup
or baseline drift. No retry/fallback/reordered fixture, power-off, apply/activation,
pool/storage/adapter/controller takeover or existing-VIOS mutation. LPAR cleanup
reuses _teardown(inactive_only=True), token/UUID/absence and baseline comparison.

VIOS deletion uses ONLY existing hmc_delete_vios(vios_name_or_uuid=returned_uuid,
system_name_or_uuid=selected_uuid). Its existing destructive vios.delete policy
must separately permit the conditional fresh target; creation permission is not
such permission. The actual tool has NO ownership_override/stamp/caller argument.
Its owner resolve_vios_uuid enforces scope, quick PartitionState must be not
activated, and client deletes only VirtualIOServer/{UUID}. Do not bypass this path.
Root/operator permission must specifically cover only this invocation's successful
returned UUID, contingent on preabsence and fresh independent inactive containment.
Reject returned UUID already present in baseline; checkpoint it before further work.
Refresh identity/state before one delete; require independent absence afterward.
Denied/unknown delete is not retried or replaced with raw delete/power-off.

Partial/unknown create WITHOUT successful returned UUID: name rediscovery is
DIAGNOSTIC ONLY, never adoption/deletion. Persist intent and stop; even controller
unavailable creation may leave an orphan. Read-only recovery reports every pending
phase and changed baseline, never promotes saved authority to cleanup permission.
Root must separately reconcile/authorize any later UUID-specific orphan cleanup.

## Independent configured-value oracle and request encoding

Before EACH raw profile read, reobserve mode with need_kvm=False and
need_management=False; controller/unknown/drift means unavailable and NO profile
query. Independently confirm returned UUID/name/system/inactive identity around
that read. Use existing hmc_run_command with this one fixed source-grounded shape:
`lssyscfg -r prof -m {quoted_canonical_system} --filter {quoted_filter} -F lpar_name,name,min_num_huge_pages,desired_num_huge_pages,max_num_huge_pages,hpt_ratio,ppt_ratio,kvm_reserved_mem_percent --header`
Filter exactly lpar_names=fresh_name,profile_names=default_profile through existing
build_filter and shlex.quote. Require served tool/policy permission before writes;
no user command, alternate projection, fallback or retry. Vocabulary: P10 mksyscfg41/
P11:49; read grammar: both lssyscfg30,34–36. No current-target service proof inferred.

Generic CSV/filter/quoting primitives may be shared. NEVER call/import the product
get_lpar_memory_layout, its column constants, model mapping/field iteration or
creation result as the oracle. Require PASS/string, exact literal eight-column
header order/width, exactly one row, no malformed/duplicate rows/headers, literal
fresh lpar_name/default_profile identity. Independent LPAR expectations: three
literal count keys are ASCII integers equal0; literal hpt_ratio/ppt_ratio equal
admitted operator strings; literal reservation is independently finite Decimal
numerically equal0 without float/normalize(). VIOS compares only literal ratio
keys; other projected columns confer no VIOS admission claim. Missing requested
values => unavailable; known contradiction takes precedence as mismatch.

Typed requests use ONE inline TypeAdapter(LparMemoryLayout).dump_python(layout,
mode="json",exclude_none=True) conversion, no serializer helper/native-pairs loop.
Literal assertions require exact six LPAR/two VIOS keys, zero int counts, exact
ratios and Decimal string. Validate real MCP request JSON roundtrip without floats.

Fault test: product getter returns requested HPT1:128 while honest native CSV says
HPT1:64. Production verification may falsely succeed; independent oracle MUST
mismatch, prevent positive observation and next fixture. Restore. Also demonstrate
raw match/product mismatch disagreement never overrides an incomplete creation.

## Observations, physical allocation and release boundary

One aggregated configured-readback observation per assigned scenario, using
record_verified only when independent assertions AND cleanup/baseline pass. Preserve
rejected/partial/unknown/unavailable handles in private ordinary rows; expected
refusal can pass a negative assertion without proving positive creation. Generate
redacted public evidence only from actual asserted candidate/recovery results.
Implementation/source vocabulary/probes do not promote live maturity.

Logical maximum:2creates,1LPAR stamp,2deletes. Physical request/time caps are NOT
DERIVED or granted. Independent full-four-adapter baselines can need21SSH EACH
(42pair), plus2raw oracle calls, product/admission/stamp/cleanup, shared pool
comparisons, authentication and recovery. Shared _read_pools uses3SSH and _compare
may read it twice; account each actual occurrence.40SSH is not feasible for that
composed worst case. Before allocation root derives finite counts/per-call timeouts,
staggered absolute parent/child deadlines and own-session/reaping reserve from the
actual composed graph. Exhaustion stops writes/retains intent; never expand caps
mid-window. Attest deployed HEAD/imports/runtime/tools/policy before a separate root
grant. No hardware, review dispatch or implementation is authorized by this proposed revised design.
