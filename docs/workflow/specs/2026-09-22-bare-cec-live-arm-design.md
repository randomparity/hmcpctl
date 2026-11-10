# Bare-CEC live arm

Issue #876, under epic #871 (requirement 5). Structure follows ADR 0163's dedicated arm; the
runner, preflight, evidence and recovery tooling are ADR 0162's. No ADR: every choice below is
a harness-internal recording or configuration detail inside those two records.

## Problem

No live arm covers create → dedicated assign → activate → observe → power-off variants →
delete. The dedicated arm (`scripts/live_test/pcie.py`, subtask 24) writes no `record_verified`
row, so its 18 PASS rows on 2026-09-21 promoted nothing, and `job.get`/`job.wait` are verified
only under subtask 12 on an arbitrary sampled job.

## Scope

A `bare-cec` group dispatching subtask 25, entry point `scripts/live_bare_cec.py`, scenario
`scripts/live_test/arms/bare_cec.py`. Nothing under `src/` changes.

**Configuration.** The arm reuses the dedicated arm's `LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME`,
`_LPAR_PREFIX`, `_PROFILE_NAME`, `_DRC_INDEX` through `pcie._dedicated_config`, so the
delimiter rejection, the no-fallback rule, preflight's predicate and `live_test_recovery.py`
(which reads `dedicated_pcie_*` config and `pcie_*` artifacts) all apply unchanged. One new
optional key, `LIVE_TEST_ACCEPT_PLATFORM_DUMP`, is added to `LiveTestConfig
._OPTIONAL_CONFIG_FIELDS` as `accept_platform_dump: str = ""`. Only `true` opts in; `false` or
empty does not; any other value SKIPs the arm before anything is created. Separate
`LIVE_TEST_BARE_CEC_*` keys were rejected: recovery and preflight would each need a second
reader for the same four facts.

**Admission, before anything is created** (each a SKIP row):
1. `HMCConfig().authorize_power_operations` is false — the evidence must cover the guarded path.
2. `LIVE_TEST_ACCEPT_PLATFORM_DUMP` is not `""`, `true` or `false` (case-insensitive).
3. The dedicated arm already ran in this process (a subtask-29 row exists, as in an `all`
   run): both arms write the single-valued `pcie_*` artifacts recovery reads, so a second
   fixture would hide the first from the witness. Restored artifacts add no rows, so a
   restored marker alone does not trip this.
4. `pcie.capture_dedicated_baseline` SKIPs (configuration, ADR 0165 envelope, slot selection).
   It also mints the run marker and records the `pcie_*` artifacts recovery reads.

**Fixture create.** `pcie.create_dedicated_fixture`'s body after the create-time probe is
extracted, unchanged, into `pcie.create_fixture_partition(client, state, fixture, *,
resources=None)`; `create_dedicated_fixture` calls it with no `resources`, so the dedicated
arm's calls, rows and returns are byte-for-byte what they are today. The bare-cec arm calls it
with explicit shared-processor resources (`min/desired/max_procs` 0.1/0.5/1.0, vCPUs 1/1/1,
memory 1024/2048/4096 MiB, uncapped), so it does not depend on #938's defaults. No create-time
probe runs: that is subtask 24's evidence.

**Steps**, in order; a step whose precondition fails ends the steps and hands to teardown.
The arm's own rows use subtask 35 (row 25 is the SR-IOV arm's; rows 29, 30 and 34 keep
coming from the reused dedicated helpers with their wording unchanged); dispatch is 25.
1. Create (above). Then resolve the activation profile: the ADR 0165 profile table must hold
   exactly one row for the fixture, named the configured profile, and `hmc_get_lpar`'s
   `AssociatedPartitionProfile.href` must end in a UUID. That UUID is the partition's one
   profile. Failure is a FAIL row.
2. `hmc_assign_dedicated_pcie_slot`, then `io_slots` readback must list the slot; the readback
   is stored as `fixture.applied_io_slots` so ADR 0163 Guard B recognises it.
3. `hmc_power_on_lpar` with no partition profile, `wait=true`. Recorded, never promoted (L3):
   see *Expected refusals*. The state is then read and recorded; if the partition left
   `not activated` it is powered off (`shutdown`, immediate) and must return there.
4. `hmc_power_on_lpar` with the step-1 UUID, `boot_mode=sms`, `wait=true`, then the state is
   polled until `open firmware` or `running`. **Promotes `lpar.power_on`.**
5. `hmc_get_job` and `hmc_wait_for_job` on that job's identifier. **Promote `job.get`,
   `job.wait`.** Skipped when the outcome carries no job identifier.
6. `hmc_read_lpar_refcodes`, `count=5`. **Promotes `lpar.list_refcodes`.**
7. `hmc_capture_lpar_console`, 30 s, `idle_timeout_seconds=30` so an idle SMS screen does not
   end it early. **Promotes `lpar.capture_console`.**
8. `hmc_power_off_lpar` `shutdown`, immediate, `wait=true`; state must reach `not activated`.
   **Promotes `lpar.power_off`.**
9. Power on as step 4 (recorded only).
10. `shutdown`, immediate, `restart=true`, `wait=true`; state polled to firmware (recorded).
11. `osshutdown` — a declared expected refusal (below).
12. `dumprestart` with `allow_dump_restart=true` only when opted in, then the state is polled
    to firmware; otherwise a SKIP row.
13. A SKIP row for network boot, naming #868.

**Teardown** runs in `finally`, after any step or an exception, and decides on live state:
1. Nothing created → nothing to do. A fixture whose create was never confirmed was never
   activated → `pcie.cleanup_dedicated`. Otherwise read identity (`_read_dedicated_state`),
   twice before concluding a mismatch; a caller-token or UUID mismatch leaves the partition
   untouched — it may be active, and the shared guards would unassign and delete without
   powering off — with a manual-recovery row naming power-off, `io_slots-` and `rmsyscfg`.
2. State other than `not activated` (including unreadable) → power off `shutdown`, immediate,
   `wait=true`, and poll. Still not `not activated` → a manual-recovery row naming the
   `chsysstate … -o shutdown --immed`, `io_slots-` and `rmsyscfg` commands; stop.
3. Profile equals `applied_io_slots` → `hmc_unassign_dedicated_pcie_slot`; readback must equal
   the captured baseline. **Promotes `pcie.unassign_dedicated_slot`.** Profile at baseline →
   no unassign. Anything else → `cleanup_dedicated` (Guard B refuses drift).
4. Re-read identity and profile (Guard C); on a match `hmc_delete_lpar` by UUID, then assert
   the name answers HSCL8012 and the slot is unowned and listed by no profile. **Promotes
   `lpar.delete`.** A failed delete first re-reads the name: HSCL8012 means a lost response
   and the delete is judged by the same assertions. Otherwise, and on a failed guard, it goes
   to `cleanup_dedicated`, which re-decides on live state.
5. `lpar.create` and `pcie.assign_dedicated_slot` are recorded last, with `cleanup: passed`
   only when step 4's three assertions held, else `failed` — their cleanup *is* the teardown.
   Unassign and delete are recorded only where they run; one the teardown never reached has
   no observation, as in every other arm, and its FAIL or manual-recovery row says why.

**Expected refusals.** A PowerOn/PowerOff refusal arrives either raised (FAIL) or as a
terminal failed job returned by `wait=true` (PASS). Two module `ExpectedOutcome`s, both
`transient=True` because an environment refusal is not a product gap: no-profile activation
(`lpar.power_on`, `HSCL3680`) and `osshutdown` without RMC (`lpar.power_off`, `RMC`). A raised
failure goes through `record_with_expected`. A failed job is converted to a `CallFailure`
from its error text; matching the declared outcome records SKIP with that text, otherwise
FAIL. A successful job goes through `record_with_expected` as an observed PASS. The codes are
unconfirmed on hardware; an unmatched refusal is a visible FAIL that #879 reconciles. On every
`wait=true` power call (steps 3, 4, 8-12 and teardown), a job still non-terminal when the wait
expired is a FAIL row naming its status and ends the steps.

**Recording.** Scenario `st35-bare-cec`. Each promoting `record_verified` uses a tool label
distinct before ` (`, because the observation id is `st35-<tool>` and a duplicate discards the
whole observations document. Assertion ids per operation are in *Validation*.

**Also changed.** Runner: `SUBTASKS[25]`, `SUBTASK_GROUPS["bare-cec"] = [25]`, `all` →
`range(26)`, docstring range and wrapper list; the live-testing row-numbering note names row 35. Preflight: a `bare-cec` verdict built on
`pcie._dedicated_config` plus admissions 1-2. `.env.example`, `docs/live-testing.md` (table
row, prerequisites, row numbering, recovery `--results test-results-bare-cec.json`),
`CHANGELOG.md`. `tests/test_live_runner.py` registers the module and the scenario's ids.

## Failure model

1. **Actors and deployments**
   - An operator on a lab host with HMC reach, running the entry point after preflight.
   - CI runs unit tests only; nothing reaches an HMC.
2. **Invariants and assets at stake**
   - The managed system: no partition or slot this run created is stranded without a
     manual-recovery row; nothing it did not create is mutated (run-marker + UUID guards).
   - Promoted observations must be true: a promoting row is `passed` only when its asserted
     postconditions held on live readback.
   - The dedicated arm's behaviour is unchanged by the extraction.
3. **Accepted failure classes**
   - A refusal whose text matches neither declared outcome records FAIL: correct until #879
     confirms the codes.
   - The profile UUID taken from `AssociatedPartitionProfile` is unconfirmed on hardware; a
     missing link is a FAIL row with teardown, not a wrong activation (the operation refuses a
     profile the partition does not contain).
   - A console, refcode or job read that lags or holds a transient error produces a failed
     observation, not a retry; the operator re-runs.
   - An interrupted process (SIGKILL) skips `finally`; `live_test_recovery.py` detects the
     partition, but its printed remedy assumes an inactive one. `docs/live-testing.md` tells
     the operator to power a stranded bare-cec partition off first.
4. **Covered elsewhere**
   - Live execution and promotion into `maturity.json`: #879. Netboot: #868. The recipe: #877.
   - Stranded-state detection after a run: `live_test_recovery.py` (ADR 0162).
   - Envelope admission of the product operations: ADR 0165/0166 gates in `src/`.

## Validation

- `focused-test`: `tests/scripts/test_live_bare_cec.py` against a stubbed state seam (the
  `test_pcie.py` pattern, production `record_with_expected`), covering: happy path call order
  and the ten verified operations with assertion ids — `lpar.create` {`lpar-uuid-resolved`,
  `ownership-and-baseline-confirmed`}, assign {`assign-call-succeeded`, `profile-lists-slot`},
  `lpar.power_on` {`activation-job-successful`, `lpar-reached-firmware`}, job get/wait
  {`job-found`, `job-identity-matches`, `job-status-successful`}, refcodes {`refcodes-returned` (at least one row),
  `refcodes-name-the-fixture`}, console {`console-captured` = call PASS and `stop_reason` not
  `error`, `console-released`}, power off
  {`power-off-job-successful`, `lpar-not-activated`}, unassign {`unassign-call-succeeded`,
  `profile-restored-to-baseline`}, delete {`delete-call-succeeded`, `lpar-name-absent`,
  `slot-released`}; each admission SKIP creates nothing; a timed-out job is FAIL; both refusal shapes (raised, failed
  job) matched → SKIP and unmatched → FAIL; dump opt-in gating; a mid-arm exception still tears
  down; a running partition is powered off before unassign; a failed teardown power-off
  records manual recovery and deletes nothing; an identity mismatch or an identity read that
  fails twice mutates nothing and records manual recovery, one transient failure does not;
  an empty reference-code read is not promoted; a lost-response delete is judged by readback; the
  wrapper dispatches `--group bare-cec`. Green:
  `uv run --no-sync pytest tests/scripts/test_live_bare_cec.py -q --no-cov`.
- `focused-test`: `tests/scripts/test_pcie.py` unchanged and green proves the extraction.
- `focused-test`: `tests/test_live_runner.py` — declared-outcome validator accepts the module,
  registered operations, scenario id map, docstring range, the new key parses.
- `focused-test`: `tests/scripts/test_live_test_preflight.py` — bare-cec verdict RUNNABLE,
  SKIP on missing config, on power authorization off, on an invalid dump value.
- `task-test-not-applicable`: `docs/live-testing.md` and `CHANGELOG.md` prose — no executable
  consumer reads them.
