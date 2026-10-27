# ADR 0303: Typed virtual serial adapters at partition creation

## Status

Proposed; design preparation for #1772. No implementation or live-verification claim.

## Context

C11 allocates one field in the P10/P11 partition-create contexts. Its seven-component
client/server grammar names remote partitions and therefore crosses ownership boundaries.
LPAR and VIOS have separate creation owners but share one native creation writer.
The operator approved explicit local maximum-slot prerequisites and HMC-final unresolved
client capability for all documented environments; the exclusion set remains empty.

## Decision

Extend the existing owners with one finite typed serial collection and the missing
maximum-slot presentation inputs. Resolve concrete peers uniquely in the selected system
and authorize their existing ownership before the single native create. Preserve optional
server components and the unbound server. Do not create or modify reciprocal adapters.
Only unresolved serial-client capability goes to HMC; the deterministic grammar, slot,
identity, containment, authorization and established combination guards stay before effects.

Keep native submission, contained created identity, requested configuration and paired
configuration as separate facts. A refusal or lost reply does not establish absence.
Never retry creation or roll it back automatically. Preserve the LPAR stamp contract;
VIOS retains its existing no-stamp entry/exception contract. A configured unbound server
is not a paired connection. Saved-profile comparison does not prove current configuration,
serial traffic, boot or console functionality; unavailable evidence stops dependent effects.

Use existing profile reads solely as observations. Do not extend #1724's profile CRUD/XML
vocabulary. Keep C12's positive virtual-console/load-source gap with #638.

## Consequences

Both creation presentations expose the complete nested input. VIOS MCP becomes explicitly
non-exhaustive for nested peer targets, requiring one all-targets grant in addition to
operation ownership checks. Named-profile observations are unavailable in controller mode;
no invented profile or successful current-pair result substitutes for them.
The field's implementation and its live evidence remain separately accounted.

## Considered & rejected

- LPAR-only or VIOS-only client exposure. verified: frozen scope comment 6419243721 retains
  all documented environments and the empty exclusion set; either would contradict it.
- A new generic adapter command or reciprocal provisioning workflow. judgment: more mutation
  surface than the one creation field requires, with additional failure/ownership states.
- Predicting client eligibility from environment names. verified: retained HSCL07C1 states
  a capability requirement but no current predicate; the operator explicitly chose HMC-final
  eligibility on 2026-10-26. This is not a source-proven platform matrix.
- Doing nothing. judgment: leaves the allocated creation field unavailable to typed callers.
