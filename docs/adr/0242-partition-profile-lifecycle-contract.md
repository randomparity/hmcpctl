# ADR 0242: Partition-profile lifecycle contract

## Status

Accepted (2026-10-18), reconciled and approved for implementation on 2026-10-22 in
scope6344262032. The complete [design](../workflow/specs/2026-10-18-partition-profile-lifecycle-design.md)
retains the five-row allocation and three exclusion/owner pairs. Further review and
scope-audit passes were explicitly waived; tests and shipping guards remain required.
The user approved the three explicit contract boundaries in approval item 0 of
call_6d051ffaf437410caef78c8c140679bb; that authority is not technical feasibility proof.
Reserved by the campaign for #1724 before creation; reservation unchanged under design-only
claim q1724-bf8fb6bd and WORK:SCOPE comment 6334528021.

## Context

Saved profiles, running configuration and persistent profile synchronization are distinct
(ADR 0201). Profile updates must preserve omitted adapters and reject stale input. Native
request-header documentation establishes conditional POST, but not conditional DELETE; profile
membership/current/default checks span more than one resource. Native force-save documentation
does not establish failure-atomic overwrite.

## Decision

Propose typed domain operations, contained LPAR/VIOS selectors, full-profile read-modify-write
with required ETag/If-Match for updates, and source-grounded conservative deletion eligibility.
Saved current configuration is written by its documented native operation, not by translating
it into typed CPU/memory patches or copying mutable fields from a stage. The actual schema's
create-only AssignAllResources refutes that earlier overwrite transformation.

For typed in-place updates, the operator selected “Report applied with preservation drift and list
added fields; retain strict recovery-copy equality (Recommended)” on 2026-10-22,
call_dc8a4f14459c4fe88003274d4ab0c683 item 0. Propose family-owned outcome facts: execution
completed/rejected/unknown, requested_change verified/mismatched/unverified and preservation
verified/drifted/unverified, with bounded complete/truncated structural-diff diagnostics.
Known successful execution AND every typed requested field verified under unchanged contained
UUID/parent/type/SettingID permits “applied with preservation drift” when readback has observed
configuration differences. HTTP 200 alone, partial request matching or unavailable readback does
not. CLI/MCP retain the three facts visibly: exit 0 only for completed/verified/verified; exit 1
only for known rejection without applied requested change; exit 2 for applied-with-drift or other
partial/unavailable/unknown outcomes requiring reconciliation. Exit 2 does not recast completed
execution as unknown transport. Preflight/known precondition rejection differs from ambiguous
post-write HTTP/transport failure. No automatic retry or rollback.

Expected configuration is the complete fetched UOM profile with only typed changes. Preserve all
existing represented fields/attributes/wrappers/order; absent, empty, zero and false differ. The
seven observed CUD optional fields are retained when present, never injected as defaults;
ManualEntitledModeEnabled is ROR, not a writable knob. The spec records exact types/source lines.
Only the sampled direct outer Atom entry published text may differ for a stable-ETag pre-write
read; UOM configuration/attributes and remaining envelope identity stay exact. Nested Metadata,
SettingID and semantic IDs receive no broader normalization for in-place updates. The later
explicit cross-copy exception below is separate. Truncated diagnostics or incomplete
comparison never establish preservation; recovery comparison exhaustion refuses before force-save.

For existing-profile save, propose first cloning the original saved configuration into a unique
same-partition recovery profile, verifying complete represented configuration equality, then
invoking native force-save with explicit overwrite consent. A failed/uncertain overwrite retains
the recovery profile and reports the destination uncertain. Preserved recoverable content is
expressly different from an unchanged destination and was explicitly approved by the user.
No new local persistence store or generic XML patch endpoint is proposed.

Source captures establish trunk,SCSI-server and SCSI-client fields and their shared bases. The
served inc/ChoiceCollections.xsd defines eight concrete profile adapter alternatives,0..unbounded
wrappers and0..1 concrete children; a populated single-child sample is not mandatory cardinality.
Preserve empty-wrapper versus absent distinctions. The focused eleven-GET source pass now
captures all eight concrete classes and their virtual-adapter backing/detail branches. Fibre
Channel partition/slot references and WWPNs are configuration; client-network and Fibre Channel
server subclasses add no fields. Dedicated/shared vNIC require Details,and dedicated requires a
backing-device choice collection. Some common primitive/list/pattern/attribute-group and abstract
base definitions remain unresolved; no additional literal candidate remained in the selected
create branch. This is source coverage,not populated-adapter clone proof. Semantic partition/slot/remote/switch identifiers stay exact. Refusing unknown
classes is a runtime safety guard,not a fourth exclusion or proof of complete allocated support. The root
SettingID is ROO/string and is not the profile UUID. Sparse clones received distinct values63/64
and65/66; in-place POST retained66. General regeneration semantics remain unestablished. On 2026-10-22 the operator explicitly
approved the bounded root-identity exception (call_ce0941250e8c4ef3bf6f6983b2454e7c item0),
replacing the earlier strict cross-copy identity rule. A verified recovery copy may differ only
in root ProfileName, root SettingID and root Metadata/Atom/AtomID and AtomCreated scalar texts.
Validate their exact paths/scalar shapes and the requested recovery name separately. All
attributes, cardinality, order, namespaces, configuration, nested IDs and unknown fields remain
exact; never omit an entire Metadata subtree or normalize behavior/schema annotations. In-place
updates still require unchanged profile identity. No new hardware run, review or merge is granted.

Two successive normalized sparse copies at commit143bf750 met that exact four-scalar comparison;
all seven experiment effects and independent CLEAN recovery completed in150transport reservations
and142.37seconds. The first patch added three zero huge-page fields; the second patch matched its
submitted full UOM tree. [PR1784](https://github.com/randomparity/hmcpctl/pull/1784) carries the
filtered evidence. At that checkpoint,nested createability and full five-row/170-unit variant coverage remained
unresolved; the later annotation decision settles projection strategy,not implementation proof. The M250 denominator and three approved exclusion-owner pairs are unchanged.
Patch-reporting and root-identity approvals add no exclusion.

A coordinated exclusive writer window plus immediate snapshot checks and conditional POSTs does
not fence independent HMC actors. Conditional DELETE is not established by the protocol sources.
The only observed system-profile inventory shape is the canonical empty sentinel on 16 systems;
nonempty inventories must refuse until a membership contract is established. The exact three
approved contract/owner pairs are in the design and latest complete WORK:SCOPE. This record
remains Proposed until the implementation-ready clone/equality contract and scope audit close.

## Consequences

No silent fallback to an unconditional whole-profile POST. No automatic deletion cascade into
system profiles owned by #1725. A save can require reconciliation and leave a retained recovery
profile; its identity and baseline content must survive that interruption. The private experimental
harness at package8720770 observed a memory patch and three additional
CUD zero huge-page fields, with all disposable resources later independently CLEAN. That is sampled
behavior, not implemented feature maturity or verified recovery-copy equivalence. No new native
action is authorized by this record; the later complete implementation approval is recorded below.

## Considered & rejected

- **SSH-only typed updates with a prior read.** verified: P10/P11 request-header topics document
  If-Match on PUT/POST, and existing `client_lpars.update_logical_partition` already uses it;
  a pre-write read alone cannot reject an intervening write.
- **Treat an acknowledgement as a lock.** judgment: the proposed window is an operational
  prerequisite, not enforceable fencing against independent HMC actors.
- **Direct force-save as failure-atomic.** verified: the supplied SaveCurrentConfig and mksyscfg
  reference contracts define overwrite consent but contain no failure-atomic guarantee.
- **New generic profile XML/attribute endpoint or local locking service.** judgment: unnecessary
  public surface and coordination machinery for this bounded lifecycle.

## Bounded projection decision — 2026-10-22

The operator approved bounded annotation projection (call_5531fb6a6ecc45c6b5d32a05b4c08225 item0),
replacing the complete-transitive-XSD prerequisite. Keep the captured root/eight-adapter grammar
and explicit structural wrappers. From a fresh authenticated contained GET,retain unchanged
CUD/CUR/COD/CUA/COR content,omit entire ROR/ROO/UOD subtrees,and refuse missing/unknown/conflicting
behavior annotations outside the explicit wrappers/metadata. Read-only ancestors dominate.
Caller-supplied typed values keep explicit validation; unchanged scalars keep their lexical form.
No generic XML proxy,runtime schema acquisition,compiler,dependency or new exclusion is added.
Complete clone readback equality still permits only the four approved root scalar differences.
Three retained instances have behavior annotations on every nonmetadata leaf; this is sampled
source evidence,not universal runtime proof. Full allocated variant coverage must still be tested.
The three-GET final branch pass captured SRIOV backup-device and physical Fibre Channel port
schemas,including a createable PhysicalVolumes field. Deeper XSD acquisition is not required by
this decision. This approval covers design reconciliation only; complete design/build approval,
separate hardware authorization and existing maturity rules remain in force. No review was run.

The later operator “approve” accepted the complete reconciled design for implementation,
without additional review or scope-audit passes (scope6344262032). Tests and shipping checks
remain required; hardware effects,merge and maturity promotion remain separately held.
