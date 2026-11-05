# ST9 refused network adapter add

## Problem and scope

Issue #1894 reports an adapter MANUAL row even when a failed N1 add leaves the
adapter identities equal to their baseline. The existing failure branch always
calls `manual`, which records FAIL and raises `_Stop`.

## Design

Keep failure reporting in `_Arm.client_network_adapter`. Validate the after-listing
with `_network_keys`, compare its canonical UUID set with the validated baseline,
and raise `_Stop` directly when the add status is FAIL and those identities match.
The existing add row already records FAIL. This branch asserts no successful add
or proven transport refusal; the call contract exposes no submission-state field.

For PASS with an unverified fixture, changed or unknown adapter inventory, retain
existing MANUAL behavior. Canonical UUID equality tolerates server spelling case;
malformed and case-duplicate identities cannot establish equivalence.

The direct caller `vlan_round_trip` retains its adapter reread and ownership-checked
VLAN cleanup before reraising `_Stop`. Its unknown inventory and residue checks
remain active. No later adapter/storage test scenario runs after that stop.
No ownership transition, new cleanup path, public API, or compatibility path is needed.
[ADR 0335](../../adr/0335-refused-network-add-recovery-evidence.md) records the choice.

## Success

- Failed N1 add plus validated equal adapter identities records FAIL without its
  adapter MANUAL row, then follows existing VLAN cleanup and stops later scenarios.
- Residue, unreadable/malformed identities and PASS without a verified fixture keep
  MANUAL. Unconfirmed VLAN restoration retains its independent MANUAL marker; a failed
  delete with proven restoration remains FAIL without a new MANUAL.
- Tests exercise the complete `mutate_virtual_networking` caller without a live HMC.

## Failure model

- Actors and deployments: local operators running the network arm; CI scripted arm tests.
- Invariants and assets: preserve recovery evidence for adapter/VLAN residue; stop
  subsequent adapter/storage mutations; delete only through existing ownership checks.
- Accepted failure classes: concurrent changes after the observed listing remain
  bounded by the caller's existing independent reread and recovery checks; the status
  contract does not distinguish submission uncertainty, so no new refusal claim is made.
- Covered elsewhere: storage behavior and live mutation safety remain operator-owned
  exclusions; existing inventory readers own response parsing.

## Validation

The implementation plan inventories regression, conservative-path and stop evidence.
Run focused arm tests, lint/type checks, final `just verify` and pinned prek hooks.
No live mutation run is authorized or needed.
