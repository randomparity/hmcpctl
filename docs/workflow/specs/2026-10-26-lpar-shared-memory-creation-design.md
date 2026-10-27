# Shared-memory partition creation — C07 / #1768

## Authority and readiness

[WORK:SCOPE](https://github.com/randomparity/hmcpctl/issues/1768#issuecomment-6438609128)
owns q1768-eb31a44e: eight fields, sixteen P10/P11 creation occurrences, empty
exclusions, full-spec, fixed M250. Base: 056cdc8006eb04323323896d126ec6f4a9929cb0.
[ADR 0299](../../adr/0299-lpar-shared-memory-creation.md) remains proposed.
The operator approved saved-configuration AME and the #1768-only compact-plan format
on 2026-10-27: exact contracts/interfaces, algorithms and test/rollback inventories
replace embedded implementation bodies. This unreviewed set grants no implementation
or hardware access. Root reserved ST65 / creation-memory on 2026-10-27; recheck collisions
before implementation/merge. Normal design/audit and later gates remain; earlier source
reviews are not product reviews.

Completion criteria C1–C5 are, respectively: exact fields/bindings; source-backed
domains/admission; containment/authorization/one writer/honest outcomes; direct
tests/presentations/docs/maturity/live procedure; full design and shipping gates.
Pool lifecycle #642, profile CRUD #1724 and quantity changes #1774 keep their owners;
this allocation introduces no provisioning inputs or stable-facade exports.

## Source contracts

P10 `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41` and P11 counterpart
`:49` place the eight fields below in partition/profile grammar. The sixteen
bindings are exactly `{p10,p11}:mksyscfg:partition-create:<field>` for that table.
They are occurrences within existing shared ledger rows, not sixteen new rows.

| Field | Strict domain, omission and meaning |
|---|---|
| mem_mode | Literal `ded` or `shared`; None omits, without inference |
| desired_io_entitled_mem | Positive integer MiB or literal `auto`; None omits |
| mem_weight | Integer 0–255; explicit zero survives |
| primary_paging_vios_name | Nonempty bounded native name, resolved exactly |
| primary_paging_vios_id | Positive integer PartitionID, not UUID |
| secondary_paging_vios_name | Nonempty bounded native name, resolved exactly |
| secondary_paging_vios_id | Positive integer PartitionID or literal `none` |
| mem_expansion | Finite exact numeric zero or inclusive 1–10; None omits |

The operator requires explicit `mem_mode=shared` for the six entitlement, weight
and paging-selector fields. A name and ID for the same role conflict even if they
would resolve equally. `secondary_paging_vios_id=none` disables that designation;
omission leaves HMC selection unchanged. No `none` sentinel is added to primary ID.
Bool is not numeric; domain/MCP construction does not coerce numeric strings.
The served validation schema advertises numeric input only, matching that refusal.
CLI parses integer/decimal strings before construction. Decimal comparison avoids
binary rounding; do not invent a two-place precision rule from `1.00–10.00` notation.
Reject nonfinite values and bound canonical attribute bytes at 65536 before expansion;
an integer's decimal conversion is bounded at 4096 digits. These are safety limits.

Retained schema bodies were rehashed while preparing this design:

| Schema | SHA256 | Relevant facts |
|---|---|---|
| LogicalPartitionProfileMemoryConfiguration.xsd | 8b14f694a076a272b7edded47e3a908af5c4fb89670b892562113728f2379808 | Optional saved mode, entitlement, weight, AME and designated provider links |
| LogicalPartitionMemoryConfiguration.xsd | 3043acbf8820d68b21e94044a26ee9195d31243bc26ff7e10a95c70bd5c35b32 | Optional current mode/scalars and one current paging provider |
| SharedMemoryPool.xsd | 2f95cc34331fec5355e2f569d5e316f82240b753dbf68537157979e3d06b750e | Pool/provider/device observations, not reservations |
| Unions.xsd | 7e9c995b467fa3268108a965771cde2187e2007b90929058b844b2cbc5898a9b | Positive integral I/O entitlement or auto |

The retained source checkpoint cites IBM's POWER8 memory-weight documentation for
0–255 and POWER7 AME documentation for shared/dedicated coexistence. The retained
POWER9 requirements establish shared processors, virtual I/O, dedicated-memory
paging providers and allocation at activation. Its VIOS version floor and IBM i
paging surcharge are generation-specific evidence, not universal current guards.
No pool-positive feature result was captured by the schema windows.

## Approved saved-configuration AME contract

Local `docs/refs/intro-to-powervm-redbook/02-ibm-powervm-features-in-details.md:230–246`
describes AIX runtime and excludes Linux/IBM i runtime. Chapter 3 `:450–478`
separates physical profile memory, expansion factor and OS-managed compression.
The common native grammar has no adjacent AIX-only qualifier on `mem_expansion`.
The separate source experiment at 7c96115b observed fresh inactive VIOS saved AME
disabled at 0 and enabled with numeric factor 1.1 at 1.10. This proves neither
VIOS activation/compression nor arbitrary release applicability. Its held original
capture remains partial; later independent cleanup/baseline confirmation is separate.

The operator approved this contract on 2026-10-27:

- Expose source-valid saved AME values on LPAR creation, including IBM i, and VIOS
  creation. Positive values require selected-system
  `AssociatedSystemCapabilities.ActiveMemoryExpansionCapable` to be positively observed.
- Selecting `mem_expansion` does not declare AIX intent. `OS400` or a selected
  Linux-intent field does not itself invalidate the stored AME request; preserve
  all other platform, capability and mixed-family admission checks.
- Verify saved/current configured facts separately. Saved equality proves neither
  guest compression nor activation. Published AIX-only runtime guidance remains explicit.
- VIOS shared-memory creation still requires AMS capability/pool/provider admission;
  dedicated VIOS saved acceptance does not prove shared VIOS runtime or activation.
- Zero stays independent of AIX intent and requires no capability enablement. The
  six shared-only fields still require explicit `mem_mode=shared`.

`hardware_mem_expansion` remains the separate C10 access boolean with its existing
classic-mode/capability/readback guard. The approval chooses this AME policy only;
complete design/audit and implementation gates remain required.

## Ownership and caller map

Add immutable `documents/lpar_creation_memory.py:LparCreationMemory` with the eight
optional fields, `pairs()` and pure cross-field validation. Add
`LparCreation.creation_memory` (empty default) and optional keyword `creation_memory`
to `operations.vios.core.create_vios`; keep `LparResources` update semantics unchanged.
MCP `hmc_create_lpar` and `hmc_create_vios` accept the typed optional family. Their
CLI commands expose eight flat hyphenated flags, constructing the same model before
opening clients. No duplicated presentation-only admission replaces domain checks.

`operations/lpar/creation_memory.py` owns creation-specific capability, pool/provider
admission and saved/current verification; existing `operations/lpar/memory.py` stays
the DLPAR quantity owner. `ssh/lpar.py:create_lpar_via_cli` remains the sole writer:
append fixed typed pairs to the same attribute record as the landed creation families.
Reuse `read_partition_profile(..., partition_type=LogicalPartition|VirtualIOServer)`
for contained classic saved reads and existing UOM resource readers for current facts.
No profile CRUD setter, alternate REST create path or generic command proxy is added.

The LPAR create workflow carries one `creation_memory` WorkflowStep through
`LparCreationResult.creation_memory_step`; its result retains separate saved/current
observations. VIOS retains `dict[str, Any] | None`: after every requested check passes,
merge `creation_memory_verification` into the entry alongside existing `serial_verification`.
Neither requested family may early-return over the other; omitted C07 adds no member.
Unverified VIOS creation raises the existing safe partial-result HMCError rather than
returning an entry resembling complete success. Existing VIOS creation does not stamp
ownership or apply profiles; this change does not invent either action.
`provision_lpar` continues constructing empty C07 inputs; omission is regression-tested.

## Admission and one submission

Validate type/domain/selectors/keylock compatibility before network access. Preserve
existing duplicate-name, update-only weight, security, role, hardware and layout guards.
Resolve the selected system UUID/native name and classify classic/controller mode with
the existing role reader. C07 requires native creation; reject explicit keylock there.
Controller mode omits `profile_name`, refuses named apply and initial-profile I/O under
the existing rules. C10 combinations still require its observed classic tuple.

Explicit shared mode requires positive `ActiveMemorySharingCapable`, known completed
shared-CPU resources, and no direct physical slots or direct SR-IOV in either initial
declarations or later assignments. Reject unknown `all_resources` prerequisites,
without silently rewriting CPUs. Preserve C06 advertised HPT/PPT checks; do not infer
a blanket vNIC, Virtual OptiConnect, or zero-huge-page prohibition.
LPAR and VIOS dedicated/omitted mode retain existing physical memory bounds. Shared
logical memory bypasses only the create owners' physical desired-memory comparisons
(both supplied/completed LPAR checks and the VIOS check), not the global bound helper.

For explicit shared mode, require one identified selected-system SharedMemoryPool and
a complete bounded VIOS inventory. Resolve selectors uniquely by exact name or numeric
PartitionID, then confirm canonical UUID/name/ID/type in the selected-system detail.
Use the same fixed routes as list_child/list_vios/get_uom, reading raw evidence with
HMCClient._get so continuation links, duplicate entries and byte limits remain inspectable.
Require a complete Atom feed, no next-page link and unique identities before parse_feed;
a selected detail must contain exactly one entry. No continuation URL is followed.
Pool `PagingServicePartitionOne/Two` links define eligible providers; require dedicated
provider memory. Reject missing, duplicate, foreign, wrong-kind or ambiguous evidence.
Validate returned links against configured HTTPS host/port and exact root/selected-system
VIOS paths; disallow credentials, queries/fragments and foreign origins. Construct fixed
detail requests from validated identities; never fetch returned arbitrary URLs.
Bound inventories at the profile owner's existing 5 MiB evidence limit and provider
count at two; a truncated or incomplete response cannot establish a candidate set.

Authorize selected providers, or the complete eligible candidates HMC can choose when
a selector is omitted, through `authorize_lpar_ownership_description` on fresh contained
details. Preserve unstamped-resource acceptance, foreign/malformed-token denial and audit;
no ownership override is added. Explicit no-secondary does not remove primary checks.
Reject final primary/secondary identity equality, including aliases across selector forms.
Both MCP create handlers already use `exhaustive_targets=False` at the captured base.
Preserve that affected-target policy for nested paging providers and serial peers;
a managed-system target-table grant alone cannot stand in for AllTargets authority.

Pool existence and eligible topology are creation checks. Free devices, active providers,
physical pool allocation and guest readiness belong to activation; do not impose them
as speculative profile-save requirements. Do not add entitlement <= desired-memory or
DLPAR incremental-capacity comparisons. A stale capacity snapshot is no reservation.

Issue one native create. C07 submission failure uses a sanitized HMCCLIError subclass
with optional `remote_refusal_code`; preserve that code through mixed C06/C11 catches.
Perform one selected-system name/ID reconciliation, with no stamp: observed identity means
partial; explicit refusal plus confirmed absence means rejected; other outcomes are unknown.
Expose only those classifications/code in safe HMCError text, never raw transport bodies. No retry,
fallback create, uncertain ownership stamp, automatic deletion or second writer follows.

## Postconditions and barriers

| Observation | Required interpretation |
|---|---|
| Identity | UUID/name/partition kind, selected-system containment and inactive state agree |
| Saved mode | ProfileMemory.ActiveMemorySharingEnabled equals explicit requested mode; otherwise observe without inventing a request |
| Saved entitlement | ManualEntitledModeEnabled and DesiredEntitledMemory distinguish auto from numeric request |
| Saved weight | MemoryWeight numerically equals requested zero or positive integer |
| Saved AME | ActiveMemoryExpansionEnabled false for zero; true and numeric ExpansionFactor equality for positive request |
| Saved providers | Primary/SecondaryPagingServicePartition resolve to admitted authorized identities; present empty secondary differs from absent field |
| Current configured values | SharedMemoryEnabled, AutoEntitledMemoryEnabled/CurrentEntitledMemory, CurrentMemoryWeight, MemoryExpansionEnabled/CurrentExpansionFactor |
| Current paging | CurrentPagingServicePartition is a separate optional runtime observation, never proof of the designated pair |

Classic creation verifies the contained `default_profile` independently before optional
apply. Omitted provider selections still require saved identities in the authorized
candidate set; no implicit primary is inserted into the request. Missing optional fields
are unavailable, not false/zero. Preserve partial observations alongside unavailable names.
For `mem_expansion=0`, a stale unused factor does not invalidate proven disabled state.

At the landed core, C07 joins the native-route, canonical-system-name, controller,
missing-identity and deferred-apply predicates. It must suppress `_create_via_cli`
early apply, then join the final core required-step list and workflow assignment barrier.
The complete requested-step set is settings/roles, reporting, vTPM, physical I/O, security
policy, hardware memory, memory layout, serial and C07; every selected step must exist
and have status `ok`, with confirmed LPAR ownership. Existing C11 peer admission and
`confirm_serial_created` remain additional checks; C07 cannot replace or bypass them.
LPAR ownership plus requested saved family checks gate the existing single optional apply.
After acknowledged apply, independently compare source-supported current requested scalar
facts and recheck identity; failure stops assignments. An inactive current paging provider
may be absent without falsifying the saved designated pair. Without apply, saved proof
does not claim current configuration, activation or guest compression.
VIOS has no automatic apply: return only verified saved configuration and label it so.
Controller creation has no named-profile read; compare supported current facts, retain
unavailable designated evidence and report incomplete shared verification. Missing facts
cannot be replaced by successful submission or an observed single current provider.
Unknown/mismatched identity, stamp failure, missing/mismatched requested family or apply
failure preserves the resource and stops later effects. Missing or skipped apply after
an explicit classic `apply_profile=True` also stops assignments. Sanitized structured statuses,
requested values, observations and unavailable names are bounded to this eight-field set.

## Failure model

1. **Actors and deployments**
   - CLI operators, direct Python callers and authenticated MCP clients creating inactive
     AIX/Linux, IBM i or VIOS partitions on classic/controller HMC; saved AME only.
2. **Invariants and assets at stake**
   - Containment, affected-provider authorization, persistent configuration, one submission,
     existing family admission, honest outcome/state evidence and stopped unsafe follow-ups.
3. **Accepted failure classes**
   - Concurrent external changes can invalidate observations; HMC remains final admission
     authority. No reservation, transaction or guest-runtime success is promised.
   - Unavailable postconditions produce incomplete outcomes; resources remain for inspection.
4. **Covered elsewhere**
   - Pool lifecycle/removal #642; profile CRUD #1724; quantities #1774; switchover #1776;
     transport trust/access-policy/audit owners; guest installation and activation owners.

## Threat model

1. **Boundary inventory:** widen caller-to-native-record, HMC-to-admission/readback and
   affected-provider authorization boundaries. No new transport or credential boundary.
2. **Actor model:** malformed/unauthorized authenticated callers and concurrent HMC writers.
   The configured HMC is trusted for evidence; embedded destinations remain untrusted.
3. **Controls:** strict bounded input, fixed keys plus record/shell encoding, complete unique
   contained identities, fixed-path reads, ownership/audit and non-exhaustive MCP target policy,
   independent verification and no uncertain replay. Error text excludes raw provider payloads.
4. **Outside this model:** compromised trusted host/HMC and guest correctness, covered by
   deployment trust and guest operations; this accepts no omitted C1–C5 creation obligation.

## Success, evaluation and live evidence

The implementation test inventory in the plan maps C1–C5 to explicit behavioral cases:
sixteen bindings; strict domains; source identity/capability/capacity negatives; both direct
owners and four presentations; mixed families; refusal/uncertainty; saved/current barriers.
Fault-test new high-risk containment/authorization/replay guards; restore each fault.
AI-SPEC: an authenticated tool-using client requests inactive creation through either MCP
create tool using the typed memory family. Output is bounded configured/partial evidence;
allowed sources are the selected HMC's contained profile/resource observations. Invented
runtime success, inferred mode and unauthorized provider selection are forbidden. Invalid
input refuses before effects; missing postconditions stop follow-ups. No model call/cost is
added; command count is one create with no retry and bounded prerequisite/readback requests.
The success signal is deterministic equality/containment and honest unavailable outcomes.

The tool-safety/schema/faithfulness evaluation is executable in T1–T3, not an LLM judge:

| Case | Setup/input; observable pass and forbidden trait | Severity/gate |
|---|---|---|
| E1 | Both handlers, valid shared fixture: saved evidence agrees; no runtime claim | 4/block |
| E2 | Shared-only field without mode: pre-effect refusal; no inferred shared mode | 4/block |
| E3 | Injection-shaped name or request to bypass checks: refusal; no extra command | 5/block |
| E4 | Stale/foreign profile or current mismatch: incomplete; no apply/assignment success | 4/block |
| E5 | Foreign/denied explicit or default provider: denial; no create or raw private payload | 5/block |
| E6 | Oversized inventory or create timeout: bounded failure/one submission; no retry loop | 5/block |
| E7 | VIOS AME0/1.10 source-shaped saved observations: configured equality; no activation/compression claim | 4/block |

Provide a named opt-in inactive-creation procedure through existing preflight, registered
arm wrapper, mandatory recovery and filtered evidence. Separate positive AMS, redundant
designation, AME stored VIOS and controller observations. Use an existing pool; no pool
changes, guest activation, VIOS service mutation or implicit deletion permission.
The procedure requires fresh-name absence, matching deployed candidate, exact fresh UUID
cleanup authority and baseline equality. Missing hardware prerequisite records skipped,
not success. No source-only capture or unrun arm promotes live maturity.

## Global Constraints

Python 3.11–3.14; declared CI amd64; observed design host arm64.
No new dependencies, toolchain floors, facade exports or provisioning inputs.
Bootstrap only with just setup; Python tools use uv run --no-sync.
Plans are ignored/transient and never committed. Root owns final full verification.
Before push: just verify and uv run --no-sync prek run --all-files, then required CI.
No live operation without a separate root hardware grant and docs/live-testing.md workflow.
