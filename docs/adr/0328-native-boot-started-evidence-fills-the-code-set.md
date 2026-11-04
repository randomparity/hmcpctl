# 0328 — Native boot-started windows record the code set and the refcode facts

## Status

Accepted (2026-11-04), issue #1850. Fills ADR 0191 Decision 2's boot-started set and records
its zone handling, and adds the observed VIOS level to ADR 0194 Decision 2's envelope.

## Context

ADR 0191 D2 makes `boot_started` depend on a code in a boot-started set that native proof fills.
It also leaves the refcode timestamp's zone handling to that proof. Parent #1230's amended
acceptance adds two more questions for the native arms:
- does the HMC enforce `If-Match` on VIOS and volume-group writes?
- do refcodes continue after the partition is Running?

ADR 0194 D3 names the two arms, `native-ubuntu-boot-started` and `native-rocky-boot-started`.
ADR 0194 D2 admits a VIOS level into the envelope once an arm records it.

ADR 0326 added provision's installer boot and the provision-boot arm (subtask 72). That arm's
native run on 2026-11-03 has two limits:
- **Wrong media.** It booted a launcher for a different distribution, on documentation
  addresses, so no installer could start. Its codes cannot fill either profile's set.
- **Too few rows.** It read 20 rows, and they were full within about two minutes. All 20 carried
  one HMC timestamp, and that timestamp ran about 1 h 42 min ahead of the runner's UTC.

The producer (iso-chain-loader) builds launchers for both profiles. The launcher:
1. applies the manifest's static IPv4;
2. downloads the pinned installer kernel and initrd from the manifest's `source`;
3. kexecs the installer.

Neither profile has a PowerVM record in the producer's own matrix.

## Decision

1. **One arm, three groups.** The two native groups dispatch subtask 72 with a profile.
   - Each uses the producer's unbound `iso-chain-media-v1` result, sent unchanged as prepared
     media. The arm refuses that result unless it names the group's profile and the configured
     MAC, and describes the configured ISO's own SHA-256 and size.
   - The guest network is the result's own.
   - Memory is 6,656 MiB, the producer's Rocky floor and the larger of the two.
   - The rest of ST72 is unchanged: the run-owned partition and volume, the teardown, and
     recovery.
2. **What is recorded.** After the provision call, the arm records:
   - the VIOS's `os_version`;
   - the HMC's `date`, between two runner UTC reads, before and after the window;
   - every 15 s until 900 s after the first provision call, up to 100 reference codes and the
     partition state.

   Every read is kept whole, newest first. The window bound is the operator's 15-minute
   powered-on limit, counted from before power-on.
3. **`If-Match` probe.** A separate `vg-if-match` window runs ST42 against one scratch
   volume group the run creates on one free physical volume, the operator's chosen target.
   - The shared volume group, its PVs and every LV the run doesn't own stay unwritten. In
     REST, an LV or a medium is only an element of the whole VolumeGroup entity, so no
     smaller run-owned object exists.
   - Between create and cleanup the run GETs the group and POSTs that same body through
     hmcpctl's own VolumeGroup write, under `If-Match: "stale-<16 hex>"`, a tag no ETag ever
     had.
   - It records only the HTTP status, plus an error body's `<Message>` and REST/HSCL codes.
4. **Grounding.** The boot-started set takes only codes these windows observed, after the
   power-on and while the installer media was booting.
   - QEMU runs, the provision-boot arm's other-distribution run, and skipped arms do not count.
   - Raw results stay private; this record carries only codes, orderings and summaries.

## Evidence

Two native windows on 2026-11-04 ran the same boot arm, ST72, unchanged between the two commits.
- **Ubuntu 26.04.1:** grant #26, commit `e2c31395`.
- **Rocky 9.8:** grant #27, commit `62198c15`.
- **Where:** a POWER9 managed system through HMC V10R3 M1060, with the operator's assigned guest
  address.
- **What each did:** booted its producer launcher once, read for about 12 minutes after the
  provision call, and tore down.
- **Recovery:** CLEAN both times. The repository, volume-group and boundary-partition reads taken
  before and after each run were identical.

Raw results stay private. The arm recorded:

| Fact | Ubuntu | Rocky |
| --- | --- | --- |
| VIOS level | 4.1.2.00 | 4.1.2.00 |
| Last codes, oldest first | … CA00E1DC, CA00E14D, CA00E105 | … CA00E1DC, CA00E14D, CA00E105 |
| CA00E105 after the pre-power-on baseline (refcode clock) | 131 s | 125 s |
| Reads after CA00E105, all identical, partition `running` | 40 over 12 min | 41 over 12.5 min |
| Provision call's 120 s wait, newest code when it ended | CA00E130 | CA260200 |
| HMC `date` vs runner UTC | UTC, about 14 s ahead | UTC, about 14 s ahead |
| Refcode `time_stamp` vs HMC `date` | about 1 h 44 min ahead | about 1 h 44 min ahead |

The firmware sequence before the handoff was the same in both windows:
- `C20082xx`, then a blank row;
- `CA26xxxx` and `CA00E8xx`;
- `CA00E13x`/`E44x`;
- `CA00E1xx`/`D0xx`;
- `CA00E1DC` (firmware waiting for a console, passed without one);
- `CA00E14D`, "Load boot image";
- `CA00E105`, "Transfer control to the operating system (normal boot)".

Both meanings are from `docs/refs/ibm-p9-progress-codes-2021.md`.

Many rows share one timestamp to the second. A passive ping of the Rocky guest's address never
answered while its gateway did. That is inconclusive: host ICMP may be filtered. Without a
console, nothing shows the distribution installer itself running.

## Findings

1. **The boot-started set is `{CA00E105}`.**
   - It is the only code that marks firmware handing control to the boot image, and both
     profiles posted it.
   - Before power-on, revalidation makes the installer medium first in the boot order and the
     blank owned disk the only disk. So the image that firmware hands control to is the
     launcher.
   - `boot_started` therefore means that the installer medium's boot image started. It does not
     mean the installer ran.
2. **No codes after Running.** The launcher, its kexec and the installer post no reference codes.
   CA00E105 stays the newest row for as long as the partition runs. So a `resume` after a short
   wait still finds it.
3. **The timestamp is not the HMC's clock.**
   - The refcode `time_stamp` runs on a clock about 1 h 44 min ahead of the HMC's own UTC
     `date`, most likely the managed system's.
   - It also repeats to the second.
   - ADR 0191 D2's rule therefore stands as the zone handling: order by HMC row, compare
     timestamps only with the baseline row from the same read source, and never compare them
     with any host clock.
4. **`wait_seconds`.**
   - CA00E105 came about 130 s after power-on, and both provision calls' 120 s waits ended first.
   - The wait counts from the call's start, so reaching `boot_started` in one call needs time
     for creation and upload plus about 130 s. A shorter call ends `needs_attention`, and
     `resume` polls again without a second power-on.
   - The arm now passes 300 s.
5. **VIOS 4.1.2.00** enters ADR 0194 D2's envelope as the observed VIOS level.
6. **`If-Match` on a VolumeGroup write: not yet observed.** The `vg-if-match` window (grant #28)
   stopped before any HMC contact, because the runner host had lost its route to the HMC. Until
   it runs, hmcpctl's VolumeGroup writes keep sending `If-Match` without knowing whether the HMC
   enforces it. VIOS (mapping) writes are not probed: no run-owned VIOS object exists to write.

## Consequences

- The windows ran before the set was filled. The `boot_started` path itself, with a 300 s wait,
  has not yet run on hardware; the next native run of either group exercises it.
- `BOOT_STARTED_CODES` is `{"CA00E105"}`. ST72's wait assertion becomes
  `boot-wait-ended-truthfully`: `boot_started` on a set code, or `needs_attention` with the codes
  it saw.
- Another firmware level, POWER10, or another HMC could post different codes. Adding one takes new
  native evidence, not this record's inference.
- Each window powers a partition on the operator's assigned guest address. The test LPAR that
  normally holds that address is never activated, and the arm skips unless it reads Not
  Activated, so the two cannot be up at once.

## Considered & rejected

- **Seed the set from the progress-code reference.** verified: ADR 0191 D2 and #1849 forbid a
  guessed set. judgment: a code IBM documents is not proof that this firmware and launcher emit
  it.
- **Use the provision-boot run's codes.** verified: that run booted another distribution's
  launcher on documentation addresses. judgment: its codes end before any installer starts.
- **Separate arm modules per distribution.** judgment: fit. The arms differ only in their
  profile and media, which configuration already carries.
