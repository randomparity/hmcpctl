# LPAR creation memory layout

## Problem and authority

Issue #1767 owns six CREATE fields and twelve P10/P11 bindings: minimum, desired,
and maximum huge-page counts, HPT ratio, PPT ratio, and KVM reservation percentage.
The [frozen scope](https://github.com/randomparity/hmcpctl/issues/1767#issuecomment-6383900644)
has empty exclusions. It retains M/full-spec/250; implementation waits for campaign
reconciliation of the shared #1765/#1766 creation interfaces.
[ADR 0279](../../adr/0279-lpar-creation-memory-layout.md) records the boundary.

## Source contract

The exact six selectors occur in `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`
and `docs/refs/hmc-commands-p11/commands/mksyscfg.md:49`. Huge-page counts apply to
AIX/Linux. HPT/PPT values must belong to the selected system's respective
`possible_hpt_ratios` / `possible_ppt_ratios` advertisements. KVM reservation is
Linux-only and accepts at most two decimal places. Selecting it, including zero,
declares Linux intent; there is no extra intent parameter or installed-OS claim.

The operator explicitly approved finite-number/two-decimal validation while leaving
undocumented KVM numeric limits to HMC final admission. Do not invent 0..100 or a
`kvm_capable=true` dependency. The captured profile schema's optional CUD float
without numeric facets establishes vocabulary, not acceptance of NaN/infinity.
Huge pages occupy a separate configured system pool; their bytes are not compared
against ordinary desired partition memory. There is no pool reconfiguration here.

## Architecture and data flow

Add one creation-only `LparMemoryLayout`, carried by `LparCreation.memory_layout`
and the existing VIOS creation owner for HPT/PPT fields only. HPT/PPT are unrestricted
by OS in the exact source rows; huge counts are AIX/Linux only and KVM reservation
is Linux-only. OS400 therefore permits ratios but rejects huge counts/reservation;
VIOS permits ratios through its existing create operation/MCP/CLI seams.
Reuse existing command record encoding, native creation, containment, ownership
stamping, profile application and workflow result mechanisms. Do not extend
`LparResources`, whose update consumers would silently widen this creation slice.
No ownership transition or compatibility surface is needed.

The model preserves omission and explicit zero. Counts are strict nonnegative
integers; ratio values are strict advertised scalar tokens. Reservation is a finite
Decimal value with at most two fractional decimal places. CLI tokens remain text
until decimal validation and use no binary-float conversion before serialization;
values that would round to two decimal places as floats are rejected.
Compare each supplied pair of min/desired/max counts without inventing omitted
HMC defaults. Any supplied field selects a single native `mksyscfg` create record;
explicit keylock is refused because that native path cannot represent it.

Before creation, resolve the selected system and retain existing duplicate-name,
authorization and ordinary-memory checks. Read ratio advertisements only when
needed; refuse missing/malformed required advertisements or an unadvertised ratio.
`docs/refs/p10jau.md:2486` establishes that huge-page shortage prevents activation,
not profile creation. Its system-pool display at :1815 does not establish a create
reservation. Do not reject a saved profile because CURRENT free huge pages are low.
Integral page counts supply alignment; changing the system pool requires power-off
(:1817) and is outside this operation. Do not estimate hypervisor overhead or infer
a processor generation's allowed ratios.

After classic-mode creation, independently read exactly the selected
system/partition's `default_profile` and the six fixed native columns. Match partition/profile names,
require one well-formed CSV row, parse counts/percentage strictly, and compare only
supplied values using numeric equivalence for percentage. Missing identity/readback, malformed output, or a mismatch produces an error
memory-layout workflow step and stops later assignments. A typed verification
result distinguishes `verified`, `mismatch` and `unavailable`; observed contradictory
values remain mismatch even if other requested fields are unavailable. Existing profile application semantics stay explicit;
readback verifies saved configuration, not guest behavior or bootability.

Controller mode is admitted: P11 `mksyscfg.md:39` forbids profile_name and
sync_curr_profile, configures the specified attributes immediately and requires
current HMC controller authority. Reuse C05's reviewed controller context; do not
invent profile names, apply a named profile, or reject layout fields for mode alone.
Its API has not landed and must be reconciled before implementation release.
The current schemas establish huge-page and HPT vocabulary, but do not establish a
complete equivalent getter for the six supplied configured values. In particular,
CurrentHugePageCount can concern allocation rather than the requested desired count,
and KVMReservedMemory is proven only on the profile root. No exact positive current
getter mapping is established by this design. Controller creation therefore reports
requested fields as `unavailable` after its single write and stops assignments; it
never reports complete verification or queries default_profile. Add a positive
current-field comparison only when its exact configured-value mapping is established.
This preserves admitted inputs and honest unavailable outcomes, not guaranteed
visibility. No controller, guest or maturity support is inferred from schema names.

## Results and failure model

- Prevent: malformed inputs, nonfinite/overprecision values, unsupported partition
  field context, supplied ordering conflicts, unadvertised ratios,
  and native keylock conflicts before creation effects.
- Detect/report: native refusal or transport uncertainty; partition created with
  absent readback, mismatch, or stamping/application failure. Preserve the existing
  partition and identity; do not retry a create or silently delete it.
- Accepted: huge-page capacity is checked at activation; HMC owns undocumented KVM limits;
  saved-profile equality does not prove activation, installed Linux, or KVM runtime.
- Outside deployment proof: no HMC window is authorized. The available campaign
  target advertises KvmOnPowerVM=false; it cannot prove positive KVM semantics.
  This observation is target-specific, not universal absence of support.

Creation is for a new powered-off partition; classic mode also creates its profile. No dynamic update operation
is introduced; later activation consumes the profile under existing behavior.
Native errors remain unknown unless scoped reconciliation proves no partition
exists; a failed command alone does not prove absence. Successful create with
unavailable verification is incomplete, with no automatic rollback.

## Presentations and validation

MCP adds optional typed `memory_layout`. CLI adds six optional flags matching native
field names with hyphens. Both construct the same model and pass it unchanged.
Existing VIOS create adds ratio-only inputs and uses its existing native-create path,
retaining C04 policy/vTPM composition, explicit-resource prerequisite and scoped
identity/profile verification. It introduces no ownership mechanism or profile apply.
Other creation/provision callers retain the empty default. No stable-facade export.

Focused tests cover strict values, omission/zero, partial ordering, decimal precision,
quoted advertisements, missing advertisements, low/zero pool capacity without invented
creation rejection, malformed/duplicate
CSV rows, foreign profile identity, one native submission, no retry, readback mismatch,
unknown outcomes and workflow assignment suppression. A controlled fault must turn
each new contract test red before its green result is accepted.

Bind all twelve source selectors in the existing capability ledger without claiming
live verification; regenerate runtime metadata/tool docs/scenarios by their recipes.
The reproducible live procedure uses preflight, a fresh owned partition, scoped
post-create readback, and recovery even after success. It records unsupported target
prerequisites and cleanup disposition; no live command runs during design.

Run focused tests during implementation, relevant static gates before commits,
`just verify` and `uv run --no-sync prek run --all-files` before push. Shipping also
requires the existing product/security review workflow and CI version matrix.
