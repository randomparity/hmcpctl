# ST9 storage-client fixture custody specification

Status: design for #1826.
Baseline: ddf3429a (main).
Authority: https://github.com/randomparity/hmcpctl/issues/1826#issuecomment-6588450776.
Lane: full-spec. Complexity: M. Fixed design denominator: 250.

## Problem

ST9's vSCSI and vFC client round trips (`scripts/live_test/arms/network.py`) clean up with
`remove_new_adapters`, which deletes every adapter UUID absent from the baseline. A foreign
adapter that another actor adds to the test partition during the run is therefore deleted as
if it were the run's own. The slot-collision probe relies on the same delta cleanup to remove
anything an accepted collision added. Ownership is inferred from an inventory delta, which the
issue forbids.

## Evidence for the create response

Retained results of earlier ST9 network-arm live runs (V10R3M1060, POWER9, 2026-10-06/07) show
the paired `hmc_add_vscsi_adapter` result carries `UUID`, `ResourceType`
(`VirtualSCSIClientAdapter`) and a `Resource` with `VirtualSlotNumber`,
`RemoteLogicalPartitionID` and `RemoteSlotNumber`, and that UUID is the one new key in the
post-add listing. No vFC create has run on the boundary system (its round trip SKIPs for lack
of an NPIV server slot); vFC uses the same `create_child` path, and its custody fails closed if
the response lacks identity. No new live capture is needed before implementation.

## Scope

In: the vSCSI and vFC client round trip and its slot-collision probe,
and `placement_drift`'s advice text, in `scripts/live_test/arms/network.py`;
`tests/test_live_network_arm.py`; the ST9 vSCSI/vFC bullet of `docs/live-testing.md`; the module
docstring. Out (operator-approved exclusions): #638 capability accounting; new adapter types or
arms; the client network adapter (Ethernet) cleanup path; VIOS mapping management.

## Design

1. **Identity custody.** A new pure function `_storage_fixture(listed, baseline, response,
   adapter_type, pairing) -> str | None` returns the listed UUID of the run's
   fixture only when all hold:
   - `listed` and `baseline` have unique, valid UUID keys (`_network_keys`, case-insensitive);
   - `response` is a mapping whose `ResourceType` equals `adapter_type`, whose `UUID` is a
     valid UUID absent from the baseline, and whose `Resource` is a mapping;
   - the listing's keys are exactly the baseline keys plus that UUID, every baseline adapter
     keeps its baseline placement, and every listed resource is a mapping;
   - the listed fixture's `VirtualSlotNumber` and the family's two pairing fields
     (`_REMOTE_FIELDS`) are integers, equal the same fields in the response `Resource`, and the
     pairing equals the requested `(vios_id, server_slot)`.
   Otherwise `None`. It reuses `_network_keys`, `_placements`, `as_int`, `_network_text`.
2. **Baseline gate.** Before any storage mutation, a baseline whose keys `_network_keys`
   rejects stops ST9 with manual recovery; nothing is created.
3. **Collision probe.** `slot_collision` keeps its contract (True when refused and the listing
   is unchanged, None when there is no used slot). When it returns False, the round trip records
   its observation (collision assertion failed, cleanup failed), stops ST9 with manual recovery,
   and creates and deletes nothing more. The collision call is never a custody source.
4. **Paired fixture.** `add_client` returns `(status, data)`. The add must report PASS, and
   one fresh listing read after it (nothing in the round trip mutates between that read and
   the delete) must yield `_storage_fixture(...)`. Only then is `hmc_delete_adapter` called
   once, for that UUID. A final listing must equal the baseline (case-insensitive keys,
   identical placements) with acknowledged deletion.
5. **Stops.** Any failed step (add not PASS, unreadable listing, `None` custody, changed
   placement, refused delete, unequal final set) records the round trip's observation and stops
   ST9 with a `MANUAL RECOVERY REQUIRED` row naming the response UUID (or "none") and directing
   by-hand reconciliation; it never names other new UUIDs as deletion targets.
6. **Removal.** `remove_new_adapters` and `_new` are deleted; no storage path deletes by delta.
7. **Recovery advice.** `placement_drift` keeps its signature (recovery calls it) but no longer
   tells the operator to remove every new UUID: new UUIDs are listed as not proven to be the
   run's; delete by hand only an adapter the run's results prove is its own (only a storage
   row reporting an unconfirmed deletion names a verified fixture UUID; a custody-failure row
   names the claimed UUID as not proven). The text is kind-independent, so Ethernet
   recovery output changes wording too; no Ethernet logic changes.

## Considered & rejected

- **Own an accepted collision-add by its response UUID.** judgment: an accepted collision is
  already an HMC defect the run reports; adding a second custody path costs more than the stop.
- **Keep delta cleanup, but refuse when the delta has more than one UUID.** verified: issue
  #1826 Expected 3 forbids inferring ownership from inventory delta alone; a lone foreign
  addition plus a failed create would still be deleted.
- **Generalise `_network_fixture` into a shared custody core.** judgment: the shared core would
  edit the Ethernet path, an approved exclusion; recorded as a follow-up candidate.
- **Capture a vFC create live first.** verified: the boundary system has no NPIV server slot
  toward the test partition (retained 2026-10-07 results: vFC round trip SKIP); fail-closed
  custody makes the unknown shape a stop, not a wrong delete.

## Success

1. In the scripted tests, no storage `hmc_delete_adapter` call names any UUID other than the
   paired create response's UUID, across every fault case in Validation.
2. The baseline adapters, foreign adapters added by the fake, VIOS server rows and mappings are
   never `hmc_delete_adapter` targets in those cases.
3. Each fault case records a manual-recovery row and makes no later ST9 mutation.
4. The happy path still promotes `adapter.add_vscsi` and `adapter.add_vfc` with cleanup passed.
5. One ST9 network-arm live run on the branch head passes the vSCSI round trip with cleanup
   passed, followed by a clean recovery check.

## Failure model

1. Actors and deployments
   - The operator running `scripts/live_network.py` against the authorized test HMC, system
     and partition, per `docs/live-testing.md`.
   - Other HMC users or automation that may add or change adapters on that partition mid-run.
2. Invariants and assets at stake
   - Adapters the run did not create (baseline or foreign) are never deleted.
   - VIOS server adapters and mappings are never mutated by the storage round trip.
3. Accepted failure classes
   - Residue after a stop: the run's own fixture may remain on the partition; bounded cost,
     reported by the manual-recovery row and the recovery check.
   - A foreign change between the post-add read and the delete: the delete still names only
     the verified UUID; the final compare reports the drift. Same race the Ethernet path accepts.
   - vFC never exercised live on the boundary system: its custody fails closed.
4. Covered elsewhere
   - Ethernet adapter custody: `delete_network_fixture` (exclusion).
   - Recovery-check detection logic: `scripts/live_test_recovery.py`, unchanged (only the
     advice text it renders changes, Design §7).

## Validation

Tests live in `tests/test_live_network_arm.py`, run by
`uv run --no-sync pytest tests/test_live_network_arm.py -q`.

| Contract | Mode | Case |
|---|---|---|
| Foreign addition is never deleted (both families) | focused-test | red on baseline code first; red output quoted in the PR body |
| Fixture vanished or re-identified after the add | focused-test | no storage delete, manual row |
| Contradictory create/readback (missing/invalid UUID, wrong type, slot mismatch, add FAIL but applied) | focused-test | no storage delete, manual row |
| Recovery advice names no delta as removable | focused-test | `placement_drift` text test |
| Unavailable listing after add | focused-test | no storage delete, manual row |
| Collision not cleanly refused (accepted, failed-but-applied, in-place repair) | focused-test | observation failed, no delete, stop |
| Two adapters from one add | focused-test | no delete, stop |
| Happy path, both families | focused-test | existing round-trip test |
| Docs bullet | task-test-not-applicable | prose read by operators; no executable consumer |
| Live proof | task-test-not-applicable | requires the HMC; run per docs/live-testing.md |
