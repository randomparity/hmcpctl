# Conventional affinity group at partition creation

Authority consistency amendment for #1769; root D3/A3 and implementation release pending.
[Scope](https://github.com/randomparity/hmcpctl/issues/1769#issuecomment-6399637239),
token q1769-1c63e8d1: full-spec, S/100, iterating, one PR, empty exclusions.
Decision: [ADR 0297](../../adr/0297-lpar-creation-affinity-group.md).

## Problem and bounded success

Frozen C1–C5 require typed creation/CLI/MCP coverage, source-backed semantics,
containment/authorization/audit and honest readback, tests/docs/maturity/live procedure,
and completed full-spec/shipping guardrails. The field has two creation occurrences.

## Source and ownership

- P10 mksyscfg:41 and P11 mksyscfg:49 under docs/refs admit none or IDs 1–255
  for memory/processor grouping, without field-specific Linux, CPU or release floors.
- P10/P11 lssyscfg:34–35 document partition/profile filters and projections;
  neither names affinity_group_id, which does not establish lack of support.
- tests/fixtures/live/cli-prof-lp3.json:2–6 proves exact selected profile none.
- Vocabulary fixtures v10r3-p10-9080-hex.json:2969,3097 and
  v11r2-p11-9242-21b.json:4257,4393 prove lpar/prof field availability only.
  Normalized text does not prove positive-ID/controller/current behavior.
- Existing core/workflows/VIOS/SSH owners retain creation, stamping, result shape
  and the fixed record. The plan maps callers. Extract only C05 linux_roles.py's
  authority observation; preserve C05 (ADR0293) and C10 (ADR0294) contracts.

## Inputs and creation

Direct operations/MCP accept strict int 1–255, exact lowercase none, or omission.
Before I/O reject bool, float, numeric string, 0, 256, negatives, empty/whitespace/
case variants, containers and record delimiters. CLI --affinity-group-id explicitly
parses none or ASCII decimals in range before client acquisition. Preserve wrappers.
Append the optional keyword to LparCreation, create_vios and the native encoder;
add no shared-update field, XML mapping, default group or stable-facade export.

Selection forces one native creation record. Validate native keylock incompatibility
before I/O; retain selected-system containment, duplicate checks and resource intent.
One owner-acquired native authority decision governs admitted role/layout/affinity
creation; reuse selected role evidence without importing Linux/KVM/CPU/release floors
for layout/affinity-only requests.
For role/layout/affinity creation with dedicated/SR-IOV follow-ups, workflow passes
its complete assignment collection as optional internal preflight_assignments data;
it removes its duplicate authority read and defers collection prevalidation to core.
Core rejects a non-collection type or unsupported use before I/O: empty/vNIC-only
collections or collections without a selected role/layout/affinity field are unsupported.
None preserves direct/default/provision behavior; ordinary and vNIC-only workflows
retain their existing prevalidation path. This amendment adds no public CLI/MCP,
creation-input or result contract.
Core's observed controller decision refuses profile assignments before foreign PCIe/
SR-IOV/VIOS inventory or creation effects; admitted classic requests then prevalidate
that complete collection. Physical-I/O normalization preserves it before SR-IOV
follow-ups are removed. No caller supplies controller/capability permission evidence.
Classic grammar uses the default profile; controller grammar omits profile_name.
Controller explicit apply and initial-profile I/O fail before submission. The chosen
observation governs grammar; it does not atomically fence later controller changes.
Affinity omission preserves existing routing and causes no extra affinity/authority reads.
Keep all_resources where independently selected feature contracts permit it.
C10 capability/readback and other selected-domain restrictions remain independently binding.
No REST-first attempt, setter, automatic retry, rollback or activation is introduced.

## Readback and effect ordering

Independently confirm contained UUID, name, numeric PartitionID and environment.
Read lssyscfg -r lpar with lpar_ids filter, fixed
-F lpar_id,name,lpar_env,affinity_group_id --header, using build_filter/shlex.quote.
parse_hmc_result_rows must return one exact header/row whose ID/name/environment
match the contained resource. Label this native-partition; compare only literal
none or canonical decimal 1–255 with the request. Documented grammar plus captured
field availability supports the attempt, not a current/hypervisor-runtime claim.

Classic grammar separately reads lssyscfg -r prof with lpar_ids/profile_names
filters and -F lpar_id,lpar_name,name,lpar_env,affinity_group_id --header.
Require the exact selected default-profile identity and request equality, label
native-profile, and do not substitute it for missing native-partition evidence.
Controller grammar issues no profile query. No XML-zero/profile-current inference.
Unsupported/missing/malformed reply, extra row, wrong identity or invalid rendering
means readback-unavailable; a well-formed unequal assignment means mismatch.

LPAR adds affinity_group_step before apply/assignments, carrying verified/mismatch/
readback-unavailable, requested value and labelled observations. Invalid identity
avoids affinity reads. Either unverified classic observation blocks follow-up effects;
controller requires its partition observation. Preserve resource_created/ownership
truth and strict-stamp exceptions; incomplete workflows skip apply/assignments.
Admitted explicit apply stays in its existing owner after ownership and selected-
domain barriers. VIOS returns its existing entry on verification or raises its
uncertain-resource HMCError. Bound failure text; retain no raw provider payload.

## Failure model

- Actors/deployments: authenticated Python/CLI/MCP callers on supported Python
  hosts against selected P10/P11 HMC contexts; no guest execution.
- Assets/invariants: contained target, single create, ownership, native record
  integrity, honest observations and ordered follow-up barriers.
- Accepted classes: HMC/transport races may leave created resources; report partial
  outcome and require inspection. Unrun native behavior is unevidenced under C4.
- Covered elsewhere: #639 ResourceGroup, #1724 profile lifecycle, minimum-affinity
  owners, C05 Linux/KVM admission and C10 hardware access capability/readback.

## Threat model

- Added boundaries: none. Widened: typed/CLI/MCP create input, native record, readback.
- Actors: caller values/HMC replies are untrusted; existing peer trust retains its owner.
- Controls: strict scalar and internal request type/context validation; core-owned
  authority, existing policy/containment/ownership, fixed-key build_attribute_record/
  shlex.quote, strict rows, existing SSH timeout and private diagnostics.
- Covered threats: transport authentication/audit; no management takeover or guest security.

## Global Constraints

Python >=3.11; CI amd64 Ubuntu 24.04 on Python 3.11, 3.12, 3.13 and 3.14.
No new dependencies or version floors. Use just setup; uv run always uses --no-sync.
No stable-facade expansion, generic native proxy, profile update or affinity setter.
Preserve C05 and C10 contracts, existing authorization, ownership and audit.
No live execution without a separate named hardware lease and matching deployed SHA.

## Validation and operational proof

The plan inventories C1–C5 focused tests/controlled faults and metadata regeneration.
Root reserves ST57, group lpar-affinity-group, scenario st57-lpar-affinity-group-creation,
and scripts/live_lpar_affinity_group.py. Run none then 1 serially with at most one
owned inactive AIX/Linux fixture: create without apply, independently observe, and
owned inactive-only delete. Preflight and independent recovery surround execution.
Unrun VIOS/controller/other values remain explicit gaps; no live maturity promotion.
