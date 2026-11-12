# vNIC witness counter settling (#1936)

Decision: [ADR 0370](../../adr/0370-settle-vnic-witness-allocation-counters.md).

## Problem

A witness fixture preflight taken right after a fixture delete can bind a
baseline while the managed system is still releasing the deleted fixture's
allocation. Its recovery then sees different counters and returns
MANUAL_RECOVERY for a state that clears itself.

## Scope

Owner: `scripts/live_test/arms/vnic_creation_policy.py`, which already owns the
fixture snapshot, preflight and recovery. No `src/hmcpctl` change; packet schema
v3 is unchanged.

1. `_fixture_system(hmc, packet)` returns the identity-checked managed-system
   resource. `_fixture_snapshot` uses it instead of its inline check.
2. `_fixture_settling(reads) -> bool` is pure. Each read is
   `{"memory": ..., "cpu": ...}`, the plain sections. It raises `ValueError` when:
   - there are more than `_SETTLE_READS` (6) reads;
   - an equal consecutive pair appears before the last pair;
   - a field other than the six counters in `_fixture_bounded_deltas` changes,
     including their scalar attributes;
   - a counter moves in its allocation direction (the sign of its fixture delta);
   - the first-to-last movement of a counter exceeds the magnitude of its delta
     at `_FIXTURE_HYPERVISOR_MAX`.

   It returns whether the last two reads are equal.
3. `_fixture_settle(hmc, packet, document, output)` reads, journals in
   `document["settling"] = {"max_reads": 6, "interval_seconds": 10, "reads": []}`,
   and saves after each read. It sleeps `_SETTLE_INTERVAL` between reads and
   stops at the first settled pair. Six reads without one raise
   "fixture allocation counters did not settle within the bound".
4. `_fixture_run`: for stage `preflight` with action `prepare`, settle first.
   Then require the snapshot's `system.memory`/`system.cpu` to equal the last read.
5. `_fixture_recover`, preflight stage with action `prepare`: require the
   journal's settling limits to equal the constants, `_fixture_settling(reads)`
   true, and the last read to equal the observation's sections. Then the existing
   single-shot equality applies.
6. `FIXTURE_LIMITS["preflight"]` time becomes 180 seconds; the REST cap rises
   only if the measured worst-case preflight exceeds 64.
7. `docs/live-testing.md`: the rule, labelled observed and not documented HMC
   behaviour. It is reconciled with the cleanup counter-lag rule. #1858 step 4
   tells the witness packet to bind the pre-fixture baseline digest. The caps
   line is updated.

## Failure model

1. Actors and deployments: a local operator running the named preflight and
   recovery scripts against one lab HMC inside the documented mutation boundary;
   CI unit tests with a mocked wire.
2. Invariants and assets at stake: no write during preflight or recovery; every
   read journaled and counted against the stage caps; a READY baseline is never
   taken from moving counters; cleanup's post-delete equality is unchanged.
3. Accepted failure classes:
   - a release slower than about 50 seconds fails normally (bounded, stated in
     ADR 0370);
   - a foreign actor releasing a smaller allocation within the bound during the
     wait reads as settling. It is held by the exclusive-writer acknowledgement
     and by the bound pre-fixture digest the witness supplies.
4. Covered elsewhere: the cleanup counter-lag rule (operator-owned exclusion);
   other arms' preflights (operator).

## Success

- In the mocked counter sequences, a settle within the bound gives READY then
  UNCHANGED; still moving at the sixth read gives MANUAL_RECOVERY; a move in an
  allocation direction gives MANUAL_RECOVERY. None of the three writes.
- Recovery refuses a journal whose settling record is missing or violates
  item 2.
- The live witness after a real fixture delete returns READY and UNCHANGED.

## Validation

- `focused-test`: `tests/test_live_vnic_creation_policy_arm.py` cases
  `test_fixture_witness_settles`, `test_fixture_witness_never_settles`,
  `test_fixture_witness_moves_away`, `test_fixture_witness_recovery_refuses_settling`.
  Each is red before the change (the settle case fails on recovery). Green with
  `uv run --no-sync pytest tests/test_live_vnic_creation_policy_arm.py -q`.
- `focused-test`: the existing `test_fixture_physical_lifecycle` per-stage cap
  assertions hold with the settling reads.
- `task-test-not-applicable`: `docs/live-testing.md` prose. No executable consumer
  reads those sections.
- Live: preflight, prepare, cleanup, witness and recovery through the named
  scripts per `docs/live-testing.md`.
