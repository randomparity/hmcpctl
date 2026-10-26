# ADR 0300: LPAR processor move

## Status

Proposed for #1779; independent review and scope audit pending.

## Context

D06 adds the documented native two-partition processor move after the landed D05
quantity owner. Native options distinguish dedicated processors, virtual processors
and entitlement, and permit four source/destination processor-mode pairs. Both endpoints
can be affected even when the command reports an error.

## Decision

Propose a typed move beside LparProcessorChange, fixed grammar in ssh/processors.py,
and one coupled processor_move operation reusing the existing processor snapshot and
result owners. Keep independent policy/ownership checks for both endpoints and explicit
same-system containment. Recheck decision evidence, submit one native move and attempt
both readbacks independently. Preserve current/profile/runtime distinctions and classify
partial or uncertain effects without replay, rollback or independent-change emulation.

In cross-mode moves, verify both selected dedicated-count/shared-entitlement deltas and
independently report and bound the shared endpoint’s actual vCPU quantities. The native
reference does not specify an unselectable vCPU delta; do not invent one. The four
documented pairs remain in scope; no same-mode-only restriction or release floor is adopted. Do not reuse the single-partition unallocated
capacity growth condition for a conserved move.

## Consequences

The native command supplies no cross-transport CAS. Fresh observations reduce stale
submissions but do not eliminate races or prove causality. A result can retain one usable
endpoint observation while the other remains unknown. Source evidence and offline tests
do not certify a live release/state combination. Unselected mixed-mode vCPU
values are observed and bounded, not promised unchanged.

## Considered & rejected

- **Do nothing.** judgment: does not deliver the requested D06 operation.
- **Compose remove and add.** judgment: independent mutations create an avoidable second
  admission/effect window and do not implement the requested native move.
- **Use a generic move framework.** judgment: the existing memory and processor quantity
  contracts differ enough that a new abstraction would obscure the two concrete operations.
- **Support same modes only.** verified: P10 chhwres.md:226–227 and P11 chhwres.md:230–231
  explicitly describe both cross-mode directions; rejecting them removes documented scope.
- **Assume unchanged or one-for-one shared vCPUs in a cross-mode move.** judgment: neither
  assumption is required to prove the selected conversion; report and bound actual vCPUs instead.
