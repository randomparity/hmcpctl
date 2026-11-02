# ST9 trunked-switch selection and stranded-network cleanup (#1861)

## Problem

`vlan_baseline` (`scripts/live_test/network.py`) takes the first listed `SwitchID`; on
2026-11-02 that switch had no VIOS trunk and the client adapter add failed. Its `_Stop`
escaped `vlan_round_trip` before `delete_run_networks`, stranding the run's network.

## Scope

- **Switch (A).** `vlan_baseline` also calls the existing `hmc_list_vios` tool for the
  test system and collects each entry's `TrunkAdapters.TrunkAdapter` `VirtualSwitchID`
  (a mapping or a list, per the live-captured VIOS entry). It selects the lowest
  listed `SwitchID` in that set. A failed or unparsable trunk read SKIPs
  `hmc_create_virtual_network` with "VIOS trunk adapters could not be read"; an empty
  intersection SKIPs it with "no virtual switch carries a VIOS trunk adapter". Never
  falls back; storage round trips still run.
- **Cleanup (B).** On a `_Stop` from `client_network_adapter`, `vlan_round_trip` re-reads
  the test partition's client network adapters. When the read succeeds and no adapter has
  `PortVLANID` or a `TaggedVLANIDs` entry equal to the run's VLAN, it runs the normal
  `delete_run_networks` and records both network observations, then re-raises the
  original `_Stop`, so later round trips still stop. Otherwise it records the existing
  manual-recovery row and retains the network, as today.
- `docs/live-testing.md` VLAN bullet states both rules. No `src/` change.
- Out of scope (operator-approved): VSI-field omission, fixture-custody refactor, other
  arms, removing the stale switch.

### Failure model

1. Actors: the operator running `scripts/live_network.py` in the test boundary; CI runs
   only fake-HMC tests.
2. Invariants: never delete a network other than the run's own; never delete a network
   while any client adapter is on the run's VLAN; no mutation on an untrunked switch.
3. Accepted: a trunk adapter not varied on still counts (the proof run checks); an
   adapter added between re-read and delete (the HMC refuses; cleanup records failed).
4. Covered elsewhere: the VSI-field hypothesis (follow-up only if the proof run fails).

## Success

1. With switches `[1, 0]` and a trunk only on 0, the network and adapter use switch 0.
2. With no trunked listed switch, or a failed `hmc_list_vios`, no VLAN mutation occurs
   and the SKIP reason is recorded; vSCSI still runs.
3. A failed adapter add leaving no adapter on the VLAN ends with networks equal to the
   baseline and later round trips stopped.
4. A stop with an adapter still on the VLAN keeps the network and the manual row.
5. `just verify` green; one live ST9 run passes through the vSCSI round trip.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Trunked-switch preference (S1) | focused-test | `test_the_lowest_trunked_switch_is_selected`; red: switch 1 used |
| No-trunk / unreadable skip (S2) | focused-test | `test_no_trunked_switch_skips_the_vlan_round_trip` (parametrized); red: create called |
| Cleanup after failed add (S3) | focused-test | `test_a_failed_adapter_add_deletes_the_runs_network`; red: network remains |
| Retention with adapter (S4) | focused-test | `test_an_adapter_on_the_vlan_keeps_the_network`; red: no manual row |
| Docs bullet | task-test-not-applicable | unasserted prose; nothing parses it |
| Live run (S5) | task-test-not-applicable | needs a real HMC; the leased ST9 run |

Focused: `uv run --no-sync pytest tests/test_live_network_arm.py -q`.
