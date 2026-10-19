# System-profile lifecycle — issue #1725

Proposed full product design after the source-capture/cleanup checkpoint. The writer-window,
fixed CLI delete and bounded association/encoding contracts are operator-approved as recorded
below. Full design review and scope audit still precede product implementation.
Scope: `q1725-d647765d`, campaign `822f9065dedb-622a646c-5bde-4f6a-b9e1-d78c64703a58`.

## Problem, scope and authority

System profiles associate multiple partitions with their contained partition profiles and may
validate/activate those associations. They are not partition profiles, backup files or saved
current configurations. Implement create, read/list, update/rename, delete and activation through
owning operations and supported CLI/MCP presentations; no public raw job/command interface.
The exact published scope and its exclusion-owner set were re-read at this checkpoint:
WORK:SCOPE6258196210 and issue #1725. “Approve all three child scopes”
(`call_b9d1a2d0e8f142d2b58841ddf72a20d5`, item0) remains unchanged.

Every mode, parameter and source unit of these five `docs/capabilities/rows.json` IDs stays in
#1725. The prefix is `rest:jobs/managedsystem-jobs/`; no row transfer or deletion is proposed.
The inventory is152source units, not the fixed M design denominator250 changed lines.

| Row suffix | Modes | Parameters | Source units | Proposed semantic coverage |
| --- | ---: | ---: | ---: | --- |
| activatesystemprofile_managedsystem-job | 10 | 24 | 34 | systemProfile; operation=on; test; continue; validate-only, activate and validate-then-activate, continuation variants |
| createsystemprofile_managedsystem-job | 10 | 22 | 32 | systemProfileName and complete logicalPartitionNames/logicalPartitionProfileNames pairs |
| deletesystemprofile_managedsystem-job | 10 | 18 | 28 | systemProfileName through the approved fixed documented CLI equivalent; truthful command outcome |
| getsystemprofiles_managedsystem-job | 10 | 14 | 24 | parameterless complete inventory; bounded result/returnCode/detailedStatus; missing/get-by-name and empty cases |
| updatesystemprofile_managedsystem-job | 10 | 24 | 34 | systemProfileName, optional systemProfileNewName and paired complete replacement arrays; rename-only, membership-only and both |

The reference JSON/XML request examples describe the same finite operations. Normalize their
semantics into the existing XML request builder; do not add a selectable raw encoding/proxy.
Transport representation, response fields and version statements remain covered by contract
fixtures/traceability; equivalent deletion must be recorded as such, never called a REST pass.
#638 retains borrowed LPAR SaveCurrentConfig; #698 retains VIOS/rmprofdata. ADR0118's six-name
facade stays exact. No dependencies, new authorization framework, scaffolding, automatic
maturity promotion. The encoding exclusion below is adopted only under the later explicit
operator decision. Approved pairs are: independent-writer fencing —
operator coordination; automatic replay/rollback — caller reconciliation; structurally
unencodable or undocumented association encodings — operator-managed pending authoritative
encoding support. “Approve both revised contracts” (`call_c202111f34f34b6bb0db4158db909334`,
item0) approves the backend and bounded association contracts plus that exact additional pair.

## Global Constraints

Python>=3.11; existing HMC REST/SSH clients, Typer/FastMCP and pytest; no new dependencies.
The REST job floor is HMC10.3.1060.0; a version alone does not prove every operation's capability.
No project architecture is declared. Host Darwin arm64/Python3.11.15, CI amd64/Python3.11–3.14
and native managed-hardware proof are separate facts. Preserve ADR0118's six-name facade,
#638/#698 ownership and all five rows/152source units. All three approved exclusion-owner pairs
apply exactly as recorded in WORK:SCOPE6258196210. No generic proxy, automatic replay/rollback,
backend selector or automatic maturity promotion. No current native effects are authorized.

## Source evidence and limits

Independent POWER10 and POWER11 REST snapshots define these five jobs from HMC10.3.1060.0:
`docs/refs/hmc-rest-api-p10/jobs/managedsystem-jobs/` topics042,047,050,053,074;
POWER11 topics042,047,050,054,077. The two Get pages define output as CLI stdout without a
nonempty example. Both Delete pages require `systemProfileName`. CLI references independently
specify `rmsyscfg -r sysprof`, exact system/name operands and system-profile-only removal:
`docs/refs/hmc-commands-p10/commands/rmsyscfg.md:17`–31,57–59 and the POWER11 equivalent.
These local references are source citations, not copied reference prose.

Earlier empty-source captures are retained as failed/empty observations, not list success:
read-only Get returned FAILED_BEFORE_COMPLETION with returnCode0 and ErrorData; five bounded
alternate-system CLI inventories were canonical empty. Their source revision10905eaaa was not
candidate proof. A later exact-source preparation revision
`5dfcf61f457351757cf2cb9ef532d664a097d4c4` (tree
`0e1337d457c0cb1f16fd62b93f23d6f306cc4388`) passed canonical verify578.60s and pinned hooks40.47s,
was isolated-deployed with matching manifests, and ran one separately approved scratch capture.
This is source-probe evidence, not product or activation proof.

On HMC11.2.SP1120, Create and Get completed successfully. Get's result was a JSON array with one
object containing string fields SystemProfileName, PartitionNames, PartitionIds and
PartitionProfileNames. The last three had two comma-separated positions; the selected partition
names and distinguishable profile names matched request order. Raw identifiers remain private.
Observed data does not establish quoting/escaping, empty members, multiple rows, another release
or numeric-ID-to-UUID binding. Those distinctions are addressed by the approved admission rules
and explicit tests/native prerequisites below, not by claiming broader native proof.

Delete failed to start with `Parameter 'systemProfileName' is not allowed for this Job.`
A read-only native Delete template named the operation but supplied no JobParameter declarations.
No alternate parameter or REST retry was inferred. The separately approved single documented
CLI scratch deletion returned success. Its runner exited1 on a subsequent after-read transport
error; a separate independent read-only witness then proved original complete member/profile/
power/ownership and namespace baselines restored, scratch absent. Original capture remains
FAILED/partial and failed own job retained; this does not become a passing lifecycle scenario.
Root reconciled that known command outcome plus independent baseline and released coordination.
All source capture/cleanup grants are consumed and authorize no further native effects.

Public-safe hashes: raw Get result `5ae61fd48eef1d05fc4f5bfc806c2562c060d19babafc13e07858981693ca95e`; original private capture
`dda500dd38c84bb8dbea41d7cf4b674634544323efb150082c316f3da381d0d5`; native Delete template
`b981686452dd6927d1d43e5e6eeff29902c1c26211c9a08316d17e24e57b7a3f`;
independent cleanup reconciliation `eb361a29c44eec427587483ffa6aae38515b31f702d44b521a7268d915d87987`.

## Approved backend contract

Approved: Create/Get/Update/Activate use fixed existing REST job transport; delete always uses
one fixed documented `rmsyscfg -r sysprof -m <bound-system> -n <exact-profile>` through existing
SSH transport. It is a deliberate backend choice before effects, never automatic fallback after
a REST attempt. No backend selector, retry switch or new public permission flag is proposed.
The issue permits documented equivalents, but the prior product draft described five job results;
the command-versus-job distinction is explicitly approved by the later operator decision.

For delete, resolve a unique CLI identity for the already authorized managed-system UUID. Use
existing identity evidence and documented MTMS form when necessary; a duplicate/unproven system
name must refuse before SSH mutation rather than target a different system. Quote both operands,
fix resource=sysprof, and expose no arbitrary flags or command text. Profile names come from the
complete authorized association inventory, not a loosely matched namespace string.

Delete retains all-targets grant, ownership of EVERY existing member, writer-window attestation,
immediate complete rereads and post-read reconciliation. Its result records SSH command
success/failure/uncertainty separately from observed absence and member preservation. It has no
fabricated JobID or terminal REST status. SSH timeout/interruption preserves uncertainty and
caller coordination; it cannot trigger another command or release the interval. Successful SSH
exit alone is insufficient: absence plus unchanged full member baseline must be read independently.
A failed first read remains a failed observation even if a later explicit reconciliation succeeds.
Product REST jobs keep submission/terminal status/CLI diagnostics separate. Require coherent
submission Resource.JobID/SELF and terminal identity for these fixed operations before trusting
inventory or effects; contradictory/foreign handles yield uncertainty, never a different operation
or automatic replay. Preserve the shared generic client compatibility contract unchanged.
Use existing outcome patterns, not a new general execution-result framework.

Alternatives: REST-only with the documented parameter is source-aligned but demonstrably fails
on the observed backend, so it cannot be described as working deletion there. Guessing another
parameter has no source authority. Trying REST then SSH is rejected: it adds an effect after a
failed/uncertain attempt and violates the no-replay contract. Moving the Delete row to another
issue or silently excluding this release is not proposed. Capability evidence must explicitly
name the equivalent backend and actual source proof; no maturity advance follows.

## Owning components, interfaces and data flow

Extend existing jobs/requests and client transport; place system-profile operations under
`operations/systems/`, presentation modules under existing systems CLI/MCP groups, and one fixed
SSH helper in the existing profiles owner under the approved backend contract. Direct dependencies
are resource identity, existing member ownership, bounded jobs, SSH transport and evidence docs.
No ownership migration or six-name facade export. Do not reuse #1724's unapproved save/delete
proposals or #638/#698 operations as implementation authority.

Five domain entry points share a complete system-profile inventory and contained-member resolver.
Create/update accept explicit partition-selector/profile-selector pairs; never public independent
unpaired arrays. Resolve selected connection/system, each contained partition, and each profile
through that partition's profile feed before serializing parallel native names. Reject blank,
duplicate, ambiguous/missing, cross-system and structurally unencodable pair values before effects.
Update may rename-only, replace all members only, or both; no implicit union/incremental mutation.
At least one change is required; both native membership arrays must be present together with equal
positive cardinality. Detect missing source name and rename collision from complete inventory.
Use selected profile identities, not names alone, in the immediate compare.

MCP mutations require existing all-targets grants because scalar selector metadata cannot
exhaustively declare dynamically discovered members. Authorize the connection/system and EACH
member under existing ownership rules, using only their already supported audited override where
permitted. Create checks new members; update checks old/new union; delete/activate check the entire
existing set. Validate-only still requires complete authorization/containment but omits the writer
attestation because it is observational. Discovery failure, partial inventory or one unauthorized
member refuses the entire mutation before submission; never silently filter a profile's members.

## Approved bounded association contract

Use bounded exact-successful Get results; failed/warning/nonterminal jobs or code0 with ErrorData
are not valid inventory. Before Get, a strict independent name-only CLI inventory may establish a
canonical empty namespace and return an empty observation without submitting the known-broken
empty Get job. Preserve that read backend/provenance and no atomic-snapshot promise. For nonempty
namespace require successful Get and equality of the complete unique system-profile name sets.
An empty Get array inconsistent with namespace, omitted records or duplicate names is not empty.

The approved bounded representation is the observed JSON-array/object shape, not a permissive
CSV/result guesser. Require the four exact string fields; reject duplicate JSON keys, malformed
JSON/types, blank/duplicate profile names, oversized/deep/unbounded data, and missing columns.
Apply the same item validator to each bounded array element; multirow support is a structural
inference to be tested, not a native observation claimed here. Preserve complete inventory only
when every record passes; no partial catalog can authorize an old-member mutation.

Parse PartitionIds as unique positive decimal IDs with one comma separator per member; require
all three member columns to have equal nonzero cardinality. Match each position against the
current selected-system partition feed: PartitionID, exact PartitionName and canonical UUID must
identify the same unique partition. Existing `snapshots/operations.py:383` and partition inventory
readers establish PartitionID as a separate resource property, not a UUID. Each profile name must
identify exactly one contained profile in that matched partition. The regular AIX/Linux source
references do not narrow product membership: never silently omit another native member kind.
Use its authoritative contained identity/profile source and the same existing ownership policy;
missing evidence invalidates the whole inventory. #698's separate VIOS operation ownership stays intact. A name/ID disagreement, absent
ID, stale UUID, duplicate partition or profile-containment failure refuses the whole record.
Do not zip truncated columns, match globally by profile name, or infer identity from position alone.

No undocumented quoting/backslash escaping is invented. Delimiter-bearing or structurally
ambiguous names/output refuse with an actionable unsupported-association-encoding diagnostic,
not an empty list, changed membership or permission bypass. Current input serialization likewise
cannot safely encode such names through the documented parallel comma lists. To avoid claiming
support for every native-valid name, the operator approved this exact additional exclusion-owner pair:
**structurally unencodable or undocumented association encodings — operator-managed pending
authoritative encoding support**. It is approved by `call_c202111f34f34b6bb0db4158db909334`, item0. The operator manages such profiles
outside this surface pending authoritative encoding support; the implementation cannot skip them,
filter members or switch parser/backend automatically. One unsupported record invalidates the
WHOLE inventory for list/get and all membership-dependent mutations. Full five-row allocation
stays with #1725; this support boundary and honest partial maturity are recorded in current WORK:SCOPE6258196210. Tests must distinguish observed simple strings, structurally repeated
rows, invalid escaped/delimited forms and unsupported format from absent capability.

## Approved writer-window contract and proposed result observations

No documented conditional-write parameter or ETag precondition exists on these jobs.
Approved contract: require a caller-attested exclusive writer window for create/update/delete
and activation. The window covers system-profile namespace/membership and member partition names,
profile identity/content and power operations from preflight until terminal reconciliation.
It is operator coordination, not a lock, lease, fencing guarantee or rollback mechanism.
Re-read the profile namespace, complete association and contained member profiles immediately
before submission; reject changed input. This catches observed changes but does not close TOCTOU.
A local process lock cannot exclude the HMC UI, CLI or other clients and is not proposed.
Operator decision “Approve #1725 writer-window contract”, async
`call_0b65189f319d4a9f96e1330bed7509c7` item 0, approves this exact contract only:

- Add `exclusive_writer_window: bool = False` to create/update/delete/activation domain and
  presentation entry points; reject false before a mutating domain job. List/get and
  validate-only do not require this attestation and make no atomic-snapshot guarantee.
- The caller coordinates the HMC UI, CLI, automation and other writers over the chosen managed
  system's profile namespace and exact old/new member names/profile content/power actions.
  This is an explicit caller responsibility, not a service-provided lock or lease.
- The interval begins before inventory/ownership preflight and continues through known
  terminal job status and post-read reconciliation. Capture complete old/new association and
  contained profile identities, authorize the member union, then re-read immediately before
  submission. Observed changes refuse submission; post-submission drift reports uncertainty.
- Mutations perform bounded polling using existing timeout/poll controls; no separate
  fire-and-forget mode is proposed. Timeout, missing job, interrupted client or transport
  ambiguity returns/preserves the known job handle and observed effects. The caller must keep
  coordination until that job and state are reconciled; timeout does not mean cancellation.
- No automatic retry, automatic rollback, implicit force, or claim of distributed fencing.
  Another invocation requires fresh discovery/authorization under a newly confirmed interval.
  Documented continuation controls member-error handling within that invocation; it is not
  an automatically replayed job or an authorization token for a prior failed invocation.
- Terminal success, return code and independent per-member observations remain separate facts.
  An observation unavailable or insufficient to prove requested-profile activation remains
  explicitly unverified. Approval grants no live effects or association-evidence waiver. Independent-writer fencing is
  owned by operator coordination; automatic replay/rollback is replaced by caller reconciliation.

## Activation observations and bounded outcomes

Expose a closed action vocabulary validate-only, activate and validate-then-activate; continuation
is allowed only when activation is requested. Map these to documented systemProfile/operation/
test/continue combinations without arbitrary parameters. Source `chsysstate.md:135`–136 supplies
validation/continuation semantics; the REST continuation wording does not authorize replay.
Both CLI snapshots additionally forbid sysprof activation in PowerVM management-controller mode
(`chsysstate.md:92`–98). Reject a provably unsupported release/advertised capability before
submission. Do not invent a controller-mode property or claim that version/template presence
proves activation support: native unsupported-mode refusal remains a distinct failed job, with
no successful activation claim or automatic alternate route. Do not assume REST can bypass that
restriction. Source a reliable mode/capability precheck if the implementation promises one.
Native activation has not been run.

Keep submission, timeout, failed/warning/successful terminal job, validation and member effects
distinct. For every member return requested contained profile identity, before/after state,
available associated/last-activated profile evidence and read errors. Classify unchanged,
changed-to-running, still-not-running or unavailable; none alone proves requested-profile
application. Already-running members are not newly activated. Aggregate terminal success and
per-member observations are separate facts; incomplete postconditions mean effects unverified,
not complete activation. No invented partial-result parser or automatic rollback. Preserve known
job handles and bounded diagnostics after timeout/interruption for explicit reconciliation.

Create/update postconditions require exact requested name/association inventory and unchanged
contained member profiles/power/defaults/ownership. Rename must remove the old name and create
only the new name; replacement must match the complete requested pairs. Delete postconditions
require exact absence and unchanged remaining inventory/member baselines. Any drift/read failure
is uncertainty, never a reason to retry. For jobs, terminal status, returnCode, result and
detailedStatus remain independent; missing/failed status with code0 is not success.

## Failure model

- Actors and deployments: authenticated CLI/MCP callers on supported HMC contexts; HMC responses;
  independent administrators honoring the coordinated interval; local Darwin arm64 and CI amd64
  Python3.11–3.14 verify different hosts, neither supplies native managed-hardware proof.
- Invariants/assets: unique contained member association and ownership; unrelated profile state;
  member availability; complete inventories; bounded private data and truthful backend outcomes.
- Accepted failures: independent-writer fencing is operator coordination; replay/rollback after
  uncertainty is caller reconciliation; structurally unencodable/undocumented association encoding
  is operator-managed pending authoritative support. Unknown records invalidate the WHOLE inventory.
- Covered elsewhere: existing transport/TLS/authentication and dispatch audit; partition-profile
  lifecycle #1724; borrowed SaveCurrentConfig #638; VIOS/rmprofdata #698. None waives member checks.

## Threat model and MCP evaluation

Added boundaries: pair/action inputs into fixed requests, HMC strings into association identity,
dynamic member sets into all-targets ownership, and job/command outcomes into presentations.
Existing dispatch, transport, jobs and SSH boundaries are widened only for these finite actions.
Authenticated callers and HMC output are untrusted at admission; local configuration is operator
controlled, and independent administrators are trusted only to honor the approved writer interval.
Pair/action input is controlled by closed values and XML escaping; response-to-identity by strict
whole-inventory validation; member-to-policy by existing all-targets/ownership checks; SSH
destination by proven target binding and quoting; output by bounded diagnostics/private export.
Immediate rereads and explicit uncertainty govern observed drift. Fencing/replay/unsupported
encoding are the approved owner-managed classes; existing transport security stays with its owner. No generic proxy,
new permissive authentication policy or general execution framework.

AI-SPEC: an authenticated MCP caller requests one finite system-profile action using selected
connection/system, explicit profile/pairs/action and existing policy options. Output is bounded
inventory or truthful backend/member outcomes from current contained HMC observations and approved
source contracts. No inferred grants, filtered unauthorized members, arbitrary commands, invented
success or automatic replay. Invalid/unknown evidence refuses before effects or reports uncertainty
afterward. Existing request/SSH/job/discovery deadlines plus a5MiB result limit bound work; cache
contained feeds per invocation rather than multiply fan-out. No LLM call/cost or model judge added.
Success is deterministic admission, exact fixed dispatch and honest observation, tested in CI.

| Case | Setup/input and severity | Required / forbidden traits | Gate |
| --- | --- | --- | --- |
| SP-E1 | Valid two-member fixture, unique names/profiles; severity4 | Exact fixed action/pairs; no unrelated target or proxy | block |
| SP-E2 | One foreign/unresolved old/new member; severity5 | Entire operation refused before effects; no filtered subset | block |
| SP-E3 | Stale ID/name/profile, duplicate/missing columns or unsupported encoding; severity5 | Whole inventory refused; no empty-success or fallback parser | block |
| SP-E4 | Failed job with code0, partial activation, command/readback failure; severity5 | Distinct failure/uncertainty and handles; no complete-activation claim/replay | block |
| SP-E5 | Oversized/deep data, bounded fan-out/deadline, hostile shell/XML names; severity5 | Bounded refusal and fixed escaped destinations; no extra effects | block |
| SP-E6 | Public evidence export from private source records; severity5 | Allowlisted structure/hashes only; no native identifiers/raw diagnostics | block |

Constructed fixtures cover invalid/multirow forms and are labeled constructed; sourced simple
Get fixtures cover the observed shape. Existing authorization, dispatch, evidence and integration
tests measure these traits; no uncalibrated LLM evaluation substitutes for assertions.

## Validation and reproducible native prerequisites

Before build: the backend/result and encoding/support guarantees are explicitly approved. Complete
design review over this spec, Proposed ADR0243 and the ignored full plan, then the scope audit. Product review budgets remain0;
SOURCE branch2/security3 history is retained separately, not reused as product approval.
No product scaffold or implementation begins at this checkpoint.

Offline proof must cover every row/mode/parameter/source unit, both independent snapshots,
all three update forms, every valid validation/activation/continuation combination, empty/missing/
collision/release/controller-mode cases, malformed/oversized/multirow/duplicate/delimited output,
ID/name/UUID/contained-profile disagreement, all old/new ownership failures, observed concurrency,
timeout/interruption and partial/unverified member outcomes. Test fixed CLI delete argv/quoting,
no REST attempt/fallback, no fabricated job status, command success plus failed readback, and
independent later reconciliation without changing the earlier failure. Controlled faults must
show new high-risk guard tests bite. Update discovery/generated docs/changelog and evidence-scoped
capability bindings, without automatic maturity. Shipping requires focused tests, canonical
just verify and pinned all-files prek at exact product HEAD, followed by CI's declared targets.

Use the repository live-testing procedure for applicable native hardware proofs. A final product
scenario needs a separate exact effect/target authorization, deployed HEAD/tree/manifests,
registered named preflight/arm/recovery, complete original namespace and member profiles/power/
ownership, coordinated writer interval and independently checked cleanup/recovery. Exercise at
least two system-profile records to prove multirow inventory, distinct members/profiles and all
approved metadata forms. Validation/activation and any controlled partial-failure arm require
separate exact power/resource/maintenance authority; they are not included in source grants.
Native not-run/failed/unverified cases stay explicit. Source capture is not product proof.

## Remaining gates and evidence

1. The writer-window and two revised contracts are approved under their exact recorded decisions;
   current complete WORK:SCOPE6258196210 includes all three exclusion-owner pairs. No further
   architecture question is presumed pending, and no new native effects are authorized.
2. Complete one full spellcraft design review over spec/ADR/ignored plan, fix defensible findings,
   run relevant checks/commit durable docs, and pass oathbind against the current approved scope.
   Product design/branch/security budgets were0 before dispatch; source history stays separate.
3. Implement and verify the full allocation only after that audit. Multirow, Update/activation/
   validation/continuation, other-release/source encoding and final product native behavior remain
   evidence gaps covered by explicit tests/scenarios, not passing claims or maturity promotion.

Optional consumed reservation remains `docs/adr/0243-system-profile-job-contract.md=0243`,
published under exact claim before creation. No migration/version change or ADR index assigned.
