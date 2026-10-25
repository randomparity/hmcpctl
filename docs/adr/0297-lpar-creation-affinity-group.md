# ADR 0297: Conventional affinity group at partition creation

## Status

Accepted for #1769 after design, scope, product and security review.

## Context

The P10 mksyscfg:41 and P11 mksyscfg:49 selectors under docs/refs admit none
or integer 1–255 for memory/processor grouping. C08 assigns their creation use.
Existing LPAR/VIOS owners already encode native records and handle partial effects.

## Decision

Add optional creation-only affinity_group_id through existing operations and
CLI/MCP: strict integer 1–255, exact none, or omission. CLI parses decimal text.
Selection uses one native record; omission preserves the existing call graph.
Reuse C05's native authority observation without importing Linux/KVM prerequisites;
retain role errors, controller profileless grammar and C10 independent barriers.
Keep all_resources where existing selected-feature contracts allow it.

Observe the selected native partition with documented projection/filter grammar;
label it native-partition. Classic named-profile evidence is separate native-profile.
Require unique contained identity, strict rows and literal request equality.
Neither observation establishes current/runtime semantics or an XML-zero mapping.
Unverified assignment is partial and blocks follow-up apply/assignments.
Preserve VIOS's entry-or-exception shape; no automatic retry, rollback or activation.

## Consequences

Some created resources require operator inspection. Positive/controller behavior
remains unevidenced until a matching-candidate live run; ST57 grants no execution.

## Considered & rejected

- **No change.** verified: #1769 Expected requires the two creation occurrences.
- **Reuse ResourceGroup/minimum policy.** verified: #1769 explicitly separates them.
- **General proxy/shared update field.** judgment: expands a creation-only contract.
- **XML-zero/profile-current equivalence.** verified: retained cli-prof-lp3 proves
  profile none; normalized catalogs carry no mapping or equivalence evidence.
- **Linux/KVM/hardware capability restrictions.** verified: neither creation selector
  attaches those prerequisites to affinity_group_id.
