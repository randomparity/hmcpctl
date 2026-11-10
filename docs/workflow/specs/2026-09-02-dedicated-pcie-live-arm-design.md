# Dedicated PCIe live-assignment arm — design

Issue: [#217](https://github.com/randomparity/hmc-mcp/issues/217) ·
Decision record: [ADR 0163](../../adr/0163-dedicated-pcie-live-evidence-arm.md)

## Goal

Add the dedicated PCIe slot arm of the live-HMC reversible-assignment scenario to
`scripts/live_test/pcie.py`, register it with the live runner, and cover its orchestration
and cleanup guards with unit tests.

The SR-IOV arm (`exercise_sriov_assignment`, ST23–ST28) is already implemented and already
live-verified — 24/24 PASS on 2026-09-01. It is out of scope and is not modified.

Nothing in this change runs against a live HMC. The arm's PASS/SKIP/FAIL matrix is produced
by the operator after merge.

## Background the design turns on

`hmc_assign_dedicated_pcie_slot`, `hmc_unassign_dedicated_pcie_slot`, and a create-time
`LparPcieAssignments.dedicated` request all fail closed under ADR 0055 — before any
*mutating* command, though `_authorize_pcie_profile_request` resolves and authorizes the
target names first, so the refusal costs one HMC round trip. ADR 0053 states the
condition that lifts the gate: exact `io_slots` readback must be admitted. ADR 0163 records
the resulting decision — the arm records the refusal, then gathers the readback evidence
through the documented profile grammar over the runner's `hmc_run_command` escape hatch.

`operations/pcie.py:require_admitted_environment` restricts the SR-IOV mutation path to
HMC `V10R3 M1060` on managed-system model `8375-42A`, the envelope ADR 0053 admits. The
dedicated arm mutates through raw profile grammar rather than through that operation, so
nothing enforces the envelope for it; the arm therefore enforces it itself, at ST29.

## Architecture

One new orchestrator, `exercise_dedicated_pcie_assignment`, registered as live-runner
subtask 24 and as the `dedicated` subtask group. It follows the SR-IOV arm's shape: a frozen
config dataclass, a mutable fixture record, a point-in-time state snapshot with a summary
formatter, one function per step, and a top-level orchestrator that sequences them and
always reaches cleanup.

Step labels are **ST29–ST34**. The SR-IOV arm is registered as subtask 23 but records rows
under subtask numbers 23–28, so 24–28 are already taken in the results document; starting at
29 keeps every row label unique across a full run. The consequence is an asymmetry worth
stating in the module docstring: `--subtask 24` produces a results file in which no row
carries the number the operator typed. Uniqueness of row labels is the property worth having
— a collision would silently merge two arms' evidence — and the docstring is where the
operator finds the mapping.

### Data structures

```python
@dataclass(frozen=True)
class _DedicatedConfig:
    system_name: str
    lpar_prefix: str
    profile_name: str
    drc_index: str | None      # explicit override; None selects from inventory

@dataclass
class _DedicatedFixture:
    config: _DedicatedConfig
    run_marker: str            # ADR 0064 caller token, unique per run
    lpar_name: str             # f"{lpar_prefix}{run_marker}"
    probe_lpar_name: str       # f"{lpar_name}-createtime"; created only if 0055 lifts
    lpar_uuid: str | None = None
    drc_index: str | None = None
    baseline_io_slots: str | None = None   # exact profile value at creation
    applied_io_slots: str | None = None    # exact profile value this run wrote
    created: bool = False      # a partition exists and is this run's to clean up
    probe_created: bool = False  # the create-time probe unexpectedly created one

@dataclass(frozen=True)
class _DedicatedState:
    slot_owner: str | None      # owner_lpar from the dedicated-slot inventory
    profile_io_slots: str | None  # exact `lssyscfg -F io_slots` value
    lpar_uuid: str | None
    caller_token: str | None    # ADR 0064 token parsed from the description
```

### Configuration

Read once, from the environment, with no fallback to `LiveTestContext.system_name` — issue
#217 requires explicit managed-system configuration and forbids falling back to an arbitrary
system.

| Variable | Required | Meaning |
|---|---|---|
| `HMC_LIVE_PCIE_SYSTEM` | yes | Managed-system name the arm may mutate |
| `HMC_LIVE_PCIE_LPAR_PREFIX` | yes | Name prefix for the run-unique fixture LPAR |
| `HMC_LIVE_PCIE_PROFILE` | no | Profile name; defaults to `default_profile` |
| `HMC_LIVE_PCIE_DRC_INDEX` | no | Exact slot to exercise; otherwise auto-selected |

A missing required variable is a SKIP for the whole arm, recorded before any tool call.
These names are not `HMCConfig` fields, so `just env-vars` — which checks `HMCConfig` fields
against `docs/environment-variables.md` — is unaffected. They are documented in the module
docstring, where the operator running the arm will look.

## Steps

**ST29 — configure, admit the environment, and baseline.** Resolve configuration; SKIP the
arm when it is absent. Then read `lshmc -V` and `lssyscfg -r sys -m <system> -F type_model`
— the same two facts `ssh/network.py:read_sriov_environment` collects — record both as
rows so the results file is self-labelling, and SKIP the arm when they fall outside the
`_ADMITTED_HMC_RELEASE` / `_ADMITTED_SYSTEM_MODEL` envelope, applying the same normalized
comparison `require_admitted_environment` applies.

Be precise about what that gate does and does not buy, because the records say less than is
comfortable. ADR 0053 admits the `io_slots` profile-mutation grammar from the **Power8
documentation** row of its evidence table, and the sole supporting artifact,
`tests/fixtures/pcie/power8-profile-contract.json`, carries `"hmc_release":
"not-established"` and `"support": "unknown"`. There is **no** `io_slots` evidence in this
repository for `V10R3 M1060` / `8375-42A`, or for any other live-verified envelope — that
envelope is ADR 0056's live SR-IOV capture, which covers SR-IOV projections, not `io_slots`.
ADR 0053 also states that a field admitted for one family cannot be assumed present in
another.

So the arm's first mutating run issues a grammar with no admitted evidence for the family it
runs on. That is the risk this arm exists to retire, and the gate does not remove it: it
**bounds** it, to the one release/model pair the repository has live-verified for anything,
where an operator is already running the SR-IOV arm and where the recorded before/after
strings become the evidence ADR 0053 asks for. Without the gate the same unprobed grammar
reaches an arbitrary Power8, Power10 or Power11 system as a real profile mutation instead of
the SKIP criterion 5 requires.

Then call `hmc_list_dedicated_pcie_slots` for the configured system. Select the configured
`drc_index`, or the first slot whose `owner_lpar` is empty when none is configured. SKIP the
arm — never PASS — when the inventory read fails, when no slot is unassigned, or when a
configured `drc_index` is absent from the inventory or already owned. There is deliberately
**no** `capability == "capability-unavailable"` branch: `operations/pcie.py:list_dedicated_slots`
returns the literal `"available"` unconditionally, so such a branch could never execute and
would advertise a SKIP path that does not exist. A failing read is already covered above.

**ST30 — create the run-unique owner-stamped fixture.** First call `hmc_create_lpar` for
`probe_lpar_name` with a non-empty `assignments.dedicated`, to record the create-time
capability boundary; `prevalidate_lpar_pcie_assignments` refuses this before
`create_and_stamp_lpar` runs, so nothing is created and it is recorded as SKIP via
`RunState.record_expected_or_real`. That refusal is the *only* reason nothing is created,
and it is exactly the gate this arm's evidence exists to lift — so the probe does not
assume it. A non-refusing outcome sets `probe_created`, records a `MANUAL RECOVERY
REQUIRED:` row naming the partition, and cleanup deletes it under the same caller-token
comparison. The skip reason states the refusal, not "no partition was created", which stops
being true on the day the gate lifts and is at that moment no longer printed either.

Then call `hmc_create_lpar` with no assignments and `caller_token=<run_marker>`. Capture the
UUID — from `result["lpar"]["UUID"]`, falling back to `hmc_get_lpar` because some firmware
answers HTTP 201 with an empty body — and read the exact baseline `io_slots` value from the
new profile. A failed create ends the arm before any mutation.

**No identity, no mutation.** The arm records the reason and proceeds directly to cleanup,
assigning nothing, when *either* half of the identity Guard A checks is missing:

- the UUID cannot be resolved after both attempts; or
- `hmc_create_lpar` reports `ownership_stamped` as anything other than `True`. Per
  `server_tools/lpar/lifecycle.py`, `False` means the ownership stamp and the caller
  segment were both lost and `None` means the stamp was skipped — either way the partition
  carries no `[caller <run_marker>]` segment, so `parse_lpar_ownership_caller_token` returns
  `None` and Guard A refuses **every** cleanup mutation. Mutating hardware after that point
  does not risk the stranded-slot outcome, it guarantees it.

The fixture is then removed on the run-unique caller token and the fixture name alone,
which is the only identity that exists — and it is never the identity under which hardware
was mutated, because no hardware was. Keeping the two apart is why this is a separate rule
and not a weakened Guard A. (When the ownership stamp is the missing half, the token is
absent too, so Guard A correctly refuses even that delete and emits recovery evidence for a
partition carrying no hardware — the safe direction.)

**ST31 — assign on the existing LPAR.** Call `hmc_assign_dedicated_pcie_slot` and record its
capability refusal as SKIP. Then issue the documented profile grammar,
`chsyscfg -r prof -m <system> -i "name=<profile>,io_slots+=<drc>//0,lpar_name=<lpar>"`, and
record the exact `io_slots` value the profile then holds as `applied_io_slots`.

**The readback runs whether the mutation reported success or failure.** `RunState.call`
returns `FAIL` for any raised exception, and `ssh/transport.py` raises `HMCCLIError` when
the SSH timeout expires — after the HMC has already executed `chsyscfg`. Returning early on
a `FAIL` without reading back is how the run would come to believe it had not written
something it had. So the readback and the `applied_io_slots` capture happen first, and only
then does a non-`PASS` command status end the step. There is deliberately no
`slot_assigned`-style flag on the fixture at all: a field recording what the run believes it
did is exactly the input Guard B must not take, and a field nothing reads is a standing
invitation to gate on it.

**ST32 — verify readback.** The profile's `io_slots` must contain the DRC index. The
dedicated-slot inventory owner is recorded as an informational row, mirroring the SR-IOV
arm's treatment of the effective layer: a profile-only assignment is not expected to change
effective inventory for a partition that has never activated.

**ST33 — unassign, verify restoration, reassign.** Issue `io_slots-`, require the profile's
`io_slots` to equal the exact `baseline_io_slots` captured at ST30 — exact restoration, not
merely the absence of the DRC index — then reassign and verify again. Each phase runs only
if the previous one passed; a skipped phase is recorded as SKIP with the reason.

**ST34 — cleanup.** Always runs. It is the safety boundary and is specified in full below.

## Cleanup contract

Cleanup performs at most three mutations: delete the create-time probe partition when one
exists, then remove the slot from the fixture profile, then delete the fixture. Before
**each** of them it re-reads live state and compares it exactly against what this run
recorded. Every refusal records a FAIL row whose tool label names the guard and whose data
begins `MANUAL RECOVERY REQUIRED:` and names the exact command an operator must run, and
then stops that line of cleanup without attempting any further mutation on it.

The probe partition is a **different partition with its own identity**, so it is handled
first and independently, and it gets the same two rules the fixture gets rather than a
weaker pair. Its own caller token is read and compared to this run's marker; the slot is
removed from *its* profile and the removal confirmed before it is deleted; and the delete
names the UUID its create returned, falling back to its name only when none was. A refusal
at any of those records recovery evidence and stops the probe's cleanup **without**
preventing the fixture's. Guards A, B and C below govern the fixture only.

The hardware-first rule matters most here, not least: on the gate-lifted path the probe is
the one partition in the whole arm that *provably* holds the dedicated slot at cleanup time,
because the probe succeeded only by applying the assignment. Deleting it without removing
the slot is precisely the stranding this design forbids everywhere else.

**Guard A — fixture identity, checked before either mutation.** Re-read the LPAR UUID via
`hmc_get_lpar` and the ADR 0064 caller token from `hmc_get_lpar_description`, parsed with
`parse_lpar_ownership_caller_token`.

- Caller token differs from `fixture.run_marker` → refuse both mutations.
- A UUID was captured at ST30 and the re-read UUID differs from it → refuse both mutations.
- Either value unreadable → refuse both mutations. An unreadable identity is a mismatch, not
  a pass; this is the direction the guard has to fail in.

The one asymmetry is deliberate: when ST30 never resolved a UUID, ST31–ST33 did not run, so
there is no assignment to remove and the token comparison alone governs the delete.

**Guard B — slot removal, decided on live state.** The branch is entered when a baseline was
captured (`fixture.baseline_io_slots is not None`) **and** the `io_slots` value just re-read
differs from it. It is *not* gated on `fixture.slot_assigned`.

That distinction is the whole guard. `slot_assigned` is a local belief, and it is false on
three reachable paths where the profile really does carry the DRC index: an assign whose
`chsyscfg` applied but whose response was lost to a timeout; an assign that succeeded while
its confirming `lssyscfg` failed; and a reassign — the last mutation before cleanup — in
either of those two states. On each of them a flag-gated Guard B skips removal entirely and
Guard C deletes a partition with a slot still assigned to it. The false-positive direction
was defended and the false-negative direction, the one that destroys state, was not.
Reading live state removes the whole class, because the profile is the fact and the flag is
only a memory of it. An unreadable value (`None`) is not equal to the baseline either, so it
enters the branch and is refused there — the safe direction.

Inside the branch:

- The current `io_slots` must equal `fixture.applied_io_slots` exactly. Anything else —
  including `None`, and including the case where `applied_io_slots` was never set because the
  confirming read failed — means the arm cannot prove the deviation is its own → refuse, and
  do not delete the LPAR either.
- On an exact match, issue `io_slots-`, then re-read. The value must equal
  `fixture.baseline_io_slots`. A removal that reports success but does not restore the
  baseline → refuse to delete the LPAR, and record recovery evidence.

When no baseline was captured, ST31–ST33 never ran (`create_dedicated_fixture` returns
`False` without one), so there is nothing to compare and nothing to remove; the delete is
governed by Guard A and Guard C alone.

**Expected on the first live run: Guard B may refuse for a benign reason.** Nothing in this
repository establishes that `lssyscfg -r prof -F io_slots` — a list-valued attribute — is
byte-stable across an add/remove round trip on a profile that already holds slots; ADR 0053
records that readback as not yet admitted, and there is no captured sample. If the HMC
re-renders or reorders the value, the post-removal string will not equal the baseline and
Guard B will refuse the delete. That is the safe direction and a real result, not a bug: the
refusal row prints the baseline and the post-removal value side by side, which answers the
stability question on the first run, and the captured before/after pair is the ADR 0053
evidence either way. Do not loosen the comparison to avoid it — a comparison that tolerates
re-rendering also tolerates the third-party drift Guard B exists to catch.

**Guard C — LPAR deletion.** Reached only after Guard A passed and either Guard B was not
entered or it completed and observed the restored baseline. Re-read the UUID and caller
token once more, immediately before the delete, and require both to still match. The delete
is `hmc_delete_lpar(system_name_or_uuid=<configured system>,
lpar_name_or_uuid=<fixture UUID, falling back to the fixture name only when no UUID was ever
resolved>)`. Two properties, both deliberate:

- **Act on the identity that was verified.** Guard C proves that the partition currently
  named `fixture.lpar_name` has UUID `fixture.lpar_uuid`; deleting by name then re-resolves
  that name, reopening the window the guard just closed. `hmc_delete_lpar` accepts either
  form, so acting on the UUID costs nothing and closes it.
- **The profile is compared too, not just the identity.** `_read_dedicated_state` has already
  fetched `io_slots` for this check, so requiring it to still equal the captured baseline is
  free — and without it, "the delete is refused while the profile differs from the baseline"
  is enforced only at Guard A's earlier read. A slot added by a concurrent HMC session after
  that read would otherwise be stranded by this delete, and on the never-assigned path Guard
  A's read is the *only* profile observation, so the unguarded window is the whole of Guard C.
- **`ownership_override` stays off.** The fixture was created and stamped by this run, so the
  tool's own description-token ownership check passes on every intended path; setting the
  override would remove the one operation-layer check that survives the escape hatch —
  precisely the check ADR 0163 argues the fixture guards exist to *replace*, not to
  duplicate and then disable. The same reasoning drops `ownership_override=True` from the
  `hmc_assign_dedicated_pcie_slot` refusal row, so the recorded refusal is the one an
  operator would actually see.

The probe partition, when `fixture.probe_created` is set, is handled before any of this,
as described above.

Hardware before partition is the ordering the issue requires, and Guard B's refusal is what
makes the ordering binding: a delete that ran first — or that ran while the profile still
differs from the baseline — would leave a slot recorded against a partition that no longer
exists, which is exactly the un-reusable pool state the issue's downstream consumer cannot
tolerate.

## Testing

`tests/scripts/test_pcie.py`, new, following the AGENTS.md convention of one test module per
`scripts/` file and the `ScenarioState` seam already used by `tests/scripts/test_inventory.py`.

The seam is extended in three ways the inventory one does not need: a status map whose value
may be a plain status **or a callable over `(kwargs, call_index)`**, so a test can fail one
call of a tool while later calls of the same tool succeed; retention of the ordered call
list, so ordering and non-occurrence can be asserted; and a mutable `io_slots` model behind
`hmc_run_command`, so a `chsyscfg` in the sequence actually changes what the next `lssyscfg`
reads.

The per-call form is not a convenience. A run-wide status cannot express two of the
scenarios below: `hmc_create_lpar` is called twice (probe, then fixture) and
`hmc_run_command` carries both the mutations and every `lssyscfg` read, so a run-wide `FAIL`
on either tool aborts the arm at a strictly earlier step than the one the test is named for
and the assertion passes over the wrong path.

The guards are what the tests must bite on. Each of these asserts both the recorded outcome
**and** that the forbidden mutation does not appear in the call list:

1. Missing required configuration → the arm SKIPs and issues no tool call at all.
1b. **A configured value carrying an HMC record delimiter** (`,`, `=`, `"`, `[`, `]`, `\`, or
   a control character) in the LPAR prefix, profile name, or DRC index → the arm SKIPs with
   zero tool calls. This is the guard that keeps a `build_filter` / `build_attribute_record`
   raise — which happens at command-construction time, in the arm's own frame, outside
   `RunState.call`'s try/except, and first occurs *after* the fixture partition exists —
   off a path where a partition has already been created.
2. **Environment outside the admitted envelope** — `lshmc -V` reports a different release, or
   the type-model is not `8375-42A`: SKIP for the arm, no LPAR is created, and no `chsyscfg`
   is issued.
3. Inventory with no unassigned slot → SKIP for the arm, and no LPAR is created.
4. A configured `drc_index` that inventory reports as owned → SKIP, no LPAR created.
5. The create-time dedicated request's capability refusal is recorded SKIP, not FAIL or
   PASS, and the arm still proceeds to create the unassigned fixture. This is the **default**
   scenario for every other test: today ADR 0055 refuses the probe, so no probe partition
   exists and cleanup performs exactly one delete.
6. **The create-time probe unexpectedly succeeds** — the probe create returns PASS:
   `probe_created` is set, a `MANUAL RECOVERY REQUIRED:` row names the probe partition, and
   cleanup reads that partition's own caller token, removes the slot from its profile,
   confirms the removal, and only then deletes it on its captured UUID — the whole sequence
   before the fixture's delete.
6b. **The probe partition carries a foreign caller token** — its description does not name
   this run's marker: no `io_slots-` and no delete names the probe, the
   `probe run-marker mismatch` row is FAIL with recovery evidence, **and the fixture is
   still cleaned up**. The probe is a different partition with its own identity, so its
   refusal must not block the fixture's cleanup.
7. The existing-LPAR `hmc_assign_dedicated_pcie_slot` refusal is recorded SKIP, and the arm
   still proceeds to the profile grammar.
8. Happy path: `io_slots+` then `io_slots-` restores the exact baseline, reassign succeeds,
   cleanup removes the slot and deletes the LPAR, the removal command precedes the delete
   call in the recorded order, exactly one delete is issued, and it names the fixture's
   **UUID** with no `ownership_override`.
9. **Guard A, UUID drift** — the `hmc_get_lpar` read at cleanup reports a different UUID than
   the one captured at ST30: no `chsyscfg` and no `hmc_delete_lpar` in the cleanup phase, and
   the `uuid mismatch` row is recorded FAIL with recovery evidence.
10. **Guard A, foreign caller token** — the token read back is not this run's marker: same
    assertions against the `run-marker mismatch` row.
11. **Guard A, unreadable identity** — the description read fails: same assertions.
12. **Guard B, profile drift** — `io_slots` at cleanup is neither the applied value nor the
    baseline: no `io_slots-` and no delete in the cleanup phase, `profile drift` recorded.
13. **Guard B, removal that does not restore** — `io_slots-` reports success but the
    re-read does not equal the baseline: no `hmc_delete_lpar` in the cleanup phase.
14. **The assign's response was lost** — the `chsyscfg` applied and then reported `FAIL`.
    `assign_dedicated_slot` must read back anyway, so `applied_io_slots` records what the
    profile holds and cleanup can prove the deviation is its own: it issues `io_slots-` and
    only then deletes, in that order. Restore an early `return` before that readback and the
    arm can no longer prove it, so cleanup refuses the delete and this test goes red.
15. **The confirming read was lost** — ST30's baseline read succeeds and every profile read
    after it fails, so the assign applies while `applied_io_slots` is never set. Guard B must
    still enter, on the baseline comparison, and refuse: no delete, `profile drift` recorded.
    A Guard B gated on `applied_io_slots is not None` skips the branch and deletes.
16. **Guard C, identity drift between removal and delete** — identity matched at Guard A and
    the removal succeeded, but the re-read before the delete reports a foreign token: no
    `hmc_delete_lpar` in the cleanup phase. The happy path makes exactly three description
    reads — ST32's, Guard A's, Guard C's — so the foreign one must be the third; a fixture
    that only turns foreign on a fourth read leaves Guard C entirely uncovered.
16c. **Guard C, profile drift between the last check and the delete** — identity still
    matches but the profile read Guard C already performs no longer equals the baseline: no
    `hmc_delete_lpar`, and `profile changed before delete` is recorded. This is what makes
    "the delete is refused while the profile differs from the captured baseline" true *at*
    the delete rather than only at Guard A's earlier read.
17. Nothing was ever assigned — the profile still equals the baseline at cleanup: no
    `chsyscfg` is issued in cleanup, and the LPAR is still deleted (the fixture is this run's
    to remove).
18. A run marker is unique per invocation, and the fixture LPAR name carries the configured
    prefix.
19. **No identity, no mutation (UUID)** — create returns no LPAR body and `hmc_get_lpar`
    resolves nothing: no `chsyscfg` is issued at all, and the fixture is still deleted on the
    token.
20. **No identity, no mutation (ownership stamp)** — create returns `ownership_stamped=False`
    and, consistently with what that value means, a description carrying no caller segment:
    no `chsyscfg` is issued at all, **and** Guard A refuses the delete with recovery
    evidence — which is what the ST30 note above claims happens on this path.

Two properties make these bite rather than merely run, and both are seam requirements, not
assertion style. First, the "forbidden mutation did not happen" assertion is scoped to the
**cleanup phase**: the arm legitimately issues an `io_slots-` at ST33 before any guard runs,
so a run-wide command list cannot express it and the assertion the whole set depends on
would be unwritable. Second, recovery evidence is asserted by the **specific guard row's**
tool label, never by scanning every row for the substring `MANUAL RECOVERY REQUIRED` — ST30's
probe row can carry that text too, and an assertion it pre-satisfies cannot distinguish a
guard that refused from one that did not.

**Confirm the tests bite, one guard at a time.** Introduce each controlled fault, observe the
named tests go red, then revert:

| Fault | Tests that must fail |
|---|---|
| Guard A's identity comparison → `if False:` | 9, 10, 11, 20 |
| `assign_dedicated_slot` returns early on a non-`PASS` command, before its readback | 14 |
| Guard B's entry → `if fixture.applied_io_slots is not None:` | 15 |
| Guard B's `applied_io_slots` exact-match → `if False:` | 12, 15 |
| Guard B's post-removal baseline check → `if False:` | 13 |
| Guard C's identity comparison → `if False:` | 16 |
| Guard C's baseline comparison → `if False:` | 16c |
| `_admit_dedicated_environment` → always return `True` | 2 |
| `_config_value_safe` → always return `True` | 1b |
| `_cleanup_probe_partition`'s token comparison → `if False:` | 6b |
| `_cleanup_probe_partition`'s slot removal → skipped | 6 |

A guard whose neutralization reddens nothing is not covered, whatever its tests appear to
assert.

## Trust boundaries

The arm is an operator-run script, not a served surface, and it adds no entry point an
untrusted actor can reach. Two boundaries are worth stating:

- **Command construction from non-literal values.** `system_name`, `lpar_name`,
  `profile_name`, and `drc_index` reach `chsyscfg` and `lssyscfg` strings. `system_name` is
  passed through `shlex.quote`. **Both** HMC-side structures are built with the existing
  helpers, and neither is hand-formatted: the `-i` profile record with
  `hmcpctl.documents.native_records.build_attribute_record` (the helper
  `ssh/profiles.py:_change_profile_io_slot` uses) and the `--filter` expression with
  `hmcpctl.documents.native_records.build_filter` (the helper
  `ssh/profiles.py:read_lpar_profile_record` uses). `shlex.quote` does not substitute for either:
  `native_records.py` states outright that the
  two mechanisms protect different layers — the remote shell versus the HMC's own record
  parser, which runs afterwards on the already-unquoted text — "and neither substitutes for
  the other". `profile_name` reaches the filter straight from `HMC_LIVE_PCIE_PROFILE` with
  only `.strip()` applied, so a comma or `=` in it would otherwise rewrite the filter
  silently and the read would answer about a different profile than the one being mutated,
  while every downstream exact-match comparison in Guard B still reported success.

- **Ambiguous reads are refused, not averaged.** `_read_profile_io_slots` requires the
  `lssyscfg` output to be exactly one non-empty line, the same single-record guard
  `ssh/profiles.py:read_lpar_profile_record` applies ("lssyscfg profile capture expected
  exactly one record"). A multi-record answer means the filter selected more than the arm
  named, and the guards compare exact strings; silently taking the first line would hand
  them a value from a profile nobody chose.
- **Operator-supplied configuration.** The four environment variables are supplied by the
  operator running the script, who already holds HMC credentials; they are a configuration
  boundary, not a trust boundary. `drc_index` is nonetheless compared against the inventory
  before use, so a typo SKIPs rather than reaching a command.

Out of scope: the escape hatch's own authorization (unchanged, and already opted into by the
runner), and SSH transport host-key handling (issue #605).

## Out of scope

The SR-IOV arm; any change to `src/`; lifting ADR 0055's gate; running against hardware;
`src/hmc_mcp/ssh/transport.py`.
