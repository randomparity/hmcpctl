# 0326 — Provision boots the installer once and polls refcodes against a journaled baseline

## Status

Accepted (2026-11-03), issue #1849 (part of #1230). Fills in ADR 0191 Decisions 1–2 and the H1 spec's
steps 8–9 for `install`. ADRs 0313 and 0319 stand: this record adds effects after ADR 0319's
`boot_order` effect and changes nothing before it.

## Context

- ADR 0319 ends an `install` run at `ready_to_boot` once the boot order is written, and
  `validate_provision_install` refused `boot: immediate` until this issue. The store already
  accepts `continuation: boot` on a `ready_to_boot` operation (`operations/logical/store.py`
  `accepted_continuations`), and the engine already ends a `boot_started` outcome `terminal`.
- `hmc_read_lpar_refcodes` reads `lsrefcode -r lpar` newest first. Its `time_stamp` carries no
  zone (`tests/unit/test_ssh_refcodes.py`). Whether codes continue after `Running`, and which codes
  mean the installer started, are unrecorded; #1850's native arms record them.
- Provision's `power_on` effect (ADR 0313) already activates the saved profile through
  `submit_power_on_lpar`, and its classifier settles an interrupted power-on from the partition
  state and `LastActivatedProfile` without resubmitting.
- No hold store exists yet (#1851). The operation's own partition guard stays held while paused
  (ADR 0190).
- #1842 left two native questions to this issue: the REST0269 behaviour of the mount on a
  provision-created partition, and whether the optical adapter in the saved profile survives the
  activation.

## Decision

1. **When the boot runs.** After the `boot_order` effect, the run boots when `boot` is `immediate`,
   when the call is `continuation: boot`, or when an earlier call of the operation already recorded
   the baseline (a `resume` after a boot wait). Otherwise it pauses `ready_to_boot` as before.
2. **Revalidation.** While no `power_on` effect is recorded, the run first re-reads and requires:
   - the partition `Not Activated`;
   - the media still in the repository, and mapped to the partition once, at the recorded device
     path;
   - `PendingBootString` equal to the recorded boot order;
   - exactly one client network adapter on the VLAN, carrying the recorded MAC that the media binds;
   - across every VIOS of the system, exactly one non-optical mapping to the partition, and it is
     the operation's own root disk.

   Any difference ends the run `needs_attention` with step `revalidate_boot` naming it, and nothing
   is powered on. The hold check joins with #1851.
3. **Baseline.** Effect `refcode_baseline`, kind `lpar.refcode_baseline`, target `lpar:<uuid>`,
   identity `{time_stamp, refcode}` of the newest row, both `null` when the HMC lists none. It is a
   read journaled before the power-on (H1 *Intent before writing*). Its classifier answers
   `not_applied`: the power-on is recorded only after the baseline settles, so an open baseline
   proves no power-on was attempted and re-reading is safe.
4. **Power-on.** ADR 0313's `power_on` effect, unchanged in key, kind, classifier and write, activates
   the saved profile. It is never resubmitted: a recorded `power_on` is replayed or classified, so the
   installer power-on happens at most once per operation.
5. **Polling.** Step `wait_boot_started` reads the newest 20 rows, then again every 15 seconds
   until the call's `wait_seconds` has passed, at least once. A row is newer than the baseline when it
   precedes the baseline's row in the HMC's own order (or the baseline is absent from the read) and,
   where both timestamps parse as the HMC's `MM/DD/YYYY HH:MM:SS`, its timestamp is not earlier than
   the baseline's. hmcpctl's clock is never compared with an HMC timestamp.
6. **Outcome.** A newer row whose code is in the boot-started set ends the run `boot_started`
   (terminal). The set is empty in code until #1850 fills it from native evidence, so today every
   wait ends `needs_attention` with the newer rows it observed (at most 20, `time_stamp` and
   `refcode` each) and the reason. A refcode read that fails ends the wait the same way, naming the
   error. `resume` then re-polls against the recorded baseline.
7. **Authority.** `install` adds `hmc_power_on_lpar` and `hmc_read_lpar_refcodes` to the delegated
   tools (H1 *Delegated authorization*). `continuation: boot` needs `exclusive_writer_window=true`,
   as `resume` does (ADR 0313 Decision 11): the revalidation's reads assume no other writer.
8. **Native proof arm.** Live group `provision-boot` provisions one scratch partition with prepared
   media from the operator's configured ISO, `boot: immediate`, and records the mount's outcome, the
   saved profile's optical slot, the activation from the saved profile, the optical mapping after
   activation, and the observed refcodes. Teardown powers off and removes what the run created. It is
   run only with per-run operator authorization (`docs/live-testing.md`).

## Consequences

- No operation reports `boot_started` until #1850 fills the set. A `boot: immediate` install always
  ends `needs_attention` today, with the partition running the installer media.
- A boot wait spends the call's `wait_seconds` inside the body. The call may return `running` just
  before the body ends; `hmc_operation_status` then reports the outcome.
- A long wait costs one SSH login per read (at most 41 at the 600-second maximum).
- An interrupted power-on stays paused until a human abandons it, as ADR 0313 already accepts.
- `lpar.refcode_baseline` is a persisted effect kind (ADR 0195).

## Considered & rejected

- **Seed the set with codes believed to mean boot.** judgment: fit. ADR 0191 D2 and the issue forbid
  it until native evidence exists.
- **Compare refcode timestamps with hmcpctl's clock.** judgment: fit. ADR 0191 D2 forbids it; the
  zone is unsettled.
- **Order by timestamp alone.** judgment: fit. Two codes in the same second would be unordered; the
  HMC's row order breaks the tie.
- **A separate poll-window input.** judgment: cost. The H1 spec's step 9 names `wait_seconds`, and a
  second bound adds a field no caller asked for.
- **Classify an open baseline by re-reading refcodes.** judgment: fit. A read has no outcome to find;
  the ordering against the power-on is what makes re-reading safe.
- **A new power-on effect for the installer.** judgment: fit. The issue requires reusing the power
  operation, and ADR 0313's effect already activates the saved profile at most once.
- **Skip the revalidation for `boot: immediate`.** judgment: cost. One code path for both timings is
  simpler, and the reads are few.
