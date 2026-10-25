# Creation memory layout: native verification procedure

This procedure covers #1767's six creation fields through the existing LPAR
operation and HPT/PPT ratios through the existing VIOS operation. It grants no
hardware, activation, pool-change or cleanup authority. Follow
[live-testing.md](../live-testing.md) for the authoritative candidate, preflight,
named-arm, private output, recovery and evidence requirements.

## Current checkpoint

There is no registered C06 memory-layout arm in the current live runner. Stop at
that prerequisite: do not substitute an unrelated arm, an ad-hoc native write or
an invented runner invocation. An operator must separately approve a concrete
registered arm, exact field/context matrix, disposable resources, call/time
budgets and recovery boundary before this procedure can produce live evidence.
The procedure and offline tests do not satisfy that missing hardware proof.

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

Run the applicable preflight from live-testing.md, inspect its actual mutation
list, then dispatch only the separately approved named arm. That arm must use the
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
