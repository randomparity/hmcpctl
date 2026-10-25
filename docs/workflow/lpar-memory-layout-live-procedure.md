# Creation memory layout: native verification procedure

This procedure covers #1767's six creation fields through the existing LPAR
operation and HPT/PPT ratios through the existing VIOS operation. It grants no
hardware, activation, pool-change or cleanup authority. Follow
[live-testing.md](../live-testing.md) for the authoritative candidate, preflight,
named-arm, private output, recovery and evidence requirements.

## Current checkpoint

The registered opt-in ST58 group is `creation-memory-layout`, dispatched only by
`scripts/live_creation_memory_layout.py --results-file <fresh-private-path>`.
It is excluded from default and `all`. Registration and offline tests are not
native evidence. Obtain a separate exclusive root window, current tool policy,
and a composed physical request/deadline/session budget before running it.

Set these optional keys in the authoritative local `.env`; ambient exports do not
arm it. Both ratio values must be admitted by the selected system's advertisements.
Empty ratios or false conditional permission skip the whole arm before writes.

```dotenv
LIVE_TEST_CREATION_MEMORY_LAYOUT_HPT_RATIO=1:128
LIVE_TEST_CREATION_MEMORY_LAYOUT_PPT_RATIO=1:256
LIVE_TEST_CREATION_MEMORY_LAYOUT_FRESH_VIOS_DELETE_AUTHORIZED=false
```

The ratios above are examples, not platform defaults. Set the boolean true only
under a current separate grant covering this invocation's successful returned
fresh VIOS UUID, conditional on prior absence, baseline exclusion, selected-system
containment and exact fresh name/type/inactive checks. Existing `vios.delete`
policy must separately allow the delete; creation permission is insufficient.

Run these as separate invocations after deployment/import attestation. `$PRIVATE`
must name an existing owner-only directory outside the checkout. Both results
and the `-observations.json` sidecar must be absent before bootstrap.

```sh
uv run --no-sync python scripts/live_test_preflight.py --group creation-memory-layout
uv run --no-sync python scripts/live_creation_memory_layout.py --results-file "$PRIVATE/test-results-creation-memory-layout.json"
uv run --no-sync python scripts/live_test_recovery.py --results "$PRIVATE/test-results-creation-memory-layout.json"
uv run --no-sync python scripts/live_test_evidence.py "$PRIVATE/test-results-creation-memory-layout.json"
```

Recovery is mandatory after failure, cancellation and apparent success. Preserve
its private findings before generating filtered evidence. Neither software
rollback nor read-only recovery removes resources, pending intent or archives.

The finite matrix first creates a fresh owned inactive AIX/Linux partition with
huge-page count0/0/0, both supplied ratios and Decimal0 reservation, without apply.
Only after independent readback, token-bound inactive cleanup and baseline equality
does it create a fresh ratio-only VIOS with explicit resources. Each uses2GiB
desired memory/0.5shared units. Maximum effects:2creates,1LPAR stamp,2deletes.
There are no roles, hardware flags, activation, storage, adapter, pool or
existing-VIOS effects, retries or fallback. Uncertainty stops the next fixture.

The journal is closed, candidate/HMC/system/invocation-bound and checkpointed
before each mutating tool. Prior ST58 output/artifacts are refused rather than
restored; round2/vmedia context is never borrowed. Saved booleans do not authorize
current cleanup. A partial/unknown VIOS without a successful returned UUID stays
an orphan intent: same-name rediscovery is diagnostic only. Root must separately
reconcile and authorize any later exact-UUID cleanup.

The independent oracle calls only the fixed native `lssyscfg -r prof` projection
through `hmc_run_command`, with exact fresh-name/default-profile identity and
literal eight-column header/order/width. It shares no product getter/field mapping.
Fresh mode and UUID/name/inactive checks surround each read. Controller/unknown
mode never triggers a profile query. A product success cannot override honest raw
mismatch; raw equality cannot repair incomplete product creation. Only independent
assertions plus cleanup/baseline equality produce the two configured observations.

Physical feasibility is not established by the logical effect count. A baseline
pair can already require42SSH before oracle/product/admission/cleanup/auth/recovery
costs.40SSH is not a valid whole-window bound. Root must derive finite per-call
budgets, staggered absolute deadlines and own-session/reaping reserve before a
physical allocation; no cap expansion is authorized mid-window.

The tokenized memory-source vocabulary fixture records read-only source answers
from a separately identified source client. It proves vocabulary availability in
that observed context; it is not evidence that this candidate created an LPAR or
VIOS, activated huge pages, or deployed Linux/KVM. Preserve that distinction and
all prior failed capture preparations. Never promote source captures to passing
C06 live observations.

## Prerequisites and selected scope

1. Freeze the candidate commit and verify the deployed/imported build matches
   that exact commit. Record host/runtime, HMC version/build, firmware, schema pin,
   selected-system UUID and management mode privately. Retain the exact tool/CLI
   inputs and the operation's existing authorization and ownership policy.
2. Use fresh partition names and, for LPARs, fresh caller tokens. Establish scoped
   absence before creation. Ambiguous names, unreadable scope or duplicate
   identities must stop the attempt; do not reuse an earlier partial resource.
3. Obtain the selected system's actual HPT/PPT advertisement for requested ratios.
   Do not infer values from POWER generation. Preserve ordinary-memory/resource
   checks and every independently selected hardware-memory, security-policy,
   vTPM, reporting, role and physical-I/O prerequisite. Selecting a layout alone
   does not imply `kvm_capable` or management-role prerequisites.
4. Huge-page counts are AIX/Linux-only nonnegative integers, with supplied
   minimum <= desired <= maximum. Omitted bounds do not acquire invented defaults.
   Huge pages use a separate pool. Current free-pool size is not profile-create
   admission: shortage can prevent activation. Positive activation proof needs an
   already configured pool and separate activation authority; never reconfigure
   the pool or estimate hypervisor overhead under a creation-only grant.
5. Reservation is Linux intent within AIX/Linux, including explicit zero, not an
   installed-OS assertion. Preserve the original Decimal input: reject nonfinite
   values and more than two fractional decimal places before effects; bound its
   canonical fixed representation to 65,536 ASCII bytes. HMC decides undocumented
   numeric limits; do not invent a 0–100 interval. A target reporting KVM capability
   false cannot establish positive KVM runtime semantics.

| Context | Admitted layout inputs | Required observation |
| --- | --- | --- |
| AIX/Linux LPAR | Three huge-page counts, HPT/PPT ratios, Linux reservation | Exact requested saved values in classic mode; no boot, pool-allocation or guest-runtime inference |
| OS400 LPAR | HPT/PPT ratios only | Exact requested saved ratios; no Linux-only fields |
| VIOS | HPT/PPT ratios only, with explicit resource values | Fresh scoped VIOS identity and exact requested saved ratios; no LPAR ownership-stamp facility invented |
| Controller mode | The same field-specific admitted inputs | No named-profile creation/read/application; unavailable supported current mappings produce incomplete verification |

## Submission, identity and outcomes

Run the ST58 preflight above, inspect its actual mutation list, then dispatch
only the separately approved named arm. That arm must use the
existing `hmc_create_lpar`/LPAR CLI or `hmc_create_vios`/VIOS CLI entry point and
retain the single native creation record without exposing private identifiers.
Omitted layout must retain its existing path with no extra layout reads or steps.

In classic mode, independently compare exactly the selected system/partition's
`default_profile`: one matching partition/profile identity, the fixed six-column
layout read and only the requested values. Decimal numeric equivalence is valid;
zero is never omission. Missing, malformed, duplicate or unreadable observations
are unavailable; a known contradictory value remains mismatch even if another
requested field is missing. Saved values prove configuration, not activation.

Controller mode forbids named profiles. Require the existing mode-only authority
check and omit profile_name from the single write. Current configured-value
mappings for these fields are not established by this implementation: report
unavailable/incomplete creation and stop dependent assignments. Never query a
classic profile and describe it as controller current state.

Retain rejected, partial and unknown outcomes, native acknowledgement and scoped
identity evidence independently. A transport failure is not rejection. The LPAR
path distinguishes a coded native refusal with scoped absence, an observed created
identity, and an unknown outcome. VIOS native failure remains an explicit unknown
outcome. Neither path retries creation or rolls back automatically. Failed/missing
verification or ownership must block profile application and dependent assignments;
existing ancillary verification barriers remain independent when inputs are mixed.

## Recovery, cleanup and public evidence

After success, failure or cancellation, run the applicable recovery procedure from
live-testing.md before another attempt. Recheck exact selected-system membership,
UUID, name, state and fresh-resource authorization before cleanup. For an LPAR,
recheck the ownership stamp and caller token against this attempt. An unstamped or
ambiguous resource requires operator inspection, not automatic adoption/deletion.
For a VIOS, require explicit deletion authority over the newly confirmed UUID and
its existing ownership policy; there is no new caller-token/stamp mechanism.
Never delete an unrelated partition, a pool or an archive. Preserve uncertainty
when identity or cleanup cannot be established, and stop further dependent work.

Keep raw results, captures, credentials and identifiers private. Once an applicable
arm provides asserted observations and recovery proof, use
`scripts/live_test_evidence.py` to generate filtered public evidence for its exact
tested commit. Review all output for identifiers before publication. Do not hand
transcribe a live matrix or mark an unrun, skipped or failed arm as passing. An
expected-refusal assertion may pass while creation remains rejected; rejected,
partial and unknown outcomes never prove successful verified creation. Existing
catalog observations and shared-family accounting remain unchanged until the
appropriate proof and separate publication authority.
