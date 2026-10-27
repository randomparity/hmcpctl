# VIOS boot-source diagnostic design candidate

Status: proposed under approved charter and format; independent review pending.
Design authority: call_dfc2b0181f834c4083aafd06c2a39556/0; frozen charter/routing in
https://github.com/randomparity/hmcpctl/issues/1770#issuecomment-6442564697. Base056cdc8006eb04323323896d126ec6f4a9929cb0.
Reserved decision: [ADR0306](../../adr/0306-vios-boot-source-diagnostic.md).
One cohesive diagnostic PR; validated L/1000, full-spec, exclusions EMPTY.
Parent #1770 C09 remains six fields/twelve bindings, M250/EMPTY; product G4 stays with C09.
This design uses the operator-approved diagnostic-only contract-plan format instead of full
implementation bodies. Normal design review and scope audit precede implementation.
Implementation and hardware execution remain held; normal design review may now proceed.

## Global Constraints

Project Python floor: >=3.11. CI target: Linux amd64 ubuntu-24.04 with Python3.11–3.14.
Bound live source runtime: Python3.11.15; host architecture does not establish target proof.
Use existing stdlib csv/io/shlex/json/hashlib plus installed repository dependencies only;
no dependency, toolchain floor or provisioning change. Use uv run --no-sync and sibling worktrees.
No product API/facade compatibility exports, default/all live route or generic framework.
Source-only outputs protect private identifiers; no product maturity or bootability claim.
The exact12-path implementation surface, one encoder/eight values and all protection gates
remain fixed. Root owns current operational grants; packet flags and records cannot grant them.

## Success and authority

The diagnostic must truthfully qualify, reject or hold the fixed candidate on the observed
release, independently verify pending text and full protected preservation, and retain a
bounded once-only effect/session history. Positive qualification requires all eight cases,
confirmed fixture deletion, matching B0 and clean capture plus independent recovery sessions.
A rejected or held result is useful source evidence; it does not satisfy positive qualification.
Named preflight, capture, read-only recovery and source-only rendering form one safe unit.
Every normative obligation below implements the frozen completion criteria, approved exact
interfaces/budgets, or the B0/B1/N1/delete-boundary clarification adopted in charter ambiguities.
No acceptance criterion promises that HMC will accept the candidate or preserve ETags/uptime.

## Purpose and exact finite candidate

Qualify one candidate on the observed release and one fresh inactive owned VIOS. The grant
replaces a complete published opaque-grammar prerequisite with exact source qualification.
It does not establish a native grammar or product domain in advance. No G4 power-control/
controller conn_monitoring work, product API implementation, activation, runtime bootability
or maturity promotion enters this prerequisite. Refusal is useful source evidence, not success.

The eight literal values, in order, are exactly the JSON array below. No extra
case, caller-selected text, reordered case, second encoder or retry is admitted.

```json
[
  "c09-plain",
  "c09,comma",
  "c09=equals",
  "c09'single",
  "c09\"double",
  "c09\\slash",
  "c09 space",
  "c09,='\"\\ combined"
]
```

Encoding:
create io.StringIO(newline=""); csv.writer with delimiter=",", quotechar='"',
quoting=csv.QUOTE_MINIMAL, doublequote=True, escapechar=None, lineterminator="\n";
writerow([f"lpar_id={verified_positive_id}", f"boot_string={literal}"]). Remove exactly
its final generated LF (not strip/rstrip), then shlex.quote the WHOLE resulting record.
The record has exactly two CSV cells. A positive ID is an exact int, not bool, from a fresh
contained VIOS identity; it is never a packet field or a name used as an ID.
This is a candidate encoder: case_index is a strict int in0..7. Serialization is not native proof.

Fixed write: chsyscfg -r lpar -m <shell-quoted verified system name> -i <whole quoted record>.
No --force, -f, alternate quote style, attribute append, generic command field or shared
build_attribute_record relaxation. Nonempty stdout/stderr on a purported successful write
holds conservatively; retain its bytes privately rather than interpreting an unknown warning.
Fixed native read: lssyscfg -r lpar -m <quoted system> --filter <quoted lpar_ids=verifiedID>
-F --header. This exact option shape is retained in the earlier header receipt; -F has no
invented field argument. Use existing build_filter for the numeric filter, then shlex.quote.

## Closed interfaces

New module scripts/live_test/vios_boot_source.py exports:
- validate_packet(packet: dict) -> None
- encode_boot_record(partition_id: int, case_index: int) -> str
- parse_native_header(raw: str, identity: dict) -> dict[str, str]
- async preflight(hmc: BootSourceHMC, packet: dict, directory: Path) -> dict
- async capture(hmc: BootSourceHMC, packet: dict, directory: Path, *,
  grant_packet_sha256: str, exclusive_writer_window: bool,
  fresh_vios_delete_approved: bool) -> dict
- async recover(hmc: BootSourceHMC, packet: dict, document: dict, directory: Path) -> dict
- main(argv: list[str] | None = None) -> int
These are proposed diagnostic-only interfaces, not new product operations or public facade.
These signatures are within the approved design surface; this private candidate is not adopted code.

Designed named commands (not installed at this baseline):
1. uv run --no-sync python scripts/live_test_preflight.py --group vios-boot-source --packet "$INITIAL" --packet-sha256 "$INITIAL_SHA" --output "$WINDOW/preflight-initial"
2. Root binds the observed baseline, creates a final packet/hash, then repeats that named
   preflight with --packet "$PACKET" --packet-sha256 "$SHA" --output "$WINDOW/preflight-final".
3. uv run --no-sync python scripts/live_vios_boot_source.py --packet "$PACKET" --packet-sha256 "$SHA" --output "$WINDOW/capture" --grant-packet-sha256 "$SHA" --exclusive-writer-window --fresh-vios-delete-approved
4. uv run --no-sync python scripts/live_test_recovery.py --vios-boot-source-packet "$PACKET" --packet-sha256 "$SHA" --results "$WINDOW/capture/results.json" --output "$WINDOW/recover"
Registration is the two explicit shared source routes and named wrapper, matching AME.
No numbered subtask, ordinary/default/all runner dispatch or capability operation is added.
Initial/final preflight, capture and recovery each claim their private slot once. Help has
no connection side effect. Exit 0 means all eight exact observations, qualified history,
confirmed owned deletion and closed session for capture; otherwise 2. Recovery exit 0
requires complete historical proof AND present baseline/absence AND closed old/new sessions.
A later current absence cannot rewrite an interrupted source run as historically complete.

Packet schema 1 has EXACT keys: schema_version, configured_profile, hmc_identity, system,
tested_commit, tested_tree, imported_source_sha256, fixture, resources, encoding_id, values,
baseline_sha256, window, budgets, grant_binding. Unknown/duplicate keys, nonfinite JSON,
wrong strict types and missing keys refuse before transport. encoding_id is exactly
"csv-minimal-two-attribute-v1"; values must exactly equal the fixed ordered array above, with literal text unmodified.
hmc_identity={host,port,user,verify_ssl}; system={uuid,name}; fixture={name,caller_nonce,
description}; grant_binding={agent_id,created_date,grant_id}; window={directory,not_before,
expires_at}. Other bounds/hash/date/profile checks retain AME's existing closed validators.
fixture.name="c09v-" + first 24 hex digits of a new 128-bit lowercase nonce. Description is
exactly [hmcpctl owner:<agent> created:<ISO-date>] [caller <nonce>] in the create record.
No credentials, destination URL, native ID, resource override, command or repair option exists.
Root owns the fresh current grant; packet/flags/journal/recovered marker are not authority.
The packet digest must match actual raw bytes. Baseline=null is allowed only on initial
preflight; root binds its stable digest before final preflight/capture. No automatic refresh.
Window is bounded to3200–7200 seconds, absolute UTC bounds; expiry stops resource effects.
A new window does not reset consumed effects. Root must separately disposition any retry.

Selected boundary-owner source hashes are a closed exact path set: the existing AME
SOURCE_PATHS minus scripts/live_vios_ame_source.py, plus scripts/live_test/vios_boot_source.py
and scripts/live_vios_boot_source.py. This expands to20 paths at this baseline and is
enumerated explicitly below; no caller-selected extra or missing path. It is a
selected execution-boundary attestation, not a claim to hash all transitive Python imports.
Budgets is exactly {stages:<the four [REST,SSH,bytes,seconds] table tuples>, rows:{regular:128,
vios:8,profiles:32,pools:4,with_own_vios:9,with_own_profiles:33}}. Integers are strict, not bool.
parse_native_header's identity argument is the closed REST-derived record {system_uuid,
vios_uuid,name,partition_id,description}; type/inactive state and selected-system containment
are independently proven immediately before its use, not supplied by packet identities.

Private journal schema1 has exact top-level keys schema_version, source_only, run,
packet_sha256, binding_sha256, baseline_sha256, observed_baseline_sha256, encoding_id, fixture,
phase, create, cases, deletion, snapshots, failure, transport, recovery. Unobserved values
are explicit null/not-attempted, not fabricated defaults. run={group:"vios-boot-source",
subtasks:[],tested_commit,tested_tree,tree_clean,imported_source_sha256,started,finished,partial}.
fixture carries the packet fixture plus observed UUID/ID when positively confirmed.
create/deletion={execution,record_sha256,identity,observation}; execution is one of
not-attempted,intent,acknowledged,rejected,unknown. Deletion adds no native create record;
record_sha256 is null for its fixed UUID route. cases contains at most8 ordered entries
{index,literal_sha256,record,record_sha256,execution,native_readback,readback_sha256,
snapshot_index,observation}; observation is unobserved,exact,rejected,mismatch,unavailable,unknown.
indices are0..7 internally; renderer prints1..8. snapshots retain the existing ame.snapshot
shape and digest; native_readback retains complete bounded private raw text and parsed cells.
A reported exact match is re-derived from record/literal/header/identity/snapshot data.
recovery={history_complete,baseline_agrees_now,fixture_absent_now,capture_session_closed,clean}
is null until recovery; empty/missing/invalid history remains incomplete. failure holds a
bounded fixed category and last phase, never provider text in publishable output. transport
holds physical used counters and finite call labels. session.json separately binds packet,
stage, logon/logoff/http states, own-child residue, counters and closed boolean. A failed
journal checkpoint cannot replace an older durable intent with a new successful phase.
Phase is one of baseline-observed,create-intent,created-observed,boot-intent,boot-observed,
delete-intent,deleted-observed,held,recovery-observed; current case index lives in cases.
This is the approved diagnostic record design, not an implemented persistence API.
Nested witness representation makes the approved history contract implementable without new
packet options: native witness={raw,cells}; parse raw again and require stored cells equality.
create.observation={outcome,baseline_snapshot_index,owned_snapshot_index,native_baseline};
create.identity is the closed REST-derived identity above. deletion.observation={outcome,
predelete_detail,predelete_native,restored_snapshot_index}; predelete_detail preserves the
bounded fresh REST detail, not only a claimed marker. cases.native_readback={detail,before,
after}, with full bounded REST detail and native witnesses; unobserved entries remain null.
Native readback hash is ame.digest of that complete object; literal/record hashes are SHA256
of exact UTF-8 bytes. Snapshot entries are unchanged ame.snapshot records; digest stable
with ame.digest when comparing B0/B1. B0 and B1 are located by create.observation indices;
N1 is native_baseline. Refusal/unknown cases keep available raw witnesses and null missing ones.
The finite outcome vocabulary for create/deletion observation is the same unobserved/exact/
rejected/mismatch/unavailable/unknown set; confirmed deletion means exact plus B0 and absence.
fixture={name,caller_nonce,description,vios_uuid,partition_id}, latter two initially null.
failure is null or {category,phase}, category in {input,authority,source,identity,refusal,
transport,observation,budget,checkpoint,session}; provider exception text remains private raw
witness only. transport={used,calls}, inherited fixed counters and physical call labels.
session={schema_version,source_only,packet_sha256,stage,logon,logoff,http,own_process_pid,
transport,closed}; logon/logoff in {not-entered,unknown,acknowledged,no-known-token}, http in
{open,closed,unknown}; own_process_pid null unless an owned child remains. Closed requires
acknowledged logon/logoff, closed HTTP, no owned child residue. A later read cannot prove a
missing closeout. The schema records observations; it does not authenticate a hostile author.

## Resource allocation and target admission

Use AME's existing tuple unchanged: memory min/desired/max 1024/2048/4096 MiB;
shared CPU 0.1/0.5/1.0 units; vCPU 1/1/2; uncapped. Memory mode ded, mem_expansion=0,
lpar_env=vioserver and profile_name=default_profile; all resources explicit. One initial
ownership description is atomic in that same mksyscfg record. No post-create ownership stamp,
all_resources, activation, devices, profile apply, paging, pool, service or AME variation.
Build creation with existing ame._record_pairs and build_attribute_record, with the one
fixed case supplying mem_expansion="0". Never reuse the experimental boot encoder for create.

Admission retains the AME snapshot's classic management and VIOS/AME capability checks,
region alignment and resource capacity. This deliberately bounds this source experiment to
that proven creation envelope, not C09's eventual applicability or controller-mode support.
At most 128 ordinary partitions, 8 pre-existing VIOSes, 32 saved profiles and 4 pools;
the one fixture permits VIOS9/profiles33. Incomplete collections are unavailable, not empty.
Require no c09v- stranded name, no identity/name/ID collision, fresh nonce/name absence,
matching connection/agent/source revision and current coordinated writer interval.

Retained AME create evidence acknowledged precisely this AME=0 resource record. Its original
source run was held/partial; a later separately authorized cleanup confirmed deletion and
baseline restoration. This is resource/create grounding, NOT completed AME qualification or
proof that this boot-source procedure ran. The earlier header came from a Running VIOS:
it establishes header shape/empty pend_boot_string only, never this inactive fixture's state.

## State, evidence and once-only effects

A. Initial/final preflight each take one full protected snapshot and close their session.
B. Capture independently rechecks B0, source/current grant and full remaining reserves.
Persist create-intent, consume the one create token before physical SSH, then submit once.
C. Require acknowledged native create plus full post-create snapshot: one fresh contained
VirtualIOServer UUID/name/positive ID, REST state exactly "not activated", initial marker,
fixed resources and no foreign drift. Read its full native header and bind that baseline.
Bind this snapshot as B1 and first valid full native row as N1.
No unknown create may be adopted or cleaned. Require a unique default profile with dedicated
memory and AME disabled; unavailable resource evidence holds before any boot write.
D. For each exact case: fresh REST detail and full native read bind UUID/name/ID/type/marker
and unchanged baseline/nonboot fields; check current inactivity/grant/source/reserves; persist
case-index/record hash/write-intent; consume that case token before SSH; submit once. Follow
with a fresh full native read and full protected snapshot. Equality is never inferred from
the exit status or submitted record. Do not proceed until the complete observation is durable.
E. Exactly two native CSV rows: one unique nonempty header row and one data row of equal width;
require name,lpar_id,lpar_env,uuid,description,pend_boot_string; no duplicate/extra data rows,
truncation, decoder replacement, [stderr] suffix or malformed CSV. Header order/set must match
the fixture baseline. Parse cells with csv.reader(strict=True), never split on commas or '='.
Native name/ID/UUID/type/marker must match contained REST identity. Do not translate native
state text into invented semantics: REST independently proves inactive; native state is
retained and must remain unchanged. All native cells EXCEPT pend_boot_string compare exactly
to the first owned baseline, including current/shadow/last-boot fields and uptime. Benign drift
holds; no telemetry exemption is added to obtain a pass. Empty pending baseline is recorded
as empty, never a clear operation. Each post-write pending read must equal its literal exactly.
A pre-write pending read must equal the last proven value, or N1's original value before case0.
F. B0 is the original pre-create stable snapshot, including available CPU/memory. Final
preflight and capture pre-create must equal B0. B0→B1 uses existing
_foreign(B1, own_uuid)==_baseline_foreign(B0): remove exactly the owned UUID and the two
available-capacity fields CurrentAvailableSystemMemory/CurrentAvailableSystemProcessorUnits;
separately prove the fixed own resource tuple. Creation is the reason for those removals.
Every subsequent boot snapshot must equal full B1, including those capacity fields, all
selected regular/VIOS/grouped/pool fields and saved-profile ETags/semantic XML. Only direct
outer Atom publication text has the existing normalization; no new normalization is added.
N1 header order/set and each nonpending cell remain exact. No rolling baseline, ETag waiver,
unknown-field pruning or invented pending XML leaf. Text-match with other drift is held.
After delete and during recovery, full stable equality is against original B0, including
capacity and absence. Current equality never repairs unknown historical execution.
G. A definite native refusal stops further cases; it can permit the sole cleanup only after
fresh identity/marker/inactivity, unchanged protected state and current cleanup authority.
Malformed output, identity mismatch, unknown execution, nonboot drift, journal failure or
budget exhaustion closes the resource-write path, INCLUDING delete. No more cases, clear,
restore, alternate algorithm or guessed cleanup; use the single observational uncertainty
reserve if available, close sessions, retain exact residue and request root disposition.
A pending-text mismatch also holds all subsequent resource writes conservatively.
H. After eight exact matches, persist delete-intent only with freshly checked own UUID,
REST/native marker, inactivity, current grant, full foreign baseline and remaining reserve.
Delete once through existing scoped delete_vios; its own containment and state checks remain.
Independent absence and restored original protected baseline prove cleanup, never delete exit.
Unknown delete is never replayed. No source-case can create another fixture.
I. Mandatory read-only recovery validates the exact packet/capture/companion session history
and takes its own complete snapshot. It has no resource mutation arming path. It reports
present residue separately from historical completion and cannot clear held status by labels.

## Physical budget design, not an execution grant

| Once-only phase | Full snapshots | REST | SSH | Accepted response payload | Absolute runtime |
|---|---:|---:|---:|---:|---:|
| preflight-initial | 1 | 70 | 1 | 4 MiB | 200 s |
| preflight-final | 1 | 70 | 1 | 4 MiB | 200 s |
| capture incl. one observational uncertainty reserve | 12 | 830 | 40 | 42 MiB | 2600 s |
| independent recovery | 1 | 70 | 1 | 4 MiB | 200 s |
| aggregate four phases | 15 | 1040 | 43 | 54 MiB | 3200 s |

Derivation: snapshot B=8+3V+P<=68 REST plus one fixed lscomgmt SSH (AME source function).
Capture normal snapshots: pre-create, post-create, eight post-write, post-delete =11; one
uncertainty snapshot gives12. REST=12*68+8 pre-write detail+4 final detail/deleter+2 session=830.
SSH=12 mode+1 create+1 native owned baseline+8*(pre-read,write,post-read)+1 final marker+
1 uncertainty native read=40. No additional diagnostic probe or pagination fetch is budgeted.
Effect ceilings: one create, eight boot attempts (one each in order), one own delete; zero
activation/power/profile/device/pool/foreign/restore/clear/stamp writes. Session logon/logoff
are separately budgeted administrative session effects. Refused attempts still consume slots.

Each snapshot<=120s/2MiB serialized; REST response<=1MiB/20s, SSH<=64KiB/20s (create60s).
SSH cap includes combined stdout/stderr, reserving the existing ten-byte stderr label.
Count and charge at physical boundaries, including nested deleter/session/error requests.
Precharge per-call maximum; refund only a complete observed smaller response. Counters bound
application payload/retention, not wire bytes or implementation-owned transport buffers.
Capture's 42MiB covers 24MiB snapshots +14MiB other REST +2.5MiB SSH with1.5MiB margin.
Before create/each boot/delete, reserve the remaining finite schedule by cursor, including
reads not yet consumed, one uncertainty snapshot/native read, logout20s and local close10s.
The cursor distinguishes before/after pre-write observations; case count alone is insufficient. Reserve expiry refuses dispatch; it does
not authorize unsafe cleanup. Each deadline is min(phase,block,window), never extended.
At most four REST sessions total and43 bounded SSH command connections; one logon/logout per
stage, each20s, local close/reap10s. Unknown logon/logout remains session residue. Only own
children/connections close; no foreign-session kill. Closing a client does not prove remote
cancellation. Root's outer deadline watchdog is required; its kill is also unknown execution.

Source attestation at stage start and immediately before each of10 resource effects uses
existing bounded read-only _git_read: three Git calls per check (HEAD/tree/status), max42
across the four phases, max33 capture. Use one stage-start attestation, not both the
AME _main and _run_stage checks. This diagnostic owns its entrypoint sequencing. Each5s plus1s own-child reap, <=64KiB output per call;
time consumes phase budgets. Hash/import checks consume the same deadline. At deletion the sole effect attestation runs at the physical DELETE boundary after
the scoped deleter's intervening identity/state reads; no extra attestation is added. Effective port is explicit to disable the existing
legacy endpoint retry; inherited once-only logon rejects implicit reauthentication. No
redirect/fallback/new credential source is admitted. Bound deployed runtime Python3.11.15 (existing
source deployment), exact clean Git/import closure, not a new project floor or host inference.
Capture worst block time2330s +198s Git =2528s <2600s; read-only170+18=188s <200s.

Packet<=64KiB; exact private results journal<=32MiB, separate session<=1MiB. Each checkpoint
uses ame._save's durable0600 temp/fsync/replace/directory-fsync; never overwrite last intent
with a fabricated result after checkpoint failure. Four final result/session pairs plus one
max32MiB in-flight temp and packets/receipts fit a proposed168MiB window disk allowance.
Reject larger serialized artifacts; no append-only payload copies. Window/output directories
0700, files0600, same owner, canonical absolute nonsymlink paths outside source checkouts.
No raw command/native/header data prints to console. SOURCE ONLY renderer allows fixed case
indices/outcome/cleanup labels and source revision only; no product PASS or maturity entries.


## Reuse and finite boundary ownership

At this base the AME module and reused client owners are byte-identical to6b85ac16.
Preflight/recovery gained additive accelerator-QoS routes; retain them unchanged.
Reuse ame.encode/digest/_json/_keys/_text/_uuid/_private/_git_read/_attest/_save/_receipt,
snapshot, _admit_capacity, _record_pairs, _candidate, _observed, _foreign/_baseline_foreign
only within their established signatures and semantics. _attest iterates packet hash keys;
new validate_packet first proves the exact20-path set. _candidate tolerates absent REST
Description, so new native marker confirmation and exact observed identity remain required.
New admission enforces c09v- absence; do not call AME's c07v- admission unchanged.
Do not reuse AME packet/cases/grant/history/lifecycle/entrypoint policy or mutate AME globals.
Use existing build_attribute_record for creation, build_filter for the numeric native query,
run_hmc_command for bounded SSH, and delete_vios for scoped inactive deletion.

BootSourceBudget(ame.Budget) owns __init__(stage: str, packet: dict) and
reserve(cursor: str, case_index: int = 0) -> None. Initialize stage,maximum,used,started,
deadline,window,block,calls from this diagnostic's table; bind initial entrypoint monotonic
start before setup. Inherit section/remaining/timeout/dispatch/charge/refund unchanged.
Snapshot opens its own section; never wrap it in a second section. Other actions each have
one bounded section. reserve overrides AME's two-case formula with the schedule below.
BootSourceHMC(ame.CountedHMC) retains constructor(config,budget) and overrides
async _request(method: str, path: str, **kwargs). Before parent resource DELETE it requires
capture stage, delete-intent, non-held state, one remaining delete slot, armed exact route,
current grant/source/reserve. Parent alone increments delete_count and consumes delete_sent.
Never reset these fields or rearm unknown. Clear only delete_uuid in finally. Session DELETE
remains separately once-only and permitted for own-session closeout after a resource hold.
Recovery/preflight never arm resource DELETE; non-GET resource REST is otherwise refused.
The finite native dispatcher has create/header/next-case commands only; mode is existing
snapshot's fixed read. Consume create/case token before transport; no arbitrary command input.

The capture schedule is: session-open; B0 snapshot; create; B1 snapshot; N1 read;
for i=0..7 {detail; native pre-read; case write; native post-read; B1 comparison snapshot};
final marker + detail + scoped deleter (four REST total including DELETE); B0 snapshot;
optional single uncertainty snapshot/native read on hold; own session-close/local-close.
Reserve uses the not-yet-consumed suffix, not a new probe allocation. Per-block maxima are:
snapshot(68REST,1SSH,2MiB,120s), detail(1,0,1MiB,20s), native read/write(0,1,64KiB,20s),
create(0,1,64KiB,60s), final cleanup(4,1,4MiB+64KiB,100s),
logon/logoff(each1,0,1MiB,20s), local-close(0,0,0,10s).
Conservative payload accounting is24MiB snapshots +14MiB other REST +2.5MiB
for40 SSH responses =40.5MiB, below42MiB; snapshot-mode SSH is included twice conservatively.
Source attestation costs up to18s/check and is reserved for the unconsumed effect suffix.
At a cursor inside final cleanup reserve subtracts consumed reads, preserving DELETE and
post-delete/uncertainty/closeout. The action list is fixed data, not a generic workflow engine. Closed reserve cursors are
before-create, before-boot-observations, before-boot-write, before-cleanup-observations and
before-delete; case_index selects0..7 only for boot cursors. Other cursor/index pairs refuse.
The optional uncertainty native read is skipped when no safely verified positive ID remains;
it cannot discover/adopt a fixture after unknown creation or an identity mismatch.

The table contains conservative ceilings, not achievable maximum run traces. Snapshot
8+3V+P reaches68 at9/33; admitted preflight V8/P32 reaches64, plus logon/logout=66, below70.
Capture830 sums12 maximum snapshots,8 details,4 cleanup REST and2 session calls; actual
pre-create/post-delete snapshots have no owned fixture. Slack buys no additional calls.
Unknown/refused attempts retain consumed slots and precharged payload; early stop reduces
available effects and never transfers them to another candidate or window.

Journal checkpoint bounds are checked before ame._save:32MiB results/1MiB session.
Preflight uses phase baseline-observed and partial=false; its own predecessor validation
checks that exact phase, matching binding digest (packet without baseline_sha256), observed
baseline, completed companion session, and final packet digest before capture.
The journal's phase enum therefore needs no AME preflight-complete value. Initial preflight
may bind a null baseline; only root creates the final packet. No automatic mutation of packets.
Recovery re-derives record hashes, fixed case sequence, native and snapshot comparisons,
acknowledged create/delete, clean source identity and session history. It does not trust
partial=false, recovery.clean, case observation labels or present absence alone.
A coded refusal is recognized only through existing HMCCLIError.remote_refusal_code, never
substring matching arbitrary errors. Create refusal needs unchanged B0 and creates no cleanup
right. Boot refusal permits sole cleanup only if fresh full observations prove last pending,
B1/N1 preservation and current exact owned inactive identity; use existing observation slots.
A partial journal, unknown token or failed fsync remains held; do not fabricate negative proof.

## Failure model

1. Actors and deployments
- Root-coordinated operator, private bound Python3.11.15 source deployment, classic HMC,
  one fresh inactive owned VIOS within the fixed resource envelope and exclusive interval.
- Offline CI/unit tests mock only external boundaries; no HMC is reachable from guardrails.
2. Invariants and assets at stake
- Foreign partition/profile/pool state; fixture ownership/identity/inactivity; no activation.
- Once-only effects, truthful unknown/history/session residue, exact pending text and privacy.
- Full fixed B0/B1/N1 witnesses and finite physical/payload/time/filesystem budgets.
3. Accepted failure classes
- Candidate rejection or preservation drift yields unqualified/held; positive storage is not promised.
- Unknown remote completion can leave one owned fixture; retries and speculative cleanup would
  violate once-only authority. Root separately dispositions residue; no clean claim is made.
- Source changes/timeouts/incomplete collections fail closed; bounded negative evidence is valid.
4. Covered elsewhere
- Product six fields/twelve bindings and G4: parent #1770 C09; no parent criterion is removed.
- AME variations: #1768; hardware allocation/current operational grants: root campaign allocator.
- Project Python3.11–3.14 CI/build guardrails: repository verification; live3.11.15 is a deployment gate.

## Threat model

1. Boundary inventory
- Added: operator packet/grant into native effect selection; HMC full native CSV into proof;
  private journal/session history into recovery; source document into publishable rendering.
- Extended: named preflight/recovery dispatch and existing counted HTTP/SSH/session machinery.
  No product permission/default-runner boundary is widened.
2. Actor model
- Trust root's current bounded grant, exclusive writer coordination and selected deployment.
- Treat packet/history/native/HTTP bytes as untrusted even from that operator or selected HMC.
- Another concurrent operator can change observed state; preserved-field/identity drift holds.
  A hash identifies selected source bytes; it does not authenticate remote operational truth.
3. Control per boundary
- Packet→effect: closed strict schema/raw hash/fixed values/fresh contained ID; whole-record
  shell quoting; once-only intent and physical tokens/current grant/source/deadline checks.
- Native/HTTP→proof: bounded output, strict CSV/complete collections, exact identity/ownership,
  protected B0/B1/N1 witnesses and unknown latching; use existing client collection guards.
- Files→recovery: private canonical once-only paths, bounded reads, durable checkpoints,
  re-derived history and separate present-state/session conclusions; never arm DELETE.
- Evidence→public renderer: strict SOURCE ONLY dispatch and fixed allowlist; raw provider
  output stays private. Malformed source evidence cannot fall through into product PASS.
- Transport→resources: inherited containment/inactivity/response counting plus one-delete
  subclass check, finite native command dispatcher and own-session-only closeout.
4. Explicitly out of scope
- No additional diagnostic exclusion or weakened threat control is introduced. Product APIs,
  bootability/activation and AME variations remain with the charter's named external owners.
  No broader grammar, hostile-administrator attestation or distributed-lock guarantee is
  asserted by eight observations under root's selected-source/exclusive-window trust model.

## Exact selected source closure

This is the20-path execution-boundary witness, not transitive-import closure. Packet hashes
must cover these exact keys; test/document paths are not executable runtime boundaries.

- scripts/live_test/vios_ame_source.py
- scripts/live_test_preflight.py
- scripts/live_test_recovery.py
- scripts/live_test_evidence.py
- src/hmcpctl/config.py
- src/hmcpctl/client/core.py
- src/hmcpctl/client/client_systems.py
- src/hmcpctl/client/client_profiles.py
- src/hmcpctl/client/client_resolution.py
- src/hmcpctl/documents/partition_profile.py
- src/hmcpctl/documents/lpar.py
- src/hmcpctl/operations/vios/core.py
- src/hmcpctl/resource_identity.py
- src/hmcpctl/ssh/lpar.py
- src/hmcpctl/ssh/commands.py
- src/hmcpctl/ssh/description_validation.py
- src/hmcpctl/ssh/transport.py
- src/hmcpctl/xmlutil.py
- scripts/live_test/vios_boot_source.py
- scripts/live_vios_boot_source.py
