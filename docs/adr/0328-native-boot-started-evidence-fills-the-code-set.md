# 0328 — Native boot-started windows record the code set and the refcode facts

## Status

Proposed (2026-11-03), issue #1850. Amends ADR 0191 Decision 2 and ADR 0194 Decision 2 once
the native windows below have run.

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
3. **Grounding.** The boot-started set takes only codes these windows observed, after the
   power-on and while the installer media was booting.
   - QEMU runs, the provision-boot arm's other-distribution run, and skipped arms do not count.
   - Raw results stay private; this record carries only codes, orderings and summaries.

## Consequences

- Until both windows run, `BOOT_STARTED_CODES` stays empty and every boot ends `needs_attention`.
- Each window powers a partition on the operator's assigned guest address. The test LPAR that
  normally holds that address is never activated, so the two cannot be up at once.

## Considered & rejected

- **Seed the set from the progress-code reference.** verified: ADR 0191 D2 and #1849 forbid a
  guessed set. judgment: a code IBM documents is not proof that this firmware and launcher emit
  it.
- **Use the provision-boot run's codes.** verified: that run booted another distribution's
  launcher on documentation addresses. judgment: its codes end before any installer starts.
- **Separate arm modules per distribution.** judgment: fit. The arms differ only in their
  profile and media, which configuration already carries.
