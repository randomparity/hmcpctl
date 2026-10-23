# D01: Partition memory quantities

## Problem and authority

Issue #1774, native child of #638, owns non-pool `chhwres -r mem -o a/r/s`
quantity and entitled-memory semantics. Frozen scope is q1774-4f3dda1d in
issue comment 6355899616; exclusions are explicitly empty. Moves, pool lifecycle,
paging, statistics and profile operations retain their existing owners.
[ADR 0285](../../adr/0285-lpar-memory-delta-operations.md) records the transport split.

Local P10/P11 `commands/chhwres.md:90–96,206,224–225,234,236` establish current-value
deltas, region alignment, shared-only numeric/auto entitlement, coupled shutdown
reset, wait and force semantics. `Primitives.xsd` defines Megabyte.Type as 2^20 bytes
with integral, nonnegative values; EntitledIOMegabyte.Union documents positive numeric
input or auto. Served memory/pool schemas establish read vocabulary, not REST write
permissions. Exact field facts were captured read-only on HMC V10R3 M1060; successful
schema retrieval is not positive AMS or quantity-mutation evidence. Published pool
child URI is in P10 `managed-system/152-shared-memory-pool.md:17,24` and P11 equivalent.
The inspected local corpora are references only; their prose is not copied here.

## Architecture and interfaces

Keep `set_lpar_memory(resources)` and its whole-partition If-Match update unchanged.
That setter edits Desired/Minimum/MaximumMemory, while native quantities operate on
CurrentMemory and permit shared entitlement. They are not interchangeable. Reuse the
existing setter for the existing resources presentation; do not emulate native deltas
by an unsafe read, arithmetic and unconditional absolute write.

Add one typed `LparMemoryChange` in `documents/lpar_memory.py`: operation add/remove/set;
optional quantity_mib (strict nonnegative integer); optional entitled_mib (strict
positive integer or literal auto); force (strict bool, default false). Reject unknown
fields, empty requests, booleans/fractions/string numbers and invalid combinations.
Use one owning `change_lpar_memory` operation in `operations/lpar/memory.py`, one fixed
quantity command encoder in `ssh/memory.py`, and no facade or generic command surface.

Extend `hmc_dlpar_mem` with optional change, preserving the old arguments/defaults and
operation/security identity. A nonempty resources request and change are mutually
exclusive before connection effects. New quantity mode requires managed-system scope;
the old setter retains its optional selector. Add `lpars memory NAME --system SYSTEM
--operation add|remove|set [--quantity MIB] [--entitled MIB|auto] [--force] [--yes]` in
its own CLI module and register it explicitly. Return structured outcomes and a failing
CLI exit for rejected, partial or unverified changes. CLI confirmation explicitly warns
that a shutdown reduction below minimum also clears processor resources.

## Admission and execution

Resolve contained system/partition identities and authorize ownership using existing
helpers; carry resolved UUID, CLI name and numeric PartitionID. Read the partition and
system after authorization. Reject missing, duplicate, malformed or contradictory
identity/state/mode/resource evidence rather than filling it with defaults.

Parse CurrentMemory, CurrentMinimumMemory/CurrentMaximumMemory, DesiredMemory,
MinimumMemory/MaximumMemory, RuntimeMemory, SharedMemoryEnabled and relevant entitled
fields. Validate each nonnegative ordered current/configured envelope independently; pending
values may legitimately differ from current. Compute add/remove targets from current,
not desired/runtime. Use current min/max for assigned resources; an inactive all-zero current
envelope may use a separately established nonzero configured min/max envelope. Refuse
subtraction beyond current or a target above that selected maximum.
A running reduction below minimum is refused; shutdown below-minimum removal/set is
admitted as the documented reset-to-zero of memory and processor assignments. A zero
current envelope admits only zero unless a nonzero configured envelope is established;
report the existing configuration/profile prerequisite, without activating or applying it.
A zero add/remove with no entitlement is an observed no-op; numeric entitlement input
is positive, and set zero is a real reset request, not an implicit no-op.

Quantity input and resulting nonzero quantity must align to MemoryRegionSize. All API
amounts are MiB; no decimal MB conversion or rounding. Numeric entitlement does not inherit
the logical memory-region alignment requirement. Entitlement is admitted only when the
partition reports shared memory. Its numeric target is computed from CurrentEntitledMemory; refuse subtraction beyond that
current entitlement and reject a negative target. Auto requests automatic mode, with no
invented formula or predicted numeric entitlement. Numeric add/remove requires a reported
numeric baseline even when automatic mode was previously enabled.

For dedicated growth, compare required increment with CurrentAvailableSystemMemory and
configured system memory. For shared mode, read the documented selected-system
SharedMemoryPool child feed and require exactly one valid pool; use
CurrentAvailablePoolMemory for numeric entitlement growth. Do not use system free memory
as pool free memory, and do not cap overcommitted logical memory to physical system size.
Automatic entitlement and the hypervisor's final allocation remain native admission:
read the available pool capacity and reject inconsistent/unavailable evidence, but do
not invent automatic demand. Capacity races can still cause native refusal/partial effects.

Admit stable running or not-activated/shutdown state; refuse transitional/unknown states.
Running AIX/Linux/VIOS requests require active RMC and DLPAR memory capability, except
explicit force permits the documented no-RMC next-activation/restart path. Force is
not admitted for IBM i memory. IBM i running admission uses reported dynamic-memory
capability rather than an invented RMC prerequisite. Inactive changes never claim a
running-guest effect. Re-read target identity and decision-relevant memory/state/mode/
profile-sync fields immediately before submitting; changed evidence refuses before write.

Submit exactly one fixed `chhwres -r mem -m SYSTEM -o a|r|s --id ID` command with only
requested -q, --entitled and admitted --force tokens. Quote system names; IDs and quantities
are validated typed scalars. No automatic replay, rollback, profile update or activation.
Use existing bounded SSH transport timeout; timeout/connection loss is uncertain submission,
not rejection. Existing REST absolute writes retain their If-Match concurrency guarantee;
the native CLI has no invented ETag option and its deltas are applied by the HMC itself.

## Readback and result

After submission success or transport/CLI failure, independently re-read the same contained
partition. Return before/after evidence, requested/expected values, submission error,
change_location/warnings and one outcome: applied, rejected, partial, unverified, or noop.
Set workflow_completed only for independently established applied/noop outcomes.
Applied requires the requested current quantity and numeric entitlement/manual mode, or
automatic mode, to match. For admitted live-RMC changes, also require the corresponding
runtime quantities; forced/no-RMC and inactive results explicitly identify deferred effect.
A shutdown reset requires memory minimum/current/maximum and the mode-specific current
processor minimum/assigned/maximum vectors to read zero; otherwise it is partial/unverified.
Current dedicated/shared processor read vocabulary already exists in captured live fixtures.

A clean native refusal plus unchanged relevant before/after values is rejected. Any observed
but incomplete requested change is partial. Missing/malformed/failed readback is unverified;
matching independent state may establish applied despite a retained submission error. A known
HMC refusal is never discarded merely because a read happens to match; retain it as the
submission outcome beside the independently verified state. No
result claims causal attribution against an unrelated concurrent writer. Preserve the existing
CurrentProfileSync change-location classification, with forced restart and discarded-profile
warnings stated separately. A changed or missing target identity cannot verify success.

## Failure model

1. Actors/deployments: authenticated local CLI operators and authorized MCP callers using
   the configured HMC; unit/contract CI is offline. HMC-supplied XML is untrusted input.
2. Invariants/assets: contained owned target, exact units/mode/quantity, partition availability,
   honest partial/unknown outcomes, preserved existing absolute-set/If-Match contract.
3. Accepted classes: native capacity/other-writer races after the final read can refuse or
   partially change resources; one submission plus independent readback exposes this. There
   is no cross-transport transaction or automatic compensation. Unavailable/malformed required
   evidence refuses, and post-write read failure remains unverified. Positive AMS, active-RMC,
   forced restart and other hardware/version behavior remain unevidenced until exercised.
4. Covered elsewhere: ownership/access/audit policies by ADR0092/0040 and existing dispatch;
   absolute-set preservation by existing RMW tests; moves #1775, paging #1776, statistics #1777,
   pool lifecycle #642, profile lifecycle #1724 and creation-time fields by their owners.

### Threat model

New boundary: typed quantity input enters fixed native-command construction. Widened
boundaries: the existing memory tool can request quantities/entitlement; HMC readback gains
memory/pool parsing. Local operator/MCP inputs are untrusted; configured HMC credentials and
transport authentication remain existing controls. Strict input models, scoped ownership,
validated numeric IDs, shell quoting, exact singleton pool/target checks and finite read/write
counts govern these boundaries. Transport limits/timeouts and defused XML stay in place.
Malicious configured HMC behavior and external administrative races are not transactions this
client can prevent; they cannot turn missing/readback mismatches into reported success.

## Validation and live proof

The transient plan maps executable contracts to focused tests, including controlled faults.
Final verification is `just verify` plus pinned `uv run --no-sync prek run --all-files` and
all hosted CI legs. Existing absolute memory tests remain the reuse/compatibility proof.

Reserved named arm 50 uses one newly created, owned inactive Linux fixture, reads initialized
current bounds, performs bounded native set/add/remove and no-op/refusal cases, verifies
independent readback and deletes only that fixture. It runs named preflight, the arm, mandatory
recovery, independent before/after comparison and generated commit-bound evidence. It never
activates a guest or invents AMS support. AMS/active/forced cases remain explicit gaps if not run.
No hardware access is granted by this design. Root must grant each candidate-bound window.

Shared runner/preflight/recovery plumbing overlaps unmerged #1773. Implementation must stop
before those edits until #1773 lands, then refresh preserving its arm49 and add arm50. Source,
review counts and frozen scope survive that pause; there is no premature merge-ready handoff.
