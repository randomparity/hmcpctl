# Registered VIOS AME source diagnostic — #1768 prerequisite

## Authority and limits

[Scope](https://github.com/randomparity/hmcpctl/issues/1768#issuecomment-6418382312)
binds q1768-2b714f98, EMPTY exclusions, diagnostic L1000/full-spec and the exact
eleven-path implementation surface. [ADR0304](../../adr/0304-vios-ame-source-diagnostic.md)
is proposed. Original C07 remains eight fields/sixteen bindings/M250, incomplete.
This PR references rather than closes #1768. No product API or maturity promotion.
The decision requiring explicit shared mode for the other six C07 fields remains.
D1 reported one upheld inventory finding; this author revision awaits independent
D2. Audit/product/security counts remain zero; no hardware has run for this design.

## Architecture and interfaces

One source module owns the finite diagnostic, following the existing standalone
system-profile source pattern. Explicit routes in live_test_preflight.py and
live_test_recovery.py bracket the named live_vios_ame_source.py capture wrapper.
The ordinary runner, numbered subtasks, catalogs and src/ remain unchanged.

The module exports validate_packet(packet), preflight(hmc,packet,directory),
capture(hmc,packet,directory,*,grant_packet_sha256,exclusive_writer_window,
fresh_vios_delete_approved), recover(hmc,packet,document,directory), and main(argv).
The async entry points return bounded private dictionaries; main returns0 for a
complete observed source outcome,2 for refused/incomplete/held state. Recovery
returns2 for interrupted history even when current absence/baseline now agree.

Packet schema1 is closed: configured_profile, hmc_identity, system, tested_commit,
tested_tree, imported_source_sha256, cases, resources, baseline_sha256, window,
budgets, grant_binding and schema_version. It contains no credentials, command,
URL, arbitrary attribute, resource override or cleanup-override input. Cases are
exactly0 then1.10; each carries name, caller_nonce and description. Names derive
from distinct128-bit nonces and must be absent. Resource values are fixed below.
Budgets contains exactly stages (the four fixed ledger tuples) and rows (regular128,
vios8, profiles32, pools4, with_own_vios9, with_own_profiles33). These constants are
bound explicitly; they are not operator-adjustable limits. Grant binding carries
a distinct validated agent ID, ISO date and root grant ID.
The description is exactly `[hmcpctl owner:<id> created:<date>] [caller <nonce>]`.
Validate actual bytes with existing validators; no escaping or alternate marker.

Window fields bind a fresh owner-only directory, not-before and expiry. Private
exclusive-create stage receipts permit one initial preflight, final preflight,
capture and recovery per window. Keep receipts after failure. Initial preflight
alone permits baseline_sha256=null; its output supplies the stable digest for
the final packet. Final preflight and capture require that digest. A changed
digest requires a new root decision/window, not an automatic refreshed baseline.
Explicit capture flags must match the raw final packet digest and current grant.
Packet replay, saved grants and discovery by name never authorize another write.
The two-attempt ceiling is not reset by opening a new directory. After any submit
intent, a replacement capture needs a separate root disposition; it is not an
automatic restart under this diagnostic approval.

## Source custody and finite observations

Source base1aad137647baa771497870e01fbc875149666140. Verify the later implemented
clean deployed commit/tree, selected boundary-owner hashes, imported package path
and Python3.11.15 before live
use; this is the bounded deployment, not a new project Python floor. Root's
sourceclient5738 evidence is a baseline, not evidence the new arm is deployed.
Private source hashes accompany the author report. No reference-corpus prose is
copied into tracked artifacts; retained sources are cited by location/identity.

- P10 mksyscfg.md:41 and P11:49: vioserver, description, ded/shared, AME0/1–10.
- Existing `ssh/lpar.py` complete resource pairs/record and description validators.
- `client/core.py:966/1561`: fixed object/contained collection reads; no fallback
  through get_managed_system or identity-by-name in the snapshot call graph.
- `client/client_systems.py:364`: explicit SCSI/FC mapping groups in one read.
- `client/client_profiles.py:83/121`: contained saved profile and narrow comparison.
- Retained profile-memory schema8b14f694 defines saved sharing/AME/factor fields;
  optional absence is unavailable, not false/zero. Pool schema2f95cc34 defines
  pool capacities and provider associations. The retained pool capture includes
  HTTP204/no-content; it supplies no positive pool/runtime evidence.
- Captured rest-vios-entry, rest-vios-groups-comma and rest-ms-vios-feed-media
  fixtures establish default versus grouped VIOS field shapes, not AME acceptance.

Snapshot limits are diagnostic admission bounds, not claimed HMC limits:
128 regular partitions,8 pre-existing VIOSes,32 total saved VIOS profiles and
4 memory pools. A single proven own fixture permits VIOS9/profiles33. Count
encoded bytes as well as rows. Missing/malformed/duplicate identity or truncated
inventory stops before effects; it never becomes an empty collection.

The arm's counted physical GET boundary validates each opening/closing regular,
VIOS and pool collection, every saved-profile collection, the console collection
and the existing deleter's contained VIOS read before shared readers flatten it.
HTTP200 requires a nonblank complete Atom feed in the exact Atom namespace and the
captured typed UOM media form. One SELF link must bind the configured HTTPS
host/port and exact collection path, without query or fragment. The only other
admitted envelope link relation is MANAGEMENT_CONSOLE with its scoped UOM UUID path.
Member IDs and single typed content resources must agree with the requested UOM
kind/namespace; direct entry metadata has a closed supported shape. Optional scalar
feed metadata is limited to id/title/updated/generator. Unknown root attributes,
feed/entry extensions or link relations hold; resource subtrees retain their
existing full/selected comparison contracts. No general Atom pagination is inferred.

Reject HTTP Link/Content-Range headers,206, contradictory Content-Length, malformed
XML, standalone entries, wrong media/resource types and foreign SELF bindings.
Unknown completeness extensions (including OpenSearch totals) hold rather than
being interpreted as evidence that a partial page is complete. No extra fetch,
followed link, retry or expanded physical budget is introduced.
Only a bodyless SharedMemoryPool204 with absent/text/plain media is an established
empty sentinel (tests/fixtures/live/vocabulary/v10r3-8375-42a-memory-source.json:4,24–28).
Regular/VIOS/profile204
responses remain unavailable; this conservative diagnostic refusal does not change
shared client semantics or claim they cannot legitimately represent empty data.

Protect this exact finite whitelist, preserving presence separately from value:

| Object | Protected observations |
| --- | --- |
| System | UUID/name/operating state, firmware, classic/controller flags, VIOS/AME capabilities; memory region/configurable/available; configurable/available CPU/minimum-unit facts |
| Regular partitions | UUID, ID, name, type, state, description, default/last/current profile association; complete memory/processor subtrees from contained feed |
| VIOS default detail | Same identity/resource/profile facts plus complete PartitionIOConfiguration, ClientNetworkAdapters, HostEthernetAdapterLogicalPorts, SRIOVEthernetLogicalPorts, SRIOVRoCELogicalPorts, SharedEthernetAdapters, TrunkAdapters, VirtualNICBackingDevices, PhysicalVolumes, StoragePools, MediaRepositories |
| VIOS grouped detail | Complete VirtualSCSIMappings and VirtualFibreChannelMappings; absent collection is unavailable |
| Saved VIOS profiles | UUID/name/SettingID/ETag and full semantic XML, including unknown fields and metadata; only direct outer Atom publication text normalizes, using existing comparison semantics |
| Memory pools | Complete returned Resource, mandatory unique PoolID; capacities/provider associations remain observed, never inferred; existing nonempty pools require observable provider association containers |

Selected subtrees retain nested metadata and list order; do not strip all Metadata.
Unselected telemetry such as uptime/utilization is outside this configuration
witness. Referenced link identities are preserved and scoped where required;
the arm never follows arbitrary response URLs or claims backing-device contents.
Optional absent adapter fields are recorded as absent, not as empty/unsupported
devices. Present opaque fields remain compared. A protected field drifting during
normal operation causes a hold; it is not removed to make the experiment pass.
Root coordinates an exclusive writer interval; snapshots cannot fence another HMC
administrator atomically. Raw GET response bodies (status/path/ETag and content-type/Content-Length/Link/Content-Range
envelope headers only; no authentication response or other headers) and comparison projections remain private. Snapshot serialization is
also capped at2MiB; projection expansion may refuse a smaller transport payload.

A snapshot makes8 fixed REST calls: console, system, initial LPAR/VIOS/pool feeds,
then closing LPAR/VIOS/pool feeds;3 per VIOS: default detail, grouped detail,
profile collection;1 per saved VIOS profile. Closing membership/identity/profile
links must agree with the first inventory. One bounded lscomgmt read establishes
classic mode using the existing native-null plus true/false/false authority tuple.
Controller or unknown mode is unavailable; no fabricated default profile.

## Admission and lifecycle

Require fresh current capabilities, exact configured target/agent, closed source
identity, inactive fresh names, valid region alignment and fixed resource bounds.
Use memory1024/2048/4096MiB, shared CPU0.1/0.5/1.0 units, vCPU1/1/2, uncapped.
Memory is dedicated; shared CPU does not imply shared memory. No all_resources,
HPT/PPT, hardware, role, device, paging or provisioning input is exposed.

Before each create, persist exact pairs/record hash, initial marker and submit
intent. The sole fixed native create uses the existing completed resource pairs,
record encoder, shell quoting and bounded SSH transport. No REST fallback or
post-create stamp. Mark submit-entered durably before entering transport.

After native completion, take a complete snapshot including the candidate.
Native coded refusal plus scoped absence and unchanged baseline is an observed
rejection, not AME-specific unless the actual refusal establishes it. A timeout,
uncoded failure or ambiguous completion is unknown and stops further writes.
An acknowledged create still needs unique fresh UUID/name/type/inactive identity.
Read its saved profile independently: sharing=false; AME0 => enabled=false;
AME1.10 => enabled=true and exact Decimal1.10. Missing/duplicate values are
unavailable; contradictory values mismatch. Current memory facts are separate;
no profile apply/activation is used to manufacture agreement. Retain one exact
filtered full native profile listing as raw supplementary source evidence;
do not invent undocumented projected-column semantics from that text.

Before cleanup, require current root grant/source/target/window, single positive
native completion, prior absence, a fresh contained UUID, exact initial marker
from independent native description read, agreeing latest detail identity,
not-activated state, foreign baseline equality and delete not-attempted. A present
REST description must also agree. No marker adoption after uncertain creation.
Persist delete-pending, then invoke the existing scoped inactive VIOS deletion
once by UUID. Its own containment/quick-state checks remain additional guards.
Only independent absence and full baseline equality establish cleanup. Global
free-capacity equality is required after deletion; while the owned fixture exists,
retain the observed aggregate but compare foreign configuration separately.

Unavailable/mismatching AME can permit cleanup when ownership and foreign state
are proven, but stops the next case. Only reconciled acceptance or native coded
rejection permits case two. No third create or repeated delete. A failed journal
checkpoint stops resource writes; last durable intent remains the recovery witness.
Unknown create/delete, missing marker, changed foreign state or exhausted bounds
produce held residuals. Root separately dispositions later exact-UUID cleanup.

Recovery validates every retained journal phase and independently rereads current
state. It contains no creator, deleter, stamp, power or adoption path. Current
absence/baseline agreement and uncertain historical completion are separate facts.
Missing/malformed evidence remains held; a partial run cannot be rewritten clean.
Complete history is rederived from retained baseline/case/restoration snapshots,
profile observations, native marker and latest identity records, not success labels.

## Physical budgets and closeout

Let B=8+3V+P<=68 REST and1 SSH per complete snapshot, including the temporary
fixture. Each case adds1 independent detail REST plus at most3 REST in existing
UUID deletion, and3 SSH (create, raw profile, description). Validated empty200
deletion-feed fallback costs2 REST and stops before delete; it does not exceed the3-call branch.

| Stage, once each | Snapshots | REST ceiling | SSH ceiling | Retained payload allowance | Absolute runtime |
| --- | ---:| ---:| ---:| ---:| ---:|
| Initial preflight | 1 | 70 | 1 | 4MiB | 170s |
| Final preflight | 1 | 70 | 1 | 4MiB | 170s |
| Capture, including one uncertainty reserve | 6 | 418 | 12 | 20MiB | 1130s |
| Independent recovery | 1 | 70 | 1 | 4MiB | 170s |
| Complete four-stage allowance | 9 | 628 | 15 | 32MiB | 1640s |

These are structural upper bounds, not observed speed or an execution grant.
Each snapshot has120s/2MiB aggregate; each case has60s create plus120s observation/
cleanup and3MiB aggregate; each stage reserves20s logout and10s local close/reap.
Logon gets20s. REST reads/requests have20s maximum; native create60s, other SSH20s.
REST response maximum1MiB, SSH64KiB; remaining block/global byte bounds can be lower.
These byte ceilings bound accepted/retained application payload, not wire traffic or
transport buffering. Charge a full per-call allowance on uncertain/error completion;
refund only a complete observed smaller payload. SSH reserves10 bytes for its
existing stderr label. Counters sit on physical HTTP/SSH boundaries, include errors and nested calls and
refuse before over-budget dispatch. Before create, reserve both remaining normal
case paths plus one uncertainty snapshot and logout/close; do not borrow that
reserve for more observations. Deadlines shrink per request and never extend.
Budget exhaustion is a held outcome, not permission for cleanup without proof.

One REST session per stage; at most four. Close/await each own SSH process and
connection, attempt own logout once, close HTTP and reap only the root's own
access-host process. Unknown logon/logoff remains explicit session residue in a separate session.json
closeout, so a failed journal checkpoint never rewrites the last durable intent.
A clean body with uncertain closeout returns2 and requires root reconciliation.
Recovery also requires the capture companion's matching packet and confirmed
closeout for clean=true; present absence/history cannot erase an unknown old session.
Local source attestation has at most39 read-only Git subprocess attempts across
the four stages (6+6+21+6). Each has5s execution,1s reap and64KiB retained output;
those elapsed times consume the same stage deadlines. Disable fsmonitor/untracked
cache and optional locks; no Git mutation is requested. Reap only each own child.
Root's outer controller enforces the process deadline if local I/O stalls; killing
that process does not prove remote command cancellation. No foreign session kill.

## Evidence, validation and rollback

Source journal schema1 carries source_only=true, group=vios-ame-source, subtasks=[],
exact revision/tree/import hashes, packet/baseline digests, requests/effect states,
per-case source observations and cleanup/session status. No scenario/operation
promotion entries and no record_verified/emit_observations call. Renderer requires
matching group/source flag and emits SOURCE ONLY with fixed safe outcome/cleanup
labels. It never prints raw errors, names, host identity or ordinary product PASS.
Raw packet/results are0600 in fresh0700 directories outside the checkout.

Offline tests cover packet/identity/grammar/budget failures before effect, marker/
state/baseline deletion guards, once-only receipts, native uncertainty/no replay,
checkpoint failure, all-phase read-only recovery, zero/factor observations, exact
call counters, privacy and preservation of existing dispatcher behavior. Controlled
faults must demonstrate those tests fail. The plan binds exact tests/commands.
Later implementation runs repository focused checks and required full shipping
gates. Design preparation runs no product imports/tests/setup or hardware.
Software rollback removes the new registration/entry point; it never deletes a
residual resource or rewrites retained journals. #1768 remains open for product work.

## Failure model

1. Actors and deployments
   - Trusted root/operator on the exact clean source checkout/Python3.11.15 deployment.
   - Existing configured HMC credentials; classic selected managed system; offline CI mocks.
2. Invariants and assets at stake
   - Existing service VIOSes, protected configuration/profile/pool baseline and availability.
   - Exact target/grant, single own-fixture effects, honest uncertainty and source attribution.
3. Accepted failure classes
   - HMC rejects either value or omits observations: bounded source result/hold, no support claim.
   - Transport/local crash or concurrent administrator: held residual, no guaranteed compensation.
   - Protected telemetry drift/oversized inventory: refusal rather than weakened comparison.
   - Guest AME behavior and linked storage contents: not observed by an inactive configuration probe.
4. Covered elsewhere
   - Product VIOS applicability and original C07 completion: #1768, unchanged product charter.
   - Pool lifecycle/profile CRUD: #642/#1724; no mutation delegated to this diagnostic.
   - Credential transport/TLS/native encoding: existing configuration/client/SSH owners and gates.

## Threat model

1. Boundaries: private packet and filesystem; HMC feed/XML/native output; native creation record;
   conditional UUID deletion; source journal to optional filtered report.
2. Actors: trusted local operator/root; untrusted malformed or stale HMC data and accidental
   packet/journal changes. Same-user malicious code/credential compromise is outside this model.
3. Controls: closed bounded schema, digests and private once-only receipts; UUID/containment and
   exact marker checks; existing safe XML/record/shell encoders; finite counters/deadlines;
   immutable uncertainty history; fixed report vocabulary without raw HMC text.
4. Outside: atomic exclusion of external HMC administrators, guest execution, HMC internal defects
   and compromised local authority. Root coordination and held residuals limit rather than erase risk.
