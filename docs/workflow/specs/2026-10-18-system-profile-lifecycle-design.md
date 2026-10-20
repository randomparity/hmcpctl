# System-profile lifecycle — issue #1725

Proposed revised full design; product build waits for root-owned scope-audit disposition. Current charter: WORK:SCOPE6273154252, claim q1725-d647765d/attempt1, campaign
822f9065dedb-622a646c-5bde-4f6a-b9e1-d78c64703a58. That complete charter, not this target,
authorizes the five-row lifecycle. Direct user “approve” also approves the exact157-line
three-contract proposal SHA f3fe736017cea9742f5299c76f8d178020f3d012b0628ac38c81419da8e55b98;
that decision replaces the superseded backend/classifier/response contracts below, not the scope. “Approve both revised contracts”
(call_c202111f34f34b6bb0db4158db909334 item0) approves fixed CLI deletion and bounded association
admission; the earlier child-allocation and writer-window approvals remain recorded there.

## Scope and Global Constraints

Python>=3.11; existing HMC REST/SSH clients, Typer/FastMCP and pytest; no new dependencies.
REST job floor HMC10.3.1060.0; version alone does not prove operation capability.
Host Darwin arm64/Python3.11.15 and CI amd64/Python3.11–3.14 are separate from native proof.
Preserve ADR0118's six-name facade and #638/#698 ownership; no generic proxy or backend selector.
No automatic replay, rollback, maturity promotion or current native effects. Fixed M denominator250.

All modes/parameters/source units of these exact `rest:jobs/managedsystem-jobs/` rows remain #1725:

| Row suffix | Modes/parameters/units | Contract |
| --- | --- | --- |
| activatesystemprofile_managedsystem-job | 10/24/34 | systemProfile, operation, test, continue; action table below |
| createsystemprofile_managedsystem-job | 10/22/32 | systemProfileName, logicalPartitionNames, logicalPartitionProfileNames |
| deletesystemprofile_managedsystem-job | 10/18/28 | systemProfileName via fixed documented CLI equivalent, not a REST pass |
| getsystemprofiles_managedsystem-job | 10/14/24 | no request parameters; complete result/code/diagnostics, empty and name lookup |
| updatesystemprofile_managedsystem-job | 10/24/34 | systemProfileName, systemProfileNewName, paired replacement arrays |

Total152 is allocation inventory, not design denominator. JSON/XML reference examples normalize
into existing XML job builders; no public raw encoding/request option. The three approved pairs:
independent-writer fencing — operator coordination; automatic replay/rollback after uncertain
effects — caller reconciliation; structurally unencodable or undocumented association encodings
— operator-managed pending authoritative encoding support. Unsupported encoding invalidates the
WHOLE inventory; operator-managed does not mean skipped members or permission bypass.

## Evidence and backend decision

Independent REST sources: POWER10 jobs topics042/047/050/053/074; POWER11 topics042/047/050/054/077,
under docs/refs/hmc-rest-api-p10/jobs/managedsystem-jobs/ and its POWER11 equivalent. Both Delete
pages prescribe systemProfileName. CLI sysprof deletion is independently documented at
 docs/refs/hmc-commands-p10/commands/rmsyscfg.md:17–31,57–59 and the POWER11 equivalent.
The Get pages supply no nonempty output example. Do not copy untracked reference prose.

Historical REST Delete rejected its documented parameter; Get failed despite code0 or returned
nonempty ErrorData; validation returned COMPLETED_WITH_WARNINGS. None is silently called success.
Approved product routes: REST Create/Update/activation-only; ALWAYS fixed documented SSH whole
inventory, delete, validation and combined validation/activation. Choose BEFORE effects; no fallback,
REST Get/test=true attempt, alternate parameter, backend flag or row transfer. Prove unique current
CLI MTMS for the authorized UUID and quote operands. Command outcomes never fabricate job facts.
POWER10/11 lssyscfg.md:17–35,110–117 grounds sysprof listing/delimiters; chsysstate.md:82–84,135–136,
229–240 grounds --test, combined -o on --test and --continue; its92–98 mode restriction remains.

Source calibration at a33e2626 observed strict complete activation and both negative variants:
N1 failed with unchanged mixed state; N2 failed while A changed witness1024->requested base512,
B retained witness1024. Profile names/contained UUIDs and actual memory corroborated transitions.
Private raw journal hash637da6cf731292afb54a473dbf85103cca57bb65facfa4c4cfcb7a25228a08a1;
manifest hashdb7a0b41532cbc873fc191df16d9e78b162faf6c03016bb008f0d1d04864c39e.
Overall source capture remains FAIL2 (two negative FAIL rows), independent recovery0 restored the
complete original baseline; root released coordination. Policy stays deliberate; failed jobs retained.
One regular/sharedCPU/dedicated-memory context does not establish universal application semantics,
other shapes/releases, product paths or combined CLI test+continue success. No automatic maturity.

## Interfaces and consumer contract

New owner src/hmcpctl/operations/systems/profiles.py defines these pre-release types; no facade exports.
SystemProfilePair is a pydantic BaseModel with extra='forbid' and exactly nonblank string fields
partition and profile (partition selector and its contained-profile selector, not HMC connection).
The following frozen dataclasses serialize with dataclasses.asdict; tuple fields become JSON arrays:

```python
SystemProfileMember(resource_type: str, partition_uuid: str, partition_id: int,
                    partition_name: str, profile_uuid: str, profile_name: str)
SystemProfileRecord(name: str, members: tuple[SystemProfileMember, ...])
SystemProfileInventory(system_uuid: str, records: tuple[SystemProfileRecord, ...] | None,
                       source: Literal['ssh-command'], job: JobOutcome | None,
                       command_status: Literal['completed', 'failed', 'uncertain'],
                       error: str | None)
MemberObservation(member: SystemProfileMember, before_state: str | None,
                  after_state: str | None, observed_profile_name: str | None,
                  observed_profile_uuid: str | None,
                  classification: Literal['changed', 'unchanged', 'unavailable'],
                  error: str | None)
SystemProfileOutcome(system_uuid: str, name: str,
                     action: Literal['create', 'update', 'delete', 'validate',
                                     'activate', 'validate-and-activate'],
                     backend: Literal['rest-job', 'ssh-command'],
                     state: Literal['completed', 'failed', 'uncertain'],
                     job: JobOutcome | None, return_code: str | None,
                     command_status: Literal['completed', 'failed', 'uncertain'] | None,
                     activation_execution: Literal['complete', 'partial', 'failed', 'unknown'] | None,
                     postcondition: Literal['verified', 'drifted', 'unverified', 'not-applicable'],
                     members: tuple[MemberObservation, ...], warnings: tuple[str, ...],
                     error: str | None)
```

records=None means WHOLE inventory unavailable; () means independently proven empty. A name
filter applies only after whole-inventory validation; a missing requested name raises ValueError.
Pre-effect invalid input/containment raises ValueError; policy denial PermissionError; unavailable
prechecks HMCError. No mutating request is sent. After submission/effect attempt, return an outcome
with known job/command facts and bounded error/warnings rather than discard uncertainty/handles.
All strings/results are bounded5MiB overall, diagnostics2048characters; do not truncate an inventory.

Exact async domain signatures (HMCClient, Sequence and JobOutcome are existing/imported types):

```python
list_system_profiles(hmc, system_name_or_uuid: str, *, name: str | None = None,
                     timeout_seconds: int = 300, poll_interval: int = 5) -> SystemProfileInventory
create_system_profile(hmc, system_name_or_uuid: str, name: str,
                      members: Sequence[SystemProfilePair], *, exclusive_writer_window: bool = False,
                      ownership_override: bool = False, timeout_seconds: int = 300,
                      poll_interval: int = 5) -> SystemProfileOutcome
update_system_profile(hmc, system_name_or_uuid: str, name: str, *, new_name: str | None = None,
                      members: Sequence[SystemProfilePair] | None = None,
                      exclusive_writer_window: bool = False, ownership_override: bool = False,
                      timeout_seconds: int = 300, poll_interval: int = 5) -> SystemProfileOutcome
delete_system_profile(hmc, system_name_or_uuid: str, name: str, *,
                      exclusive_writer_window: bool = False,
                      ownership_override: bool = False) -> SystemProfileOutcome
activate_system_profile(hmc, system_name_or_uuid: str, name: str, *,
                        action: Literal['validate', 'activate', 'validate-and-activate'] = 'activate',
                        continue_on_error: bool = False, exclusive_writer_window: bool = False,
                        ownership_override: bool = False, timeout_seconds: int = 300,
                        poll_interval: int = 5) -> SystemProfileOutcome
```

MCP consumers in server_tools/systems/profiles.py expose hmc_create_system_profile,
hmc_list_system_profiles, hmc_update_system_profile, hmc_delete_system_profile,
hmc_activate_system_profile. Drop hmc from domain signatures and append profile: str|None=None
for configured connection; pair inputs are list[SystemProfilePair]. No arbitrary parameter dict.
CLI consumers in cli_commands/systems/profiles.py register systems profile-create/profile-list/
profile-update/profile-delete/profile-activate. Required arguments SYSTEM NAME (list: SYSTEM only),
--name for list lookup; --new-name and repeated --member JSON objects for create/update;
--action/--continue-on-error for activation; approved --exclusive-writer-window and existing
--ownership-override for mutations, --json for structured output. Pair JSON uses exact two keys.
No wait/force/backend/retry flag. timeout_seconds bounds the whole call including CLI; poll_interval affects REST polling only,
never inventing CLI polling. Defaults remain300/5.
Both consumers serialize the SAME dataclass fields. CLI exits0 only completed+verified metadata or
successful validate; failed =>1; uncertain/unverified activation =>2. Native job/command facts and member
observations remain visible even when exit is nonzero. MCP returns the typed outcome, not a string.
Register the new module in server_tools/catalog.py and cli.py; keep existing groups/entries intact.

## Inventory, authorization and freshness

ALWAYS execute `lssyscfg -r sysprof -m <quoted-bound-MTMS> -F name:lpar_ids:profile_names --header`
through run_hmc_command(include_stderr=True), without a name filter. Bound5MiB; reject [stderr]
BEFORE canonical empty sentinel or strict existing delimiter parsing. Reject missing/duplicate/extra
columns, duplicate row names, malformed rows/IDs, empty membership, unequal cardinality, duplicate
partition IDs, ambiguous delimiters or incomplete output; never truncate/filter. Bind ordered
ID/profile positions to current positive unique PartitionID/name/UUID and unique contained profile
UUID in BOTH regular/VIOS feeds. Compare the complete independent name namespace. Fresh separate
before/after observation phases must agree on current identity/profiles; drift invalidates WHOLE
inventory. Name lookup occurs afterward. Existing undocumented-encoding exclusion remains unchanged.
Inventory job=None always. Valid native exit0/no stderr plus complete binding gives records=tuple
(possibly empty), command_status=completed. Known native nonzero gives records=None/failed/bounded
error. Exit0 with diagnostics/malformed/contradictory data, timeout or ambiguous connection/termination
gives records=None/uncertain; never empty. No English diagnostic parser or fabricated REST success.

Read selected-system LogicalPartition and VirtualIOServer feeds via existing list_logical_partitions
and list_vios. Resolve each member's corresponding contained LogicalPartitionProfile child feed
with existing list_child(parent_type, uuid, child_type); missing/unsupported evidence refuses the
whole inventory, never filters a member kind. #698's VIOS operations are not transferred.
Resolve caller pairs within those current feeds; reject duplicate partitions/missing/ambiguous/
cross-system selectors and empty/unencodable names. Preserve source pair association, not an
invented partition-activation order. Update permits rename-only, replacement-only or both; require
at least one change, paired arrays together and collision/missing-name checks on complete inventory.

All mutations require existing MCP all-targets grants (exhaustive_targets=False) plus connection/
system policy and EVERY member's existing ownership check. Create checks new set; update old/new
union; delete/activation old set. Reuse authorize_lpar_ownership_description on freshly read
ownership metadata, including existing audited override only; no source-reference grant or stamp.
A missing authoritative ownership/profile read still refuses; override cannot waive complete data.
Validate-only performs containment/authorization but omits writer attestation.

The approved exclusive_writer_window=False flag gates create/update/delete and any activating
action. Caller coordinates namespace/membership, names, contained profile content/ownership and
power from BEFORE preflight through known terminal reconciliation. It is not fencing/lease/lock.
Observation phases are initial discovery, immediate pre-effect compare, and independent post-read.
Each phase creates its OWN local feed cache; reuse only within that phase. The next phase ALWAYS
re-reads current feeds/ownership/namespace; never share a cache across phases or trust an old ETag.
Compare stable full profile Resources/identity/name/defaults/ownership/power before effects;
Metadata transport wrappers alone are omitted. An A->B->C changing-feed test must observe B/C.

## Dispatch and outcomes

For REST paths, fixed XML requests use jobs/requests.py and existing build_job_request(operation, 'ManagedSystem',
parameters), HMCClient.submit_job and wait_for_submitted_job(..., True, timeout_seconds, poll_interval).
Validate coherent Resource.JobID/SELF before polling and terminal identity before trusting results;
contradiction =>uncertain, no different job or generic-client contract change. Keep known handle.
No automatic job deletion/replay/rollback. Missing/warning/failed status, absent/nonzero returnCode
or ErrorData cannot be success. Preserve result/detailedStatus separately and boundedly.

| action | fixed route/parameters | window |
| --- | --- | --- |
| validate | SSH chsysstate -m MTMS -r sysprof -n NAME --test | omitted |
| activate | REST systemProfile=NAME, operation=on, test=false, continue=false/true | required |
| validate-and-activate | ONE SSH chsysstate -m MTMS -r sysprof -n NAME -o on --test; --continue iff requested | required |

Quote only exact bound MTMS/name. validate+continue=true refuses. Reuse existing SSH transport with
include_stderr=True: normal return proves native exit0; any [stderr] makes outcome uncertain.
HMCCLIError caused by ProcessError with known nonzero exit is failed; timeout/signal/connection
without definite outcome is uncertain. Preserve bounded native detail; never parse error text for
numeric codes. No English stdout success parser, second command, replay or implicit route change.
Known unsupported version/capability/controller mode refuses; native unsupported failure stays failed.

For create/update/delete, state=completed requires backend success AND exact desired namespace/
association plus unchanged member profiles/power/defaults/ownership. Success with unavailable
readback =>state=uncertain/postcondition=unverified; observed mismatch =>uncertain/drifted. Failed
terminal/command =>failed, even code0 or apparently desired state; no old failure is rewritten.
SSH has job=None/return_code=None and actual command_status; REST has command_status=None and
normalized JobOutcome. Timeout/interruption returns uncertain with the known handle/command fact;
it does not cancel work or release coordination. No fire-and-forget path.

For validate, completed requires CLIexit0/no stderr AND independent unchanged member/profile/
namespace/power observations; postcondition=not-applicable, no activation claim. Combined command
stays ONE command, backend=ssh-command/job=None/return_code=None; activation-only uses actual REST
job facts. Any definite failed backend stays state=failed even if effects occurred. Timeout or
ambiguity retains coordination/known facts; no cancellation guarantee or automatic cleanup.

Member classification is raw state equality: changed if readable before!=after, unchanged if equal,
unavailable without a reliable pair. Firmware->running is not a new power-on or guest-OS proof.
Profile/resource witnesses are independent internal observations, not these state labels.
A NEW requested activation witness requires before INACTIVE with absent/DIFFERENT prior activation
identity, after requested CLIcurr_profile AND LastActivatedProfile AND exact contained associated
profile UUID, stable activated state (running/open firmware), and coherent available actual CPU/
memory facts. Distinct configured memory transitions require corroborating actual Current/Runtime
Memory, never saved DesiredMemory alone. Starting/unknown/contradiction refuses a positive witness.
Before-already-matching/stale profiles cannot be newly attributed. Other shapes/releases need
source-backed equivalent witnesses; missing proof stays unknown, no dropped member/new exclusion.
Coherent memory does not prove EVERY saved field or universal REST-field semantics.

| Evidence | activation_execution / state / postcondition |
| --- | --- |
| Strict successful backend AND ALL members new-witnessed | complete / completed / verified bounded requested-profile activation |
| Failed backend, >=1 new-witnessed AND >=1 fully read still DIFFERENT requested identity with coherent contrary resources | partial / failed / drifted |
| Failed backend, zero new witnesses, independently unchanged mixed baseline | failed / failed / unverified |
| Stale before-match, unavailable/contradictory/transition/uncalibrated witness | unknown / uncertain (definite native failure stays failed) / unverified |
| Failed backend but all appear witnessed, or successful backend with incomplete witness | unknown; preserve failed/uncertain / unverified |

Metadata/validate use activation_execution=None. Preserve bounded result/detailedStatus separately;
no invented per-member English stderr parser. Existing job/inventory/member reads support later
caller reconciliation as a NEW observation, never rewritten failure. No persistence/resume API.
Tests must include complete, N1 unchanged mixed, N2 partial, stale-before-match, missing/contradictory
and uncalibrated shapes. Native product-path/equivalent-route evidence remains unmet/partial maturity.

## Failure model

- Actors/deployments: authenticated CLI/MCP operators, selected HMC data and independent admins;
  supported HMC contexts, local/CI hosts above. Operator controls local configuration/credentials.
- Assets/invariants: contained association/ownership, unrelated profiles, member availability,
  complete bounded inventory, private diagnostics and truthful backend/member outcomes.
- Accepted classes: the THREE exact approved owner-managed pairs above; none waives fresh reads,
  authorization or filtering prohibition. No distributed fencing/automatic recovery is promised.
- Covered elsewhere: transport/TLS/auth/audit existing owners; #1724 partition profiles; #638/#698.

## Threat model and MCP evaluation

Added boundaries: pairs/actions->fixed requests (closed values, XML/shell quoting); HMC output->
identity (strict whole-inventory/current-field checks); dynamic members->policy (existing all-targets
and each-member ownership); outcomes->CLI/MCP/evidence (same bounded schema, private export whitelist).
Existing client/jobs/SSH boundaries widen only to these finite actions. Untrusted authenticated
callers and malformed/stale HMC data are admitted only through those checks; independent admins
are trusted only to honor coordination. Existing transport security is unchanged.

AI-SPEC: MCP caller supplies finite selectors/pairs/action, receives bounded inventory or truthful
backend/member facts from selected current HMC observations and approved sources. Disallow inferred
grants, arbitrary commands, filtered members, invented success and automatic replay. Invalid input
refuses before effects; ambiguity afterward reports uncertainty. Bound5MiB/result and existing
request/SSH/discovery/job deadlines; no LLM call/cost. Deterministic tests, not a model judge, gate:
SP-E1 valid fixed action; E2 one unauthorized member; E3 stale/encoded association; E4 code0 failed
job/partial/readback; E5 injection/oversize/deadline; E6 private evidence export. All are blocking,
severity4(E1)/5(E2–E6); the full plan names cases/commands/forbidden effects for each.

## Validation and native prerequisites

The ignored full plan defines exact files, interfaces, task tests/red signals and focused commands;
source unit binding covers all152 through both reference-contract fixtures and operation manifests.
Run controlled guard faults, focused adjoining tests, final just verify+ pinned all-files prek,
root-authorized code review and exact-head CI; no further security reviews (explicit prohibition). No automatic maturity promotion from unit/source proof. ADR0212/#620 scopes live evidence to an
authorized release/ad-hoc window, not a merge gate; operations stay unevidenced until passing proof.
This does not waive AGENTS applicable end-to-end proof or authorize hardware/unrun criteria.
The plan's PRODUCT system-profiles arm is separate from SOURCE-only system-profile-source. It
requires NEW exact native authority, normal ownership/all-targets authorization and any existing
audited override explicitly covered by that future grant; source UNOWNED-reference permission is
not reused. Metadata scenario creates two unique records, lists multiple rows, exercises all update
forms and fixed delete, preserving original complete baselines. Validation/activation/continue and
controlled partial-failure effects need separate power/maintenance scope. Recovery runs every
outcome; partial/unreadable/not-run never passes. Original source failed job remains retained.
Use the repository live-testing procedure for applicable native hardware proofs.
