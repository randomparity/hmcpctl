# ST9 trunked-switch selection and stranded-network cleanup (#1861)

## Problem

`vlan_baseline` (`scripts/live_test/network.py`) takes the first listed `SwitchID`; on
2026-11-02 that switch had no VIOS trunk and the client adapter add failed. Its `_Stop`
escaped `vlan_round_trip` before `delete_run_networks`, stranding the run's network.

## Scope

- **Switch (A).** `vlan_baseline` also reads `hmc_list_vios` for the test system and
  collects each entry's `TrunkAdapters.TrunkAdapter` `VirtualSwitchID` (mapping or list,
  per the live-captured VIOS entry), then selects the lowest listed `SwitchID` in that set.
  A failed or unparsable read, or no trunked listed switch, SKIPs
  `hmc_create_virtual_network` with that reason. Never falls back; storage still runs.
- **Cleanup (B).** On a `_Stop` from `client_network_adapter`, `vlan_round_trip` re-reads
  the test partition's client network adapters. Only when the read succeeds and every
  adapter has an integer `PortVLANID` and a parsable `TaggedVLANIDs`, none equal to the
  run's VLAN, does it continue the normal path (`delete_run_networks`, both observations,
  the networks manual row if not restored), then re-raise the original `_Stop`. Otherwise
  it records the existing manual row and retains the network.
- `docs/live-testing.md` VLAN bullet states both rules.
- Out of scope (operator-approved): VSI-field omission, fixture-custody refactor, other
  arms, removing the stale switch.

### Failure model

1. Actors: the operator running `scripts/live_network.py` in the test boundary; CI runs
   only fake-HMC tests.
2. Invariants: delete only the run's own networks, never while a test-partition adapter
   is on the run's VLAN; no mutation on an untrunked switch.
3. Accepted: a trunk adapter not varied on still counts (the proof run checks); an
   adapter added between re-read and delete (the HMC refuses; cleanup records failed).
4. Covered elsewhere: the VSI-field hypothesis (follow-up only if the proof run fails).

## Success

1. With switches `[1, 0]` and a trunk only on 0, the network and adapter use switch 0.
2. With no trunked listed switch, or a failed `hmc_list_vios`, no VLAN mutation occurs
   and the SKIP reason is recorded; vSCSI still runs.
3. A failed adapter add leaving no adapter on the VLAN ends with networks equal to the
   baseline and later round trips stopped.
4. A stop with an adapter on the VLAN, or an unparsable VLAN field, keeps the network.
5. `just verify` green; one live ST9 run creates the network and adds the adapter on a
   trunked switch (a SKIP fails the proof) and passes the vSCSI round trip.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Trunked-switch preference (S1) | focused-test | `test_the_lowest_trunked_switch_is_selected`; red: switch 1 used |
| No-trunk / unreadable skip (S2) | focused-test | `test_no_trunked_switch_skips_the_vlan_round_trip` (parametrized); red: create called |
| Cleanup after failed add (S3) | focused-test | `test_a_failed_adapter_add_deletes_the_runs_network`; red: network remains |
| Malformed VLAN fields retain (S4) | focused-test | `test_an_unparsable_adapter_vlan_keeps_the_network`; red: same mutation |
| Retention with adapter (S4) | focused-test | `test_an_adapter_on_the_vlan_keeps_the_network`; red: `_vlan_free` forced true |
| Docs bullet | task-test-not-applicable | unasserted prose; nothing parses it |
| Live run (S5) | task-test-not-applicable | needs a real HMC |

Focused: `uv run --no-sync pytest tests/test_live_network_arm.py -q`.
