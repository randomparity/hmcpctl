# ADR 0312: Qualify existing-vNIC policy modes through the registered owner

## Status

Proposed design for #1828, 2026-11-01. The maintainer approved this source-only
direction for formal design. Implementation, final budget admission and hardware
execution remain held. This record does not settle the complete updater design.

## Context

#1828 retains five adapter-policy fields and one submission per complete update.
Creation evidence establishes neither existing-vNIC sentinel updates nor the
result of removing the last list member. The maintainer selected qualification
first and explicit no-members clear: acknowledged `=none` plus independent literal
`none`, never blank or unavailable readback.

At `4d500831b1a197204cbb2a47cc08526a4bb95fbf`, the registered
`scripts/live_test/vnic_creation_policy.py` owner provides contained ownership,
exact candidate/baseline binding, private intent persistence and physical budgets.
Its version-1 packet and recovery history permit creation followed by removal,
not an intervening update. Its cleanup equality still requires the creation policy.

## Decision

Extend that owner with a separately discriminated version-2 `update-mode-source`
packet and source-only history. Reuse its preflight, named capture and independent
read-only recovery entrypoints. Preserve version-1 schema, behavior and limits.

Each packet selects one of six fixed cases: replace-all, replace-none or remove-last
for either allowed-list field. Permit one fresh single-backing creation, one exact
derived update and one removal conditional on acknowledged expected policy and
bounded preservation. Rejection or uncertainty disarms removal. Cleanup compares
the captured object with precisely its selected cell changed to the fixed oracle.

LPAR preparation stays outside this owner. No activation, reseed, alternative
encoding, automatic retry, rollback or maturity promotion follows. The full
five-field updater, later list modes and two-list composition remain #1828 work.
The [source qualification design](../workflow/specs/2026-11-01-vnic-update-source-design.md)
defines the packet, evidence, proposed bounds and implementation admission gates.

## Consequences

A refused or ambiguous experiment can deliberately leave its captured vNIC for
operator disposition. Recovery can report residue but cannot remove it or repair
uncertain history from current absence. A qualified source result establishes only
the exact stored-policy case and environment; it does not establish traffic,
failover, public updater support or completion of #1828.

## Considered & rejected

- **Infer update support from creation.** verified: the retained command reference
  `docs/refs/hmc-commands-p11/commands/chhwres.md:241` describes creation sentinel
  values separately from existing-vNIC list updates; it does not establish their
  update semantics. This is source interpretation, not a hardware result.
- **A separate general diagnostic driver.** judgment: duplicating the existing
  destination, ownership, journal and recovery owner adds unnecessary surface.
- **Reset between cases or clean up after refusal.** judgment: either requires an
  unqualified mutation or authority beyond the approved qualified-cleanup boundary.
- **Do nothing or implement finite modes first.** judgment: neither satisfies the
  maintainer's chosen qualification-first sequence; later modes cannot disappear.
