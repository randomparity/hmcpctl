# System-profile lifecycle — issue #1725

Proposed full design; product build waits for the single scope audit after the bounded design
review. Current charter: WORK:SCOPE6258196210, claim q1725-d647765d/attempt1, campaign
822f9065dedb-622a646c-5bde-4f6a-b9e1-d78c64703a58. That complete charter, not this target,
authorizes the five-row lifecycle. “Approve both revised contracts”
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

A separately approved source capture at5dfcf61f/HMC11.2.SP1120 observed successful Create/Get:
Get result was one JSON-array object with string SystemProfileName, PartitionNames, PartitionIds,
PartitionProfileNames, with two comma-separated member positions matching the distinct request.
Raw result hash 5ae61fd48eef1d05fc4f5bfc806c2562c060d19babafc13e07858981693ca95e; capture hash dda500dd38c84bb8dbea41d7cf4b674634544323efb150082c316f3da381d0d5.
REST Delete FAILED_TO_START: "Parameter 'systemProfileName' is not allowed for this Job."
Its read-only template declared no parameters. No alternate parameter/REST retry was inferred.
Separately approved documented CLI cleanup returned success; runner exit1 retained its after-read
transport failure. Separate independent reconciliation proved original complete namespace/member
baseline restored, scratch absent. Original capture remains FAILED/partial; failed job retained;
root reconciled/released coordination. All effect grants consumed. Source is not product proof.
Earlier empty Get at10905eaaa/HMC10.3.1060 failed despite code0 (terminal hash
0116ea368a7a9ed4747435d246a200b45fae6e4d4860d5e2ba5636328b868147).

Approved route: REST Create/Get/Update/Activate; ALWAYS one fixed documented SSH sysprof deletion
through existing transport, chosen BEFORE effects. No REST-then-SSH fallback, alternate parameter,
backend selector, row transfer or silent release exclusion. Delete requires SSH connectivity/
privileges and a proven unique CLI identity for the already authorized system UUID (documented
MTMS form if needed); ambiguous names refuse. Quote both operands, fix resource=sysprof.
Command outcomes have no fabricated JobID/status. These choices are recorded in Proposed ADR0243.

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
                       source: Literal['ssh-empty', 'rest-job'], job: JobOutcome | None,
                       error: str | None)
MemberObservation(member: SystemProfileMember, before_state: str | None,
                  after_state: str | None, observed_profile_name: str | None,
                  observed_profile_uuid: str | None,
                  classification: Literal['unchanged', 'changed-to-running',
                                          'still-not-running', 'unavailable'],
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
No wait/force/backend/retry flag. REST polling uses300/5defaults unless called programmatically.
Both consumers serialize the SAME dataclass fields. CLI exits0 only completed+verified metadata or
successful validate; failed =>1; uncertain/unverified activation =>2. Job completion and member
observations remain visible even when exit is nonzero. MCP returns the typed outcome, not a string.
Register the new module in server_tools/catalog.py and cli.py; keep existing groups/entries intact.

## Inventory, authorization and freshness

Parse ONLY bounded observed JSON array items with four exact required string fields; reject
missing/duplicate keys, invalid types/JSON, duplicate row names, oversized/deep output. Additional
unexpected fields do not authorize anything. For each row require nonzero equal member-column
cardinality, unique positive decimal PartitionIds, exact current PartitionID/name/UUID binding in
the selected system, and unique contained profile name/UUID. Source IDs are NOT UUIDs. Existing
snapshots/operations.py reads PartitionID. No undocumented CSV quoting/backslash parser: delimiter-
bearing/ambiguous input/output fails the entire inventory under the approved exclusion.

Apply the item validator to bounded multiple rows; this is structural inference, not observed
multirow native proof. Compare complete unique profile-name sets with strict independent name-only
namespace. A canonical empty namespace may return source='ssh-empty', records=(), job=None without
the known-failing empty Get. Failed/warning/nonterminal Get or code0+ErrorData gives records=None;
never treat invalid/truncated/missing rows as empty. Public lookup still validates the whole set.

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

Fixed XML requests use jobs/requests.py and existing build_job_request(operation, 'ManagedSystem',
parameters), HMCClient.submit_job and wait_for_submitted_job(..., True, timeout_seconds, poll_interval).
Validate coherent Resource.JobID/SELF before polling and terminal identity before trusting results;
contradiction =>uncertain, no different job or generic-client contract change. Keep known handle.
No automatic job deletion/replay/rollback. Missing/warning/failed status, absent/nonzero returnCode
or ErrorData cannot be success. Preserve result/detailedStatus separately and boundedly.

| action | operation | test | continue | window |
| --- | --- | --- | --- | --- |
| validate | omitted | true | false | omitted |
| activate | on | false | false or true | required |
| validate-and-activate | on | true | false or true | required |

systemProfile always names the selected record; validate+continue=true is refused. Boolean native
values are lowercase strings. CLI chsysstate source135–136 grounds validate/continue semantics;
its92–98 mode restriction is not bypassed. Known unsupported version/capability refuses; native
unsupported-mode failure remains failed, never silently converted to another route.

For create/update/delete, state=completed requires backend success AND exact desired namespace/
association plus unchanged member profiles/power/defaults/ownership. Success with unavailable
readback =>state=uncertain/postcondition=unverified; observed mismatch =>uncertain/drifted. Failed
terminal/command =>failed, even code0 or apparently desired state; no old failure is rewritten.
SSH has job=None/return_code=None and actual command_status; REST has command_status=None and
normalized JobOutcome. Timeout/interruption returns uncertain with the known handle/command fact;
it does not cancel work or release coordination. No fire-and-forget path.

For validate, completed means successful validation JOB, postcondition=not-applicable, no activated
claim. Activation returns each member's before/after state, available LastActivatedProfile or
associated-profile observation and read error. Classify unchanged/changed-to-running/still-not-
running/unavailable; already-running is not newly activated. These observations do not alone prove
requested-profile application. activation_execution separates the published complete/partial
criterion from application proof: complete requires successful terminal plus ALL members freshly
running; partial requires a fully readable mix of running/not-running members; failed requires a
failed terminal without that mixed effect; otherwise unknown (including any unavailable member).
Metadata/validate use None. Already-running is reported unchanged, never newly activated. Preserve
terminal/result/continued-failure diagnostics separately; no unsupported per-member stdout parser.
Requested-profile application stays postcondition=unverified and successful activating jobs return
state=uncertain; failed terminal stays state=failed. Complete execution is not proven application.
Tests must assert both classifications; native application proof remains unmet/partial maturity.
Caller retains returned outcome/known handle and coordination while reconciling with existing
hmc_get_job/hmc_wait_for_job, fresh hmc_list_system_profiles and member/profile reads. No persistence
service or new resume API. Later reconciliation is a new observation, not a rewritten result.

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
branch/security review and exact-head CI. No automatic maturity promotion from unit/source proof.
The plan's PRODUCT system-profiles arm is separate from SOURCE-only system-profile-source. It
requires NEW exact native authority, normal ownership/all-targets authorization and any existing
audited override explicitly covered by that future grant; source UNOWNED-reference permission is
not reused. Metadata scenario creates two unique records, lists multiple rows, exercises all update
forms and fixed delete, preserving original complete baselines. Validation/activation/continue and
controlled partial-failure effects need separate power/maintenance scope. Recovery runs every
outcome; partial/unreadable/not-run never passes. Original source failed job remains retained.
Use the repository live-testing procedure for applicable native hardware proofs.
