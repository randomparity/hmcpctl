# Partition-profile lifecycle (#1724)

Approved implementation design for scope `q1724-1608abef`; decision [ADR0242](../../adr/0242-partition-profile-lifecycle-contract.md).
Operator “approve” accepted complete-design/implementation checkpoint6344185745; implementation
scope6344262032 records the exact five-row170-unit allocation,unchanged three exclusion-owner
pairs,readback/four-root-scalar/projection decisions and no-further-review/scope-audit waiver.
M250 and spent design2/2 budget remain unchanged. Run focused tests and shipping guardrails;
no new hardware effects,merge or automatic maturity promotion is authorized.

## Problem and source evidence

The existing profile family backs up/restores whole-system profile data and changes the persistent
sync setting. It has no supported per-profile lifecycle. Upcoming #1225/#1226 must consume these
primitives rather than confusing current configuration with a saved profile (ADR 0201).

The independent P10/P11 profile topics list GET/PUT/POST/DELETE and ProfileName quick reads:
`docs/refs/hmc-rest-api-p10/managed-system/150-logical-partition-profile.md:17` and
`docs/refs/hmc-rest-api-p11/managed-system/170-logical-partition-profile.md:17`.
Their method tables refer to a schema rather than defining field shapes. Read-only retrieval of
`/rest/api/web/schema/LogicalPartitionProfile.xsd` returned 200, SHA-256
`20f91053e66d4090e70127ffc08d2a188da2d1e774bff1a566c0fdcc14ff3477`.
A native profile GET returned 200 with ETag and one profile element; capture SHA-256
`c04ec839407401f29293b2bd862f119e9f4157b3f03fa1d44ba7a6b1ca36302d`.
These initial captures are source observations, not mutation proof; no raw capture is public.
The profile HTTP metadata records schema V1_17_0, HTTP 200, ETag present and capture date
2026-10-18T19:30:27 GMT. The initial capture release/build was not established. The acquisition used the
existing client and the unambiguous configured default, not an invented deployment context.
A bounded read-only survey of 16 existing managed systems on that same HMC returned the exact
`No results were found.` sentinel for each system-profile query; no nonempty shape was observed.
The query shape was `lssyscfg -r sysprof -m <resolved-system> -F name:lpar_ids:profile_names --header`.
Its 23-byte empty output has SHA-256
`2d55f3a16b3f6940092590f293efb65ea1747a98c3e862b16a8eafbc2f91285f`.
Private acquisition provenance retains exact requests, headers and commands beside the source
bodies. The issue handoff identifies the private continuity record; an authorized operator can
repeat these GETs and this bounded CLI read with the existing loader/client/transport. A body hash
alone does not prove HTTP metadata; no opaque ETag, selector or private path is published.
`ProfileMemory`, `ProcessorAttributes`, and `ProfileName` are actual profile fields.

P10/P11 `http-protocol/014-request-headers.md:30` documents conditional PUT/POST; the corresponding
status-code topic at line 23 describes POST/412. Neither establishes conditional DELETE.
Existing `client_lpars.update_logical_partition` and ADRs 0169/0171 establish the local RMW pattern.
The two ChangeDefaultProfileName and SaveCurrentConfig job pairs require 10.3.1060.0 or later;
`mksyscfg -o save` is the documented older SSH equivalent. The explicitly approved allocation
assigns both SaveCurrentConfig rows to #1724; mixed CLI rows retain their #638 owner and
require semantic equivalence reconciliation, not duplicate implementation or whole-row credit.
`rmsyscfg.md:32` documents system-profile deletion cascades. No cascade editing belongs here.

## Exact approved allocation

Preserve every mode, parameter and source unit of these five IDs in docs/capabilities/rows.json:

- rest:managed-system/logical-partition-profile
- rest:jobs/logicalpartition-jobs/changedefaultprofilename_logicalpartition-job
- rest:jobs/virtualioserver-jobs/changedefaultprofilename_virtualioserver-job
- rest:jobs/logicalpartition-jobs/savecurrentconfig_logicalpartition-job
- rest:jobs/virtualioserver-jobs/savecurrentconfig_virtualioserver-job

Exact row counts are46 for profile CRUD,30 each for the LPAR/VIOS default jobs,and32 each for
the LPAR/VIOS save jobs. For every row,the union of modes and parameters equals its source-unit
set; both release variants remain included. List/read and contained-name selection cover quick
reads; native jobs cover submit/result semantics and their named parameters. This is a design
mapping,not implementation credit. The private allocation reconciliation retains all170 unit IDs.
The first three contribute106sourceunits; both32-unit save rows bring the total to170. Mixed
CLI rows remain with #638; preserve equivalent semantic bindings without claiming whole-row
implementation. No new exclusions, discovery-epic work or sibling allocation are absorbed.

## Concrete proposed interface and ownership

Keep policy in a new `operations/lpar/profiles.py`, native transport in an owning client profile
module, and presentation adapters in existing LPAR profile modules. Reuse partition/VIOS
resolution, ownership authorization, bounded HTTP transport, XML parsing and job outcomes.
No facade exports, generic XML/attribute patch, arbitrary commands, dependencies or local lock.

- `list_partition_profiles(target, partition_type)` returns bounded contained identities, names,
  saved resource fields, and opaque ETags; `read_partition_profile` resolves exactly one UUID or
  exact name inside that feed. Names-only selection is the ProfileName quick-read equivalent.
- `update_partition_profile(target, profile_selector, expected_etag, patch)` takes a typed patch:
  name, boot mode, memory min/desired/max MiB, processor min/desired/max, shared processing units
  min/desired/max and sharing mode. Omitted values preserve the fetched XML, including adapters.
  Reject inconsistent ranges/modes, missing required existing elements and unknown fields locally.
  Re-fetch the contained instance, compare the caller's ETag, apply only the typed elements and
  POST the complete profile with If-Match. Missing ETag or 412 refuses; do not retry.
- `save_current_partition_profile(target, name, overwrite=false)` is also the supported create
  path: new named profiles are created from the current configuration. Do not add a redundant
  create wrapper. Explicit save remains distinct from the persistent sync setting.
- `set_default_partition_profile(target, selector)` resolves contained profile identity and uses
  the documented default-profile job. Return submitted versus terminal versus verified state
  distinctly, and check the job's CLI returnCode/result/detailedStatus rather than transport alone.
- `delete_partition_profile(target, selector, expected_etag)` supports only an eligible profile:
  not current, default, last/required, synchronized or referenced by a system profile. Enumerate
  an unfiltered system-profile inventory before deletion. The proposed currently source-grounded
  eligibility admits only the canonical empty sentinel above. Blank output, a header alone, any
  nonempty record, malformed output or failed read refuses with an actionable explanation; no
  absence is inferred by parsing an unobserved member-pair shape. This approved conservative system-wide
  eligibility boundary does not claim arbitrary nonempty-membership support. Use native deletion
  only after the concurrency gate below.
  #1725 may later alter memberships, but this path never edits them or authorizes cascades.
  The proposed finite partition predicate reads `DefaultProfileName`, `LastActivatedProfile` and
  `CurrentProfileSync` from the contained parent resource. Their scalar shapes are captured in
  `tests/fixtures/live/rest-lpar-create-ok.json` and `rest-vios-groups-repeat.json`; existing
  `profile_sync.py` treats LastActivatedProfile as the current profile. Require these identity
  fields to be present and unambiguous; missing/empty current identity refuses. Require more than
  one unique contained profile and a target name distinct from both protected names. If the
  parent carries an AssociatedPartitionProfile link, protect that linked UUID too. Admit only
  observed Disabled/Suspended sync states; On/unknown refuses. Re-read this snapshot and the
  empty system-profile sentinel immediately before deletion; any observed change refuses.

LPAR and VIOS use their own parent type in containment, jobs, targets and authorization. Missing,
ambiguous or foreign selectors refuse before mutation. Existing activation containment remains.
CLI/MCP mirror these domain operations with operation/effect/target metadata, generated docs and
experimental/unverified maturity. No automatic promotion or claim that the private sample proves implemented feature maturity.

## Approved typed-update readback contract

User 2026-10-22 selected “Report applied with preservation drift and list added fields; retain strict
recovery-copy equality (Recommended)”, call_dc8a4f14459c4fe88003274d4ab0c683 item 0. This changes
reporting, not the three exclusion-owner pairs. The later explicit bounded root-identity
exception below replaces only the earlier cross-copy identity comparison requirement.

Expected configuration is the complete fetched UOM profile with only typed requested elements
changed. Preserve existing values, attributes, wrappers, adapters and repeated-child order;
absence, present-empty, zero and false are distinct. Do not inject an optional field to manufacture
a default or silently discard unfamiliar XML. Unknown namespaces/fields/attributes/cardinality
remain visible differences or a pre-write source refusal, never whitelist equivalence.

Propose family-owned `PartitionProfileUpdateOutcome` in `operations/lpar/profiles.py`, following
`SystemProfileOutcome`'s execution/postcondition separation, without a shared result framework:

- `execution`: completed/rejected/unknown; submitted HTTP result and bounded error detail remain facts.
- `requested_change`: verified/mismatched/unverified; verify EVERY requested typed field after GET
  under the same contained profile UUID, parent/type and unchanged SettingID. HTTP 200 alone,
  partial matching or unavailable/malformed readback cannot earn the applied classification.
- `preservation`: verified/drifted/unverified; compare the complete expected represented UOM tree.
- Before/after opaque ETags, contained identity, and `differences`: family-specific records with
  expanded QName/ordinal paths and added/removed/changed kind; before/after presence, scalar text,
  attributes and child cardinality/order. No generic XML patch or new global outcome enum.
- `comparison_complete` and `details_truncated`: traverse the complete response-bounded tree;
  render at most 100 differences/32 KiB. Truncated diagnostics cannot imply complete comparison.
  Exhausted/incomplete comparison without established drift is unverified; known drift can remain
  drifted with truncated details. Recovery-copy equality requires a complete comparison regardless.

Only completed execution + ALL requested changes verified + observed preservation drift yields
“applied with preservation drift”. CLI/MCP visibly retain those three facts and added-field paths,
state full preservation not verified/reconciliation needed, and promise no rollback or retry.
CLI exits 0 only for completed execution, verified requested change and verified preservation;
1 only for a known rejection with no applied requested change; 2 for applied-with-drift, partial,
unavailable or unknown outcomes requiring reconciliation. Exit 2 does not rewrite completed
execution into unknown transport. Local preflight rejection and known precondition failure differ
from ambiguous post-write HTTP/transport failure; an HTTP error alone proves no absence of effects.
Unavailable requested readback remains unverified, never masquerading as applied or known drift.

For stale pre-write reads, require the same ETag and exact UOM tree/attributes. The fixed sampled
exception is ONLY direct outer Atom entry `published` text; retain the remaining envelope identity,
links/attributes and full raw strings. It is not permission to omit nested Metadata, AtomCreated,
SettingID or configuration fields. After mutation record a fresh opaque ETag; do not require it
to equal the pre-write version. No automatic retry follows 412, timeout or preservation drift.

The private experimental harness at package revision 8720770 observed POST200, requested saved
memory512→768, stable SettingID66 and an ETag transition, with source profile unchanged. Readback
added three CUD zero huge-page counts below, so requested_change=verified and preservation=drifted;
this was not full-preservation success. Both disposable fixtures/profiles were independently
cleaned. The sample is not an implementation/maturity/native-matrix promotion or variant coverage.
Same-target V10R3 SP1060/build2408210051 and fixes matched before/after the outage; header schema
V1_17_0 and root schemaVersion V1_0 are observations, not proof of a version-independent contract.

Retained schema declarations (all minOccurs=0/maxOccurs=1, ExcludeFromETag=false, NO declared
element default) agree with the observed attributes. Seven CUD fields stay intact when present;
ManualEntitledModeEnabled is ROR, not a public patch knob. Omit it only where source-backed create
projection requires; check its derived readback. Echoing a present ROR field in POST remains
unproved by this sparse-profile sample. No field is blindly injected or omission-equated:

| Relative field | Declared type | Behavior | Retained schema/line |
|---|---|---|---|
| IOConfigurationInstance.VirtualOpticonnectPool | xsd:boolean | CUD | LogicalPartitionProfileIOConfiguration.xsd:361 |
| ProcessorAttributes.SharedProcessorConfiguration.UncappedWeight | MemoryWeight.Type | CUD | LogicalPartitionProfileSharedProcessorConfiguration.xsd:420 |
| ProfileMemory.DesiredEntitledMemory | EntitledIOMegabyte.Union | CUD | LogicalPartitionProfileMemoryConfiguration.xsd:219 |
| ProfileMemory.DesiredHugePageCount | xsd:int | CUD | LogicalPartitionProfileMemoryConfiguration.xsd:259 |
| ProfileMemory.ManualEntitledModeEnabled | xsd:boolean | ROR | LogicalPartitionProfileMemoryConfiguration.xsd:419 |
| ProfileMemory.MaximumHugePageCount | xsd:int | CUD | LogicalPartitionProfileMemoryConfiguration.xsd:459 |
| ProfileMemory.MemoryWeight | xsd:int | CUD | LogicalPartitionProfileMemoryConfiguration.xsd:539 |
| ProfileMemory.MinimumHugePageCount | xsd:int | CUD | LogicalPartitionProfileMemoryConfiguration.xsd:579 |

The old postoutage profiles gained all eight; fresh profiles initially had none; POST materialized
only DesiredHugePageCount/MaximumHugePageCount/MinimumHugePageCount=0. Those observations establish
neither omission=zero/false/AUTO nor default semantics. Retained declaration release provenance
and referenced Types/union closure remain incomplete; the private source inventory carries hashes.

## Approved concurrency and recovery boundary; technical closure still required

A profile ETag conditions only that profile's POST. It cannot condition system-profile membership,
partition current/default identity, or a job saving current configuration. The supplied protocol
contract does not establish conditional DELETE. An acknowledgement is not a lock or fencing.

Approved bounded deployment: an operator-coordinated exclusive writer window covering the
selected partition's configuration, its profiles, and system profiles referencing them. Reuse the
accepted `exclusive_writer_window` concept of the H1 spec/ADR 0192; do not create a local lock.
The caller must explicitly assert this window before these new mutations. Immediately before a
mutation, re-read the expected profile ETag and relevant partition/membership snapshot and reject
observed staleness. Conditional profile POST remains mandatory within the window. An independent
writer that violates the window can still race a job or deletion after that check; this is an
explicit unsupported deployment, not a guarantee that the check fences it. The explicit later contract approval settles
this boundary; the earlier child-scope approval alone did not decide it.

Direct SaveCurrentConfig force-overwrite has no documented failure-atomic guarantee. The reviewed
stage-current-to-existing-copy proposal is rejected: the actual schema marks AssignAllResources
create-only (COD), so even a complete mutable-subtree copy cannot establish SaveCurrentConfig
semantics. A CPU/memory-only transfer is not an alternative and will not be implemented.

The approved baseline-preservation boundary preserves the old configuration as a separately verified recovery
profile, then calls the documented native force-save. This is the explicitly approved interpretation
of “preserve the baseline,” not a claim that the existing destination stays unchanged. The exact
clone/equality algorithm remains a technical prerequisite rather than authority supplied by approval:

1. Resolve the existing destination and caller ETag, authorize the same partition, and refuse
   full-system profiles (the mksyscfg save contract prohibits overwriting them). Require overwrite
   consent, the proposed writer window, and the expected profile/partition snapshots.
2. Create a unique `hmcprec-<random-token>` recovery profile under the same partition by a typed
   profile PUT. Source its complete createable configuration from the original full GET. The
   finite root schema set has CUD/CUR configuration plus create-only AssignAllResources. Keep
   ProfileName separate; omit original Metadata/Atom identity, AssociatedPartition, ProfileType
   and SettingID identity/read-only fields. Root fields outside the captured finite schema or
   unknown createability refuse before PUT. Composite fields copy their complete saved content,
   including adapters, rather than translating only the public patch fields.
3. Read the new profile and require complete equality under the approved four-scalar root-identity
   exception below before force-save. Configuration element names, values, attributes, wrappers
   and repeated-child content/order remain exact; schema/behavior annotations are attributes and
   remain exact too. Missing, extra or changed represented configuration refuses; the original
   destination is untouched.
   Read-only fields remain checked for same parent/type as derived identity. No guessed default
   or missing optional field is silently accepted. This is a required postcondition, not an
   assumption that PUT succeeded. Two normalized sparse AIX/Linux copies met this exact comparison
   on commit143bf750; populated-adapter and VIOS recovery-copy variants remain unproved.
   Applied-with-preservation-drift on an in-place patch never admits this force-save gate.
4. Recheck original ETag and current/default/profile-sync snapshots immediately before the native
   SaveCurrentConfig force job. Do not POST staged current configuration. Check job returnCode,
   terminal outcome and destination readback. A timeout, bad result or failed readback reports
   destination outcome uncertain, retains the verified recovery profile and exposes its identity.
   Do not retry, delete recovery material, claim rollback, or claim the old destination unchanged.
5. After a verified successful save, retain the recovery profile and report it for explicit later
   deletion through the protected delete operation. A profile-capacity/create refusal stops before
   force-save and never deletes an existing profile to make room. Its creation/retention is an observable effect,
   bounded to one recovery profile per admitted attempt; no automatic retry creates more profiles.
   The caller must reconcile an uncertain prior attempt before another save. An explicit existing
   recovery-profile selector may be reused only after revalidation of the same baseline/ownership;
   otherwise a fresh invocation may create a new recovery profile and must disclose that effect.

The finite root configuration projection is 17 fields: AffinityGroupID, AssignAllResources,
AutoStart, BootMode, ConnectionMonitoringEnabled, DesiredProcessorCompatibilityMode,
ElectronicErrorReportingEnabled, HostEthernetAdapterLogicalPorts, IOConfigurationInstance,
PowerControlPartitions, ProcessorAttributes, ProfileMemory, RedundantErrorPathReportingEnabled,
SwitchNetworkInterfaceDeviceID, ProfileSRIOVEthernetLogicalPorts, ProfileSRIOVRoCELogicalPorts and
KVMReservedMemory. ProfileName is assigned separately; AssociatedPartition/ProfileType are checked
as same-parent/type read-only state. SettingID is omitted from the create payload because it is
ROO. Its cross-copy scalar value may differ only under the explicit exception below; no
general identity meaning is inferred from the field name.
No Atom entry identity is copied into the created UOM profile resource.
The captured V11R2 SP1120 root additionally declares optional PartitionPlacement:xsd:int,CUD,
ksvV1_19_0. Retain it when present and include it in that release's create projection; do not
inject it on V10 or add a public patch knob. The two-version source comparison,not host CPU
architecture,establishes that difference. A fixed projection must refuse an unrecognized
uncaptured top-level field rather than silently omit it.

One focused source inspection fetched the dedicated/shared profile processor schemas, profile
I/O-slot collection, IBM i tagged-I/O, SR-IOV Ethernet/RoCE and host-Ethernet/channel collections.
Those eight bodies returned 200. The earlier flat Types and ProfileVirtualIOAdapterChoiceCollection
requests returned200empty; those paths did not establish that the shared definitions were absent.
The later correctly located /rest/api/web/schema/inc/Types.xsd returned a nonempty chameleon index.
A separately bounded pass captured ChoiceCollections,Primitives,Patterns,Lists,Unions,Globals and
Enumerations. Private receipts retain request paths,headers,body hashes and unavailable results.
Root createability is established. Nested projection now uses the explicitly approved bounded
annotation contract below; missing transitive primitive definitions do not block unchanged scalar
retention or authorize invented lexical constraints. The comparison policy is the explicitly approved four-scalar
root-identity exception below: every other represented field, attribute and repeated value stays
exact. No missing defaults or behavior-annotation normalization is admitted. Any unexplained
difference refuses before force-save. The approved annotation contract below settles the
projection strategy; its implementation and populated-copy behavior remain unproved.

### Captured virtual-adapter grammar and remaining allocation hold

The original profile and three contained LPAR candidates on its system had no nonempty
ProfileVirtualIOAdapters subtree. The separately bounded additional probe stopped at the first
VIOS candidate: HTTP 200, 45,641 bytes, SHA-256
`6fc3ef3a47096ead93bf3df661e1919d5ff46ef31445a47346579eb6a08dfb19`.
The actual path is IOConfigurationInstance/ProfileVirtualIOAdapters. The populated native sample
used ProfileVirtualIOAdapterSubclass wrappers containing one concrete adapter: ProfileTrunkAdapter
or ProfileVirtualSCSIServerAdapter. This observation does not establish mandatory cardinality.
The served inc/ChoiceCollections.xsd defines0..unbounded wrappers and a0..1 concrete-child choice.
Preserve a present empty wrapper distinctly from absence; reject multiple concrete children.
Its complete declared choice is: ProfileVirtualFibreChannelClientAdapter,
ProfileVirtualFibreChannelServerAdapter,ProfileVirtualSCSIClientAdapter,
ProfileVirtualSCSIServerAdapter,ProfileClientNetworkAdapter,ProfileTrunkAdapter,
ProfileVirtualNICDedicated,ProfileVirtualNICShared. The schema hash is
`cea54860514d7c216142ca93f8457c3752f9c49e1aea0a68f50aabe74293371a`.
The [source checkpoint](https://github.com/randomparity/hmcpctl/issues/1724#issuecomment-6343617903)
qualifies the bounded passes and the corrected private filename selector. No new request is implied.

Five populated schema bodies establish these two concrete classes and their inheritance:
ProfileTrunkAdapter -> ProfileVirtualEthernetAdapter -> ProfileVirtualIOAdapter;
ProfileVirtualSCSIServerAdapter -> ProfileVirtualSCSIAdapter -> ProfileVirtualIOAdapter.
A separately authorized GET now also establishes ProfileVirtualSCSIClientAdapter ->
ProfileVirtualSCSIAdapter: the client subclass adds no fields. Its source hash is
`2dc69d0b9b7a0332c035e1eb68ea0adae7fe3499882b2bbc9a029ad74f68aa62`.
The final shared-base GET was HTTP 200, 16,149 bytes, SHA-256
`fdb6f39f97ec857dde47dd16b0e6e4200914bbdf0104c440beff1b077fdca8e5`.
The source-backed finite field map is:

- Shared base: VirtualSlotNumber (CUR), IsRequired (CUD), AdapterType (CUR), LocalPartitionID (CUR).
- Ethernet base: IsTaggedVLANSupported/TaggedVLANIDs (CUA), PortVLANID (CUR), and CUD fields
  MaCAddressValue, AllowedOperatingSystemMACAddresses, QualityOfServicePriority,
  VirtualSwitchInterfaceManagerID, VirtualSwitchInterfaceType, VirtualSwitchInterfaceTypeVersion,
  IsQualityOfServicePriorityEnabled and VirtualSwitchName. VirtualSwitchID is ROR.
- Trunk additions: TrunkPriority and InterfaceName (CUD).
- SCSI base: RemoteSlotNumber (CUA), RemotePartitionID (CUR), BackingDeviceName and
  RemoteBackingDeviceName (CUD). The server subclass adds no fields.

For these captured classes, clone grammar must retain the actual choice wrapper/cardinality,
source child order and represented configuration. The create projection may omit the ROR
VirtualSwitchID, but baseline equality must still require its reconstructed value to match.
LocalPartitionID, VirtualSlotNumber, RemotePartitionID and RemoteSlotNumber remain semantic values,
not disposable identity metadata. Metadata/Atom is empty at the captured adapter nodes.
Neither arbitrary namespace-insensitive copying nor deleting every field containing “ID” is valid.

### Approved bounded annotation projection — 2026-10-22

Operator decision “Approve bounded annotation projection”,question
call_5531fb6a6ecc45c6b5d32a05b4c08225 item0,authorizes this design reconciliation only. It replaces
complete-transitive-XSD acquisition as the clone prerequisite; it grants no hardware,implementation,
review,merge or maturity promotion. The full five-row170-unit scope and three exclusions stay fixed.

Build the recovery payload only from the authenticated,fresh,contained profile GET. Keep the fixed
captured root field set,the eight-adapter choice grammar and explicit captured structural wrappers.
Preserve cardinality,child order,namespace and present-empty/absent distinctions. Do not accept
arbitrary caller XML or derive permission from a field name. Original root Atom identity is omitted.

- Recognized createable behavior tokens are CUD,CUR,COD,CUA,COR. Retain unchanged lexical values,
  attributes and child order; recurse into composites. Do not normalize booleans,numbers,lists,
  names or identifiers,and do not invent missing primitive facets or materialize defaults.
- Recognized ROR,ROO and UOD fields are omitted with their entire subtree. A createable-looking
  descendant never overrides a noncreateable ancestor. This omission applies only to payload
  construction; full represented content still participates in recovery readback equality.
- Unannotated nodes are allowed only at explicitly captured structural-wrapper/metadata paths.
  Missing behavior on an ordinary field,unknown tokens,conflicting captured annotations or
  unsupported root/adapter shapes refuse before PUT. No permissive fallback,no live schema fetch,
  no general schema compiler,proxy,local store or new dependency is introduced.
- New caller-supplied typed patch values still use their explicit domain/range/mode validation.
  Unchanged HMC scalar content is retained verbatim; it is not exposed as an arbitrary patch API.
- After clone creation,enforce complete recovery equality with only the four approved root scalar
  exceptions. Any missing,added or changed configuration,attribute,namespace,cardinality,order,
  nested identity or unknown field refuses force-save. Keep the recovery profile on uncertainty.

All three retained profile instances annotate every nonmetadata leaf. The unannotated nodes are
root and captured collection/choice wrappers; that sample supports this design,not a guarantee
that every release annotates identically. Tests must cover all eight adapter alternatives and
nested SRIOV/physical-I/O branches,unknown/missing annotations,read-only ancestors,empty wrappers,
exact recovery equality and both partition types. Runtime refusal is a safety guard,not a fourth
exclusion or evidence that an allocated variant has been implemented. Populated-clone live proof
is still absent. No further source or hardware pass is implied.

### Approved bounded root-identity exception — 2026-10-22

The operator selected “Approve bounded root-identity exception”,
call_ce0941250e8c4ef3bf6f6983b2454e7c item0. This explicitly replaces the earlier strict
cross-copy identity rule, without changing the three exclusion-owner pairs. Before an existing
profile overwrite, the verified recovery copy may differ from its source only in scalar text at:

- root ProfileName, which must separately equal the requested unique recovery name;
- root SettingID, which must remain a present valid profile identity value;
- root Metadata/Atom/AtomID and root Metadata/Atom/AtomCreated.

Mask only these four scalar texts after validating their exact expanded-QName paths and
unambiguous scalar shape. Keep their attributes, cardinality, wrappers and ordering in comparison.
Do not remove a whole Metadata subtree, normalize behavior/schema attributes, equate defaults,
ignore unknown fields, or normalize any other identifier. All remaining scalar values, attributes,
configuration, nested IDs, namespaces, wrappers and repeated-child cardinality/order remain exact.
AssociatedPartition must still resolve to the same parent type/UUID and ProfileType must match.
Missing/extra/changed configuration or incomplete comparison refuses before native force-save;
retain both fresh contained identities and ETags. This policy grants no hardware action or review.

The separately authorized experiment at commit143bf750 completed two successive copies of a
normalized sparse AIX/Linux profile. Both full captured pairs differed only in these four scalar
values. An independent offline comparison masked exactly those values and retained all other
attributes, tags, order and scalar text; both comparisons passed. In-place patches retained
SettingID; the second patch matched the submitted whole UOM tree. The first patch materialized
DesiredHugePageCount, MaximumHugePageCount and MinimumHugePageCount=0 and is preservation drift.
All seven effects and independent CLEAN recovery completed in150transport reservations/142.37s.
See [the evidence checkpoint](https://github.com/randomparity/hmcpctl/issues/1724#issuecomment-6342695164)
and [PR1784](https://github.com/randomparity/hmcpctl/pull/1784). This establishes the finite observed
comparison, not general SettingID regeneration semantics, VIOS behavior or populated-adapter proof.
In-place updates continue to require unchanged contained profile identity, including SettingID.

All eight class-specific definitions are now captured. The focused adapter pass returned eleven
nonempty schemas in eleven GETs/fourteen total transports/3.393 seconds,with own logout complete.
It captured the five previously missing classes,their four fixed base/backing schemas,and the
literal referenced ProfileVirtualNICDetails and ProfileVirtualNICBackingDevice definitions.
The source inventory no longer has a missing concrete adapter-class definition. This does not
establish complete nested clone projection or populated-copy behavior.

The added source facts are:

- Fibre Channel client/server extend ProfileVirtualFibreChannelAdapter,which extends the shared
  ProfileVirtualIOAdapter base. Its ConnectingVirtualSlotNumber and ConnectingPartitionID are CUR;
  client WWPNs is CUR/WWPN2.List. The server adds no fields.
- ProfileClientNetworkAdapter extends the captured Ethernet base and adds no fields.
- Dedicated/shared vNIC extend ProfileVirtualIOAdapter. Both require one CUR Details element of
  ProfileVirtualNICDetails.Type. Dedicated also requires one CUR AssociatedBackingDevices choice
  collection; shared has optional CUR AssociatedSwitchID/PositiveInteger.Type.
- Details contains optional CUD PortVLANID,PortVLANIDPriority,AllowedVLANIDs,MACAddress,
  AllowedOperatingSystemMACAddresses,LparName and AutoPriorityFailover fields.
- The backing base contains optional CUR DeviceType,AssociatedVirtualIOServerID and FailOverPriority.
  Its SRIOV subclass adds COR RelatedSRIOVAdapterID/RelatedSRIOVPhysicalPortID and COD
  DesiredCapacityPercentage/MaxCapacityPercentage. CurrentCapacityPercentage and
  DesiredMaxCapacityPercentage are ROR; omit only in create projection,retain in baseline comparison.

The create-branch reference inventory still names unresolved WWPN2.List,PositiveInteger.Type,
MACAddress.Pattern,MACAddress4.List,AllOrNone.Enum,Percent.Pattern,AbstractNonRest.Type and common
attribute groups. No unattempted literal profile candidate remained in that selective inventory;
this is not a complete-validator proof or proof that other endpoints do not serve them. A final three-GET source pass captured ProfileSRIOVVirtualNICBackupDevice,
PhysicalFibreChannelPortCollection and PhysicalFibreChannelPort. All were nonempty; six total
transports/2.518seconds,own logout complete. Backup-device configuration is reached through COD
AssociatedProfileVNICBackupDevice. Physical-port configuration is reached through CUD profile
slot/associated I/O-adapter/physical-port collections; its PhysicalVolumes member is also CUD.
This confirms the branch cannot be dismissed as read-only inventory. It does not require a
recursive capture of every physical-volume type under the approved annotation projection.
Source permission annotations alone do not establish reconstructed read-only equality after clone. Unknown classes/shapes/fields must
not be silently discarded; fail-closed guards do not count as implementation of allocated variants
or a fourth exclusion. The shared Types index and actual choice definition are now available;
some common/abstract definitions remain unavailable at attempted paths. The complete design and allocated coverage still require approval before build;
source acquisition is no longer held for a complete transitive type universe.
Use the approved annotation contract and retained source map to implement nested projection;
allocated variants cannot be silently excluded. No additional transitive schema closure is required. Root-identity comparison is explicitly
decided; further native work still requires a separate concrete authorization.
No further probing is implied by this document.

The complete schema-backed recovery clone remains a candidate protocol, not verified by the sparse
private native sample. A failed clone/comparison leaves the old destination untouched; a failed native overwrite
can leave it changed or unknown while the old saved content remains in the recovery profile.
Restoring that content is a separate operator-owned action: current/default/system-profile
associations are not automatically rewound. The user explicitly approved this distinction. It does
not permit weakening preservation to an ephemeral XML snapshot or skipping clone verification.

### Approved contracts and exact exclusion owners

The later explicit approval covers #1724 only and supersedes the earlier empty additional
exclusion-owner set only for these three pairs:

- Independent writer-window violators — operator coordination. The implementation requires an
  operator-coordinated exclusive writer window, fresh precondition checks and conditional profile
  updates, and makes no fencing promise against independent violators.
- Recovery from verified copy versus unchanged destination after failed force-save — operator-owned
  recovery. The on-HMC recovery profile must be verified and retained before overwrite. A failed
  force-save may leave the original destination changed or uncertain; preserved baseline content
  remains in the verified recovery resource. Automatic restoration is not promised.
- Retention of deletes whenever any system-profile entry exists pending source-backed support —
  operator-managed retention. DELETE fails closed whenever an unfiltered system-profile query
  yields any entry or anything except the canonical empty result; there is no member-pair parser
  or mutation of #1725-owned system profiles.

Approval provenance is the user's “Approve all three contracts”, approval item 0 of
call_6d051ffaf437410caef78c8c140679bb, carried forward under campaign 0eb20b6e4f5c-82a5e9af-4308-49db-9115-9d82028f5e3e and
claim q1724-bf8fb6bd, read back in WORK:SCOPE comment 6334528021. The readback-reporting choice
adds no exclusion. The later complete-design approval authorizes implementation of this exact scope; it supplies
no proof of unobserved variants or new hardware/merge authority.

## Success and verification

For the named typed patch, tests prove meaningful saved fields round-trip and omitted adapters
remain exact in the constructed request and preservation readback; server additions instead produce
known applied/verified-request/drifted-preservation facts. Tests cover every requested field,
partial/missing readback, unknown transport, bounded/truncated QName/attribute/cardinality deltas,
absent versus empty/zero/false, unchanged identities/SettingID, and the sole envelope published
exception. Recovery-copy additions/missing nodes or incomplete comparison MUST prevent force-save.
Malformed records, foreign/ambiguous selectors, stale ETags, absent ETags,
412, denied targets and invalid ranges cause no write. Tests discriminate instance versus parent
identity, LPAR versus VIOS job groups, CLI returnCode failures and timeout/partial outcomes.
Deletion tests cover current/default/last/sync protection, referenced profiles, ambiguous inventory,
and successful removal of an eligible profile. Recovery-save tests inject failure at recovery
creation, equality comparison,
pre-save revalidation, native polling and destination readback; no failure is reported as success,
and verified recovery material remains identifiable. Source-copy loss of a create-only field or
adapter must fail the pre-save comparison. Tests explicitly distinguish preserved recovery content
from an unchanged destination. Nonempty system-profile inventory is a refusal, never an invented
no-membership success. Controlled faults must redden tests.

Map all five assigned rows' complete modes/parameters/source units to supported equivalents
in capability records; both save-current rows belong to #1724 under the approved allocation. Update supported discovery, generated
docs, changelog and packaged maturity. Run focused tests, `just verify`, and pinned
`uv run --no-sync prek run --all-files` before shipping. Local host is Darwin arm64/Python 3.11.15;
CI targets amd64/Python 3.11–3.14. No project architecture declaration is inferred from either.
No further live mutation is authorized. Historical private sample/cleanup is not the feature arm.
A reproducible scenario must specify target prerequisites,
baseline capture, eligible disposable profiles, explicit effects, failure/recovery, and deployed
revision; use the repository preflight, named profiles arm, recovery and evidence generator.
Not-run/skip/failure are not passing observations; retain the applicable native obligations.

## Failure model

1. Actors/deployments: authenticated CLI/MCP operators; offline tests/CI; separately authorized
   native operators; HMC concurrent configuration writers inside the explicitly coordinated window.
2. Invariants/assets: saved profiles and contained identities; current/default/required profiles;
   system-profile memberships; baseline saved content and its explicitly approved identity/recovery
   contract; authorization/audit and honest results.
3. Accepted classes: feature native proof remains not-run; the private experimental sample is
   qualified above. Known execution with verified patch and observed preservation drift is reported
   as applied with drift, never unknown transport/clean success. Runtime refusal
   on missing source fields/ETag/capability is safer than a guessed write. Independent writers
   violating the approved exclusive window remain operator-coordinated exclusions, as approved above.
4. Covered elsewhere: system-profile membership changes #1725; backup artifact management #1726;
   broader configuration fields #638/#698; native environment evidence follows the existing catalog.

## Threat model

Added boundaries: typed profile mutations and untrusted profile/membership XML. Widened boundaries:
partition/VIOS ownership authorization, existing client/job transport and CLI/MCP presentation.
Authenticated callers control selectors, patches, expected ETags and consent; the HMC controls
resource documents and outcomes. Validate selectors and strict parent containment; reuse existing
ownership/audit and destination UUID guards; schema-backed fixed field paths prevent arbitrary
XML/query/command writes; preserve bounded response limits and reject malformed/duplicate resources.
Recovery captures can contain private identifiers and must be private, bounded and never posted.
No new secret handling or privilege bypass. External-writer fencing and unapproved live effects
are outside the approved deployment boundary. Approval of the complete reconciled design still precedes build.
