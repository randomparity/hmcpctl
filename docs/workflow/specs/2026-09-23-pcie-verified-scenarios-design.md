# PCIe live arms: verified observations and the io_slots scenario (#985)

## Problem

The dedicated (ST29–ST34) and SR-IOV (ST23–ST28) arms pass live, but record
every step through `RunState.record`, so a run emits no observation and
`maturity.json` cannot be promoted (`_emit_observations` prints "nothing to
write"). The #912 `io_slots` grammar questions were answered only by a manual
probe, which the promotion path cannot take either.

## Scope

Harness only: `scripts/live_test/pcie.py`, one helper moved out of
`scripts/live_test/arms/bare_cec.py`, their tests, and `docs/live-testing.md`. No
`src/` change, no new configuration key, no new runner subtask or group.

**Conversion rule.** Every existing row stays as it is, SKIP, FAIL and
manual-recovery rows included. A step that verifies a mutation by readback
adds one `record_verified` row, except rows 30 and 34 (below), whose
operations another arm observes. A step whose call failed before any readback
adds none. A step that adds something the teardown must undo (rows 25, 27, 31,
33-add, 36-add) stores its assertion values and is recorded after the arm's
cleanup, with cleanup `passed` only when that cleanup restored the baseline,
else `failed`, as `bare_cec._record_create_and_assign` does. Removal and
cleanup steps record where they run. An SR-IOV call whose result reports
`changed=False` (the operations' idempotent no-op) dispatched nothing and backs
no observation, and an unreadable profile never counts as cleared. On this arm
that leaves row 27 unobservable live (the profile-only unassign keeps the
effective port, so the reassign is always a no-op) and row 28 observable only
when ST26 did not clear a pre-populated profile; both records stay as guards.

**Observation ids.** `_observation_id` keeps the label up to the first ` (`,
so every verified label either is a tool name or is a hyphenated slug, and the
(row, label) pairs below are distinct within one run: a duplicate id discards
the whole observations document.

| Row | Label → id | Operation | Assertions | Cleanup |
|---|---|---|---|---|
| 25 | `hmc_assign_sriov_logical_port (verified)` | `sriov.assign_logical_port` | `assign-call-succeeded`, `logical-port-configured`, `owner-is-target-lpar`, `capacity-matches` | teardown |
| 26 | `hmc_unassign_sriov_logical_port (verified)` | `sriov.unassign_logical_port` | `unassign-call-succeeded`, `profile-ports-cleared` | not-required |
| 27 | `hmc_assign_sriov_logical_port (reassign verified)` | `sriov.assign_logical_port` | `assign-call-succeeded`, `logical-port-configured`, `owner-is-target-lpar` | teardown |
| 28 | `hmc_unassign_sriov_logical_port (cleanup verified)` | `sriov.unassign_logical_port` | `unassign-call-succeeded`, `profile-ports-cleared` | passed / failed |
| 31 | `hmc_assign_dedicated_pcie_slot (verified)` | `pcie.assign_dedicated_slot` | `assign-call-succeeded`, `profile-lists-slot` | teardown |
| 33 | `chsyscfg-io-slots-remove` | `command.run` | `remove-command-succeeded`, `profile-restored-to-baseline` | not-required |
| 33 | `chsyscfg-io-slots-add` | `command.run` | `add-command-succeeded`, `profile-lists-slot` | teardown |
| 36 | `hmc_assign_dedicated_pcie_slot (io-slots)` | `pcie.assign_dedicated_slot` | `zero-suffix-add-accepted`, `added-slot-renders-none-pool`, `other-slots-stable-on-add` | teardown |
| 36 | `hmc_unassign_dedicated_pcie_slot (io-slots)` | `pcie.unassign_dedicated_slot` | `zero-suffix-remove-accepted`, `other-slots-stable-on-remove` | not-required |
| 36 | `chsyscfg-io-slots-remove-required` | `command.run` | `required-slot-removed-by-zero-suffix`, `remaining-slot-stable` | not-required |
| 36 | `chsyscfg-io-slots-remove-last` | `command.run` | `remove-command-succeeded`, `empty-profile-reads-none` | not-required |

Scenarios: rows 23–28 `st23-sriov-logical-port`; rows 31–33
`st29-dedicated-pcie`; row 36 `st36-io-slots`. The operation is the tool
actually dispatched: raw `io_slots` grammar goes through `hmc_run_command`, so
it is `command.run`, never a `pcie.*` id. ST32 is not converted: its profile
check repeats ST31's readback and its inventory row is informational.

**Row 28 cleanup value** is `passed` only when the final inventory shows the
port unconfigured and the profile reads clean; it is recorded only on the
branch where cleanup issued the unassign. "Teardown" cleanup is `passed` when
the SR-IOV cleanup left the port unconfigured and the profile clean, or when
the dedicated fixture delete succeeded, its name is absent and no probe
partition remains.

**Rows 30 and 34 are judged rows, not observations** (#1389). The lpar-power
arm (subtask 41) owns the `lpar.create` and `lpar.delete` observations, and the
catalog keeps one per operation, so a dedicated-arm observation would overwrite
it. Both rows are judged as `record_verified` would judge them and recorded
PASS or FAIL without promotion:

| Row | Label | Operation | Assertions | Cleanup |
|---|---|---|---|---|
| 30 | `hmc_create_lpar (create-time judged)` | `lpar.create` | `create-call-succeeded`, `profile-lists-slot` | passed / failed |
| 34 | `hmc_delete_lpar (judged)` | `lpar.delete` | `delete-call-succeeded`, `lpar-name-absent` | not-required |

Row 30's cleanup is the probe partition's own cleanup result. Row 34 is
recorded by the dedicated orchestrator, not by `cleanup_dedicated`, after a
delete call it attempted: `cleanup_dedicated` returns the delete status (or
`None` when it made none), and the orchestrator confirms absence with
`name_absent`, moved from `bare_cec._name_absent` to `pcie.name_absent`.
Keeping it out of the shared cleanup stops the bare-cec arm's fallback call from
emitting a dedicated-scenario row.

**io_slots scenario (row 36).** Runs inside the dedicated arm after a
successful reassign, on its fixture, whose profile then reads `<A>/none/0`
(A = the arm's slot). It requires `baseline_io_slots == "none"` and two further
slots B, C chosen from a fresh inventory and profile read with the arm's own
guards (unowned, listed by no profile, #916); otherwise it SKIPs one row. It
also SKIPs when `LIVE_TEST_DEDICATED_PCIE_DRC_INDEX` pins the arm's slot: a
pinned run mutates no slot the operator did not name.
Steps, each followed by the ADR 0165 readback:

1. raw `io_slots+=B//1`; the value must list B as `B/none/1` beside A, else a
   FAIL row and restore. This value is P2.
2. `hmc_assign_dedicated_pcie_slot` for C (writes `C//0`): C listed; C's element
   is exactly `C/none/0`; the other elements equal P2's, in order.
3. `hmc_unassign_dedicated_pcie_slot` for C: C absent; value equals P2 exactly.
4. raw `io_slots-=B//0`: B absent; value equals `A/none/0`.
5. raw `io_slots-=A//0`: value is exactly `none`.

Each step's verified row is recorded after its readback, whatever the call
status (a lost response has still written). Any failed step, meaning a FAIL
call, an unmet assertion or an unreadable readback, ends the sequence. A
restore then re-reads the profile and removes whichever of C and B it still
lists, each with the `is_required` suffix its readback renders (`C//0`, `B//1`).
That keeps the restore independent of the step-4 hypothesis. If the re-read is
unreadable, it issues both `io_slots-=C//0` and `io_slots-=B//1`. The dedicated
cleanup (Guard B) handles A, or refuses with its existing manual-recovery row
when the profile is neither the baseline nor A. After step 5 the profile is the
baseline, so cleanup deletes the fixture without a removal.

### Failure model

1. **Actors and deployments**
   - The operator running `scripts/live_dedicated.py` or `scripts/live_sriov.py`
     against the admitted V10R3 M1060 / 8375-42A lab, from a clean committed tree.
   - CI running the offline tests with fakes.
2. **Invariants and assets at stake**
   - A slot left listed by a fixture profile, or a fixture left undeleted.
   - The observations document: a duplicate id or a wrong operation id discards
     it or makes a promotion claim nothing exercised.
   - A `passed` observation must mean every named assertion held and its
     cleanup passed or was not needed.
3. **Accepted failure classes**
   - B or C still listed after a restore that failed: the fixture is not deleted
     (Guard B refuses), and the recovery check reports the fixture. B and C are
     not in the recovery artifacts. Bounded: a disposable partition that was
     never activated holds them, and the manual-recovery row names it.
   - Preflight still predicts one dedicated slot. The runbook names the two more
     the scenario takes.
   - `io_slots-=` on an empty list, and quoting of multi-element values, are not
     asserted; the parser already handles quoting.
4. **Covered elsewhere**
   - The live re-run and `maturity.json` entries → #879 window (orchestrator).
   - Scenario and ledger generation → #706.
   - Product operation behaviour → operator.

## Success

- A fake-driven happy run of each arm emits exactly the observations in the
  table: scenario, operation, assertion ids, cleanup, `result: passed`, and
  unique ids.
- A controlled fault (e.g. a C element rendered `C/none/1`, or a failed
  fixture delete) flips the named assertion or the teardown cleanup and makes
  `result` `failed`.
- A pinned DRC, or fewer than two spare slots, SKIPs row 36 with no io_slots
  command naming another DRC.
- `test_scenarios_declare_their_expected_assertion_ids` lists the three new
  scenarios, and `test_verified_scenarios_name_registered_operations` passes.
- Existing SKIP/FAIL/manual-recovery tests keep their assertions (three SR-IOV
  call sites gain the evidence argument): no existing row
  is renamed or removed.

## Validation

`uv run --no-sync pytest tests/scripts/test_pcie.py tests/scripts/test_live_bare_cec.py tests/test_live_runner.py tests/test_live_testing_doc.py`, then `just verify`.
Live confirmation belongs to the #879 window.
