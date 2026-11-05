# ADR 0335: Refused network add recovery evidence

## Status

Accepted.

## Context

ST9 records FAIL for a failed client network adapter add, but its unconditional
manual-recovery branch demands adapter inspection even when independently listed
adapter identities are unchanged (#1894).

## Decision

For a FAIL add, validated canonical post-listing identities equal to the baseline
permit raising the existing stop directly without an adapter MANUAL row. This is
inventory evidence, not a claim about transport submission. Preserve the existing
caller-owned VLAN cleanup and its recovery checks before the stop reaches ST9.

## Consequences

No later adapter/storage scenario runs after the failure. Changed, unreadable or
malformed inventory, and PASS without a verified fixture, retain MANUAL recovery.
Unconfirmed VLAN restoration retains its recovery requirement; failed deletion
with proven restoration remains FAIL without a new MANUAL.

## Considered & rejected

- **Return normally after the failed add.** verified: at `2487f390`,
  `mutate_virtual_networking` continues storage scenarios after `vlan_round_trip`
  unless `_Stop` propagates.
- **Always request adapter recovery.** judgment: inspection is unnecessary when
  validated inventory proves no new adapter identity.
- **Add a second cleanup path.** judgment: existing caller-owned VLAN cleanup
  already supplies the required reversal and recovery checks.
