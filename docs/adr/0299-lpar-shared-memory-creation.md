# 0299 — Shared-memory partition creation

## Status

Proposed

## Context

C07 / #1768 allocates eight optional fields in sixteen P10/P11 creation bindings.
Both LPAR and VIOS owners already use one native creation record when a family
requires it. Saved profiles, current configuration and guest runtime differ.
The bounded inactive VIOS AME experiment establishes saved values only; published
runtime requirements cannot silently become a refusal of that observed behavior.

## Decision

Extend the existing direct owners and their CLI/MCP presentations with one typed
creation-memory family, one creation-specific admission/readback owner and the
existing native writer. No profile writer, pool lifecycle or provision input moves.
Use the operator's explicit shared-mode prerequisite for the six shared-only fields;
do not infer a mode. mem_expansion is independent.

Resolve paging names/IDs uniquely inside the selected system and pool; authorize
the affected candidates before submission. Preserve explicit secondary none.
Verify saved designated values independently before optional LPAR apply; report
current configured facts separately. VIOS creation does not automatically apply.
Controller designated postconditions that cannot be observed remain unavailable.
An incomplete creation stops follow-up effects without retry or automatic rollback.

The operator approved the saved-configuration AME policy: source-valid values on
LPAR creation, including IBM i, and VIOS creation; positive values require advertised
ActiveMemoryExpansionCapable. Verify stored settings without claiming guest compression.
Selecting mem_expansion declares no AIX intent. All other admission guards remain;
this contract approval does not accept the complete proposed design.

## Consequences

Missing prerequisite evidence refuses before effects; missing postconditions can
leave an existing inactive resource with incomplete verification. Creation proves
no reserved paging capacity, activation success, guest compression or failover.
Existing mixed-family guards and affected-target policy still apply. At base 056cdc80,
C07 joins landed C11 barriers and preserves both VIOS verification members.

## Considered & rejected

- **Infer shared mode.** verified: #1768 WORK:SCOPE comment 6430972898 records the
  operator's explicit shared-mode prerequisite instead.
- **Second REST/profile writer.** judgment: the existing native record and contained
  profile reader keep one creation owner without another partial-effect boundary.
- **Require free paging devices to save a profile.** verified: retained IBM POWER9
  shared-memory requirements place paging-device allocation at activation.
- **Use current provider as the designated pair.** verified: retained schema hashes
  in the specification distinguish CurrentPagingServicePartition from saved primary
  and secondary links; these are different facts.
- **Do nothing.** verified: #1768's eight-field creation outcome remains unimplemented.
