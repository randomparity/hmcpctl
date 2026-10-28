# 0301 — Partition boot and start configuration uses existing owners

## Status

Reviewed design; A1 approved; approved VIOS fixture stamp awaits changed-input A2

G4/raw/result/format and N1/N2/N3 remain approved; D1/D2 findings are dispositioned.

## Context

Issue #1770 assigns six native names across twelve P10/P11 occurrences: five partition
creation inputs and boot_string update. The landed creation core already
owns memory/affinity/serial admission, independent identity and follow-up barriers.

## Decision

Propose extending the existing LPAR and VIOS creation owners and fixed mksyscfg record,
with a creation-only typed value, contained controller resolution and authorization before
effects. Retain AllTargets dispatch for nested targets and domain ownership checks.
Join C09 independent postconditions to the existing apply/assignment family barriers.

Extend the existing LPAR boot-order owner for the approved raw input while preserving path
calls and clear refusal. Keep ETag RMW; observe pending XML text losslessly, not through strip.
Keep saved-profile, controller-current and pending-boot observations separate. A write reply
or an absent field cannot substitute for independent exact readback. Use exact current
IsConnectionMonitoringEnabled and saved ConnectionMonitoringEnabled on separate required
planes. Compare PowerControlPartitions links only against already admitted identities through
existing contained URL policy; do not follow provider URLs or require arbitrary XSD closure.
Missing/malformed required evidence blocks subsequent effects; a valid present empty
relationship mismatches a requested nonempty set. The approved conditional policy replaces
the earlier positive-controller fixture prerequisite without inventing controller live proof.

Adopt a separate VIOS boot owner/presentation in the design. Neither the existing
LPAR XML leaf nor the shared native scalar serializer establishes safe VIOS raw text support.
Source d68a5c55 qualifies the fixed CSV writer and independent native readback on one release.
Consume its bounded evidence through normal parent D1/A1/build gates, without parent live credit.
The [full design draft](../workflow/specs/2026-10-26-lpar-boot-start-design.md) names those gates.

## Consequences

Creation may complete while requested configuration remains mismatched or unavailable;
follow-ups stop and the surviving partition remains visible for inspection. No automatic
rollback, retry, activation, clear alias, new profile CRUD or stable-facade export is added.
A stored boot string proves neither bootability nor a future power-cycle outcome.
Preserve LPAR boot results and outer creation schema; use existing workflow/VIOS metadata seams.
Separate VIOS boot results have three observed keys; null only means observed empty read text.
New raw text preserves printable Unicode/spacing, requires a non-whitespace character and has
a 16KiB UTF-8 application budget; legacy path lists remain unchanged. Exact readback is mandatory.
ST68 may stamp one independently confirmed fresh VIOS fixture using the existing ownership helper.
Separate acknowledgment/marker verification and bounded pre-session config gate guarded cleanup;
No public creator change/hardware authority/live credit; the tracked design owns this finite contract.

## Considered & rejected

- Count activation/path lists as C09 coverage: P10/P11 distinguish create/update; baseline
  boot_order.py returned the write response and activation.py submits a separate immediate job.
- Add fields to shared update/create LparSettings. judgment: a creation-only value keeps
  this finite scope from widening unrelated update inputs and documentation.
- Reuse LPAR XML for VIOS: VirtualIOServer extends BasePartition; only LogicalPartition
  declares BootListInformation. An instance POST endpoint establishes no writable VIOS leaf.
- Relax the generic serializer: its delimiter/trailing-list restrictions cannot carry raw
  boot punctuation; shell quoting is separate. Use only the qualified fixed two-cell writer.
- Introduce a second LPAR setter or generic command proxy. judgment: the existing owner can
  accept the approved additional input while preserving one authorization/readback policy.
