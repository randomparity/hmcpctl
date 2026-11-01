# Retained fixture for vNIC source qualification

Design for #1828 at `07d8a360460aedd8d502fbad5755b7b6e67f7238`.
Authority: [amended scope](https://github.com/randomparity/hmcpctl/issues/1828#issuecomment-6563693890),
token `q1828-eed0dad6`, and the maintainer's explicit prerequisite approval in
issuecomment-6563667424. [ADR 0312](../../adr/0312-vnic-policy-updates.md) records
the owner decision. Status: design; no fixture stage is yet runnable.

## Problem and scope

Source-v2 needs an already-owned, caller-bound inactive LPAR. Existing test arms
perform additional experiments and teardown; advisory ownership alone does not
make an existing partition disposable. This prerequisite creates and retains one
fresh fixture, then separately cleans up only that proven fixture. It does not
run source cases, implement the public updater or establish native update support.

Retain all five parent fields and later modes. Creation-policy product work stays
with #1827, backing updates/failover with #669, system defaults with #680,
multi-backing/ledger transfers with #638 and profile CRUD with #1724/#637.
Root alone admits target-bound hardware windows. No activation, guest, media,
disk, network creation, VIOS/pool change, profile apply, override or maturity claim.

## Global Constraints

Python >=3.11; local pinned Python 3.11 and CI Python 3.11–3.14 on amd64 Linux.
Local arm64 macOS verification is distinct evidence. Add no dependency or tool.
Use `just setup` and `uv run --no-sync`; preserve the six-name `hmcpctl.api` facade.
Keep plans ignored, use existing operation/transport ownership, and redact public
evidence. Version-1 and source-version-2 behavior and limits remain unchanged.
Fixture preparation, source capture, fixture cleanup and recovery are separate
invocations; root admits each hardware window. No publication authority is added.

## Owner and interface

Extend `scripts/live_test/vnic_creation_policy.py`. Existing preflight, standalone
capture wrapper and recovery dispatcher remain the only entrypoints. A separately
validated version-3 packet with purpose `vnic-source-fixture` selects `prepare` or
`cleanup`; the capture entrypoint dispatches that explicit packet action. Existing
version-1/2 packets keep their current routing and result interpretation.

The new purpose reuses `create_and_stamp_lpar`, `LparCreation`, `LparResources`,
`delete_lpar`, `read_native_profile`, existing ownership parsers/authorizer,
attribute builders, private readers and atomic `save`. No production operation
contract changes, owner move, compatibility re-export or general fixture service.
No existing record is re-stamped or adopted by a name-only lookup.

## Packet and history boundaries

The closed packet has `version`, `purpose`, `action`, `configured_profile`,
`hmc_identity`, `expected_commit`, `system_uuid`, `fixture_name`, `caller_token`,
`baseline_sha256`, `expires_at`, `exclusive_writer_window`, `allow_fixture_effect`,
`prepare_journal`, `source_declaration`, and `source_evidence`.
Version is strict integer3; action is `prepare` or `cleanup`; identity has exactly
host/port. Retain exact clean candidate/import, UUID, private-file, raw-byte digest,
duplicate-key/nonfinite rejection and strict scalar checks from the current owner.

Name is `hmcpctl-vnic-fixture-` plus16 lowercase hexadecimal characters; caller is
`q1828-fixture-` plus the same nonce. Both are unique against the bounded current
inventory. Expiry is a positive integer, writer/effect acknowledgements are true.
Baseline is null only for an initial preflight, then a canonical snapshot digest
bound into a newly hashed packet. Capture rechecks fresh equality before effects.

Prepare requires null `prepare_journal`, declaration `none-invoked` and an empty
source list. Cleanup requires the original successful preparation journal path.
Its declaration is independently explicit: `none-invoked` requires an empty list;
`complete-set` requires one to six records. Each record has exactly `packet`,
`capture`, and `recovery` private paths. Duplicate records are refused. No path
is followed without the existing private regular-file/no-symlink checks and bound.
Source packet files are at most1MiB; each source capture/recovery is at most5MiB;
the six-record input ceiling is66MiB. The fixture's own journal remains5MiB.

Root declares the complete set of source invocations during its coordinated
window. This is the existing trusted-coordinator assumption, not a discovery or
ledger guarantee. Missing, malformed, partial, uncertain or contradictory supplied
records disarm cleanup. An explicit no-source declaration cannot override a known
recorded attempt. Attestation cannot manufacture successful source evidence.

History records the validated packet, original baseline, captured UUID/id/name,
profile identity/default pointer/ETag, current observations, exact effect intents
and settlements, counts and own-session closure. Prepared retention and completed
deletion are different terminal states. Retained history never restores authority
to retry an effect after restart. Recovery has no mutation allowance.

## Fixed resource and preservation proof

The resource tuple is memory min/desired/max512/512/1024MiB, shared capped units
0.05/0.05/0.05, vCPU1/1/1 and maximum virtual slots8. Require an operating target,
positive memory-block size dividing512 and1024, at least1024MiB available memory,
at least0.05 available shared units, and positive minimum units per vCPU at most
0.05. Unknown/nonfinite/malformed or inadmissible values refuse; never increase
resources or change processor mode. No claim that this is a universal HMC minimum.

Bound the inventory to at most four pre-existing partitions/VIOSes and eight saved
profiles; preparation may add one partition and its one default saved profile.
Read contained feeds and independent full current resources, retaining unknown
resource/I/O fields. Read each bounded contained saved profile with the existing
native profile reader, including UUID, name, SettingID, parent association and
ETag. Reject duplicate/ambiguous identities and unavailable full resources.
Only the explicitly volatile uptime observation is excluded from preservation;
it supplies no fixture or resource authority. The remaining foreign projection
must remain equal before/after this procedure's effects.

Configured tuple proof comes from the uniquely contained `default_profile`, bound
to the current default pointer and exact parent/candidate. Check its shared/capped
mode, all memory/CPU bounds and max slots; reject nonempty configured I/O beyond
the fixed virtual-slot ceiling. Preserve current inactive placeholders exactly:
do not replace zero/unknown current allocations with configured desired values.
Current placeholders alone cannot prove or disprove the configured tuple.

Capture one empty target-vNIC projection and complete system backing projection.
Preparation cannot hand off pre-existing adapters or unexplained backing changes.
The source-v2 preflight separately proves its selected adapter/port/VIOS/capacity
eligibility; preparing this fixture earns no source admission or positive oracle.

## Lifecycle and effects

1. Preflight is read-only, captures bounded baseline and validates the fixed
   resources and selected action's history. It never changes the input packet.
   A separate recovery invocation verifies its closed, effect-free history and
   fresh equal observation, reporting `UNCHANGED`. Keep each packet's original
   bytes when binding a new baseline digest; recovery uses the packet it observed.
2. Prepare takes a fresh matching baseline, persists a once-only REST-create
   intent and calls `create_and_stamp_lpar` with required stamp and no apply.
   The existing fallback is permitted only after a completed406 or400REST0001
   pre-creation refusal. Its native create has its own durable intent. Timeout,
   ambiguous success, unrecognized refusal or failed readback never permits retry.
3. Before the one stamp, independently confirm a fresh contained UUID/name/id,
   inactive state, exact saved resource tuple, preserved foreign baseline and no
   vNICs. Scope the check around the existing stamp owner. Persist stamp intent
   before its physical SSH dispatch. Afterward independently compare REST/native
   descriptions, effective owner and caller token. Acknowledgement alone is
   insufficient. Any unknown outcome stops, retaining residue for recovery.
4. Successful prepare records `PREPARED` only with confirmed identity/preservation
   and own logout. Separate read-only recovery may report `RETAINED` only from
   complete successful history plus a fresh equal retained observation.
5. Cleanup validates the original preparation and declared source evidence. Each
   source packet must bind the same candidate/system/fresh UUID/id/caller/state;
   its complete CLEAN capture is recomputed with the existing `_source_history`
   contract against fresh source baseline. Require its matching independently
   completed CLEAN recovery and closed own session. Source-v2 semantics do not
   change. Unknown or failed source histories cannot be cleared by current absence.
6. Fresh contained inactive identity, caller/owner, saved-profile identity/ETag,
   preserved current placeholders/unknown fields, foreign projection and original
   empty vNIC/backing baseline must still match. Then permit one exact UUID DELETE
   through `delete_lpar(..., ownership_override=False)`. Its existing fresh reads
   remain bound to the qualified object before the physical effect. No power call,
   guessed deletion or enclosing deletion to hide source residue is permitted.
7. Independently observe fixture absence and original foreign baseline after
   acknowledged deletion. Only complete history and own logout permit `CLEAN`.
   Separate read-only recovery independently rechecks this state and the original
   history. An incomplete journal stays incomplete even if the fixture is absent.

## Proposed physical budgets and verification

Preflight proposes64REST/40SSH/120s; recovery32REST/24SSH/120s;
prepare96REST/24SSH/300s; cleanup128REST/64SSH/300s.
These are design caps, not hardware admissions. Cleanup preflight and capture may
validate six separately selected source baselines, adding up to30REST/24SSH;
the larger cleanup cap also includes its fresh physical-delete guard and aftermath.
The bounded inventory/profile graph explains the larger REST allowance: at most
21 requests per original snapshot and24 after adding the fixture, including the
three-request ManagedSystem null-property path. Count own logon/logout, empty-feed
summary reads, both recognized create-fallback branches, native name fallback,
stamp, fresh deletion checks, original source verification and outside recovery.
Offline complete physical traces must substantiate the graph and suffix before
execution; a discrepant graph returns to design rather than silently increasing caps.

Reserve one REST slot and the final15s for own logout. Effects require a remaining
complete observation/closeout suffix, at least15 body seconds, and fresh wall and
monotonic checks after atomic intent persistence. Apply refreshed remaining time
to the physical dispatch timeout. Input/REST/native output remains1MiB, own journal
5MiB; oversized save preserves the preceding journal and prevents dispatch.

Tests exercise REST creation and recognized native fallback, combined null-property
and ownership-name fallback, strict capacity/profile/placeholder observations,
foreign drift, uncertainty at each effect, late cleanup drift, false no-source
claims over known history, complete/partial source evidence, byte/count/time
exhaustion, delayed intent, own logout failure and independent recovery. Controlled
faults must demonstrate validation, history, cleanup and budget tests bite. Test
the unchanged v1/v2 contracts and actual wrapper/preflight/recovery boundaries.
Run affected tests/static checks, then normal independent product/security review,
simplification and one exact final `just verify` plus separate pinned all-files hooks.

## Failure model

Deployments: one trusted root coordinator on a private operator host, exact clean
candidate and configured target, exclusive coordinated writer windows; bounded
remote REST/SSH observations may be stale, malformed, refused or incomplete.
Assets: foreign partition/configuration and backing state, captured fresh identity,
finite effects/resources, truthful retained/clean history and private evidence.
Accepted: an uncoordinated external writer can race the final observation; no
atomic HMC compare-and-swap exists. Refusal, timeout, bounded unreadability and
unknown completion may leave residue rather than guess a safe effect. Trusted
coordinator completeness of declared source invocations is not independently
discoverable. Stored configuration does not establish traffic or support maturity.
Covered elsewhere: HMC access/window admission by root; transport parsing/output
and profile containment by existing owners; product API/later modes by #1828;
backing/product/profile ownership by the exclusions above.

## Threat model

Added boundaries: fixture packet/history paths to local authority; remote fresh
identity/profile/capacity to create/stamp/delete authority; declared original source
records to cleanup eligibility. Widened boundaries: existing private journal and
physical transport budget owner now gates LPAR effects as a separate purpose.
Trust root's private configuration, declared invocation completeness and exact
current effect grant; do not trust packet shape, stale journal contents or remote
claims without validation. Closed schemas, candidate/destination binding, contained
profile identity/ETag, required owner/caller equality, exact once-only effects,
independent histories and finite physical budgets govern these crossings.
No credentials enter packets; terminal output contains safe status/type only;
private raw evidence remains0600 under0700. Same-user malicious file replacement,
compromised HMC truth and uncoordinated writers are outside atomic protection;
existing private-file checks and the explicit operational trust model apply.

## Shared Description integration

Correct the verified source preflight/removal representation mismatch in the
existing owner: use `leaf_text` for strictly validated REST attribute leaves and
remove only one native LF/CRLF record terminator before semantic comparison.
Refuse malformed leaves, controls, multiple records and real text/owner/caller
drift. Preserve raw returns/resources; fixture history also retains the native
raw description. Apply the same boundary in new held/prepared fixture reads.
Bare/attributed REST and native no-ending/LF/CRLF must complete v1/v2 and fixture
lifecycles/recovery; negative and late-drift cases must disarm the next effect.
No public transport contract, source-v2 semantics or safety guarantee changes.
