# 0317 — Optical media load and unload run the VIOS loadopt/unloadopt commands through viosvrcmd

## Status

Accepted (2026-11-03), issue #1285. Grounded in the #1284 live capture (V10R3, POWER9), whose
fixtures and answers are in `tests/fixtures/live/README.md` (capture prefix `2026-11-02-q1284/`).

## Context

- `hmc_mount_optical_media` creates a mapping and `hmc_unmount_optical_media` removes it. Nothing
  changes the medium in a mapped file-backed virtual optical device; operators run `viosvrcmd`
  `loadopt`/`unloadopt` by hand, sometimes with `-release` to drop a stale client reservation
  (#1280).
- The capture found two working paths on V10R3 (README Q1, Q2):
  - a read-modify-write `ViosSCSIMapping` POST whose only change replaces the mapping's
    `Storage` with another `VirtualOpticalMedia` changes the loaded medium in place
    (`rest-vios-scsi-mapping-replace-storage`, then `cli-vios-lsvopt-after-storage-post`);
  - `viosvrcmd … -c 'unloadopt -vtd V'` and `'loadopt -disk M -vtd V'` exit 0 and print `:`
    (`cli-vios-unloadopt`, `cli-vios-loadopt`), with `lsvopt` reading `No Media` between them.
- The command syntax is `loadopt [-f] [-release] -disk FileName -vtd VirtualTargetDevice` and
  `unloadopt [-release] -vtd VirtualTargetDevice` (`cli-vios-help-loadopt`,
  `cli-vios-help-unloadopt`).
- The issue requires load, unload without detaching the mapping, and an explicit release option.
- The SSH transport already runs HMC commands for many operations (`ssh/transport.py`,
  `run_hmc_command`), and `viosvrcmd` is an HMC command.

## Decision

1. **Transport.** Load and unload each submit one `viosvrcmd -m <system> --id <VIOS partition id>
   -c '<loadopt|unloadopt> …'` through `run_hmc_command`. A thin builder in a new
   `ssh/vios_optical.py` composes the command; no new transport or general VIOS command runner is
   added.
2. **Tokens are refused, never quoted into the VIOS shell.** The medium name must match the
   repository rule `upload_iso` already enforces (`[A-Za-z0-9_.]{1,79}`) and the device name the
   VIOS device-name shape (`[A-Za-z0-9_]{1,15}`); the inner command therefore needs no quoting, and
   only the whole `-c` argument is `shlex.quote`d for the HMC shell.
3. **Release is explicit and audited.** `release` defaults to false and adds `-release`. When true,
   an `optical-release` audit record is emitted before the command is submitted. `-f` is not
   exposed: loading into a device that holds a medium is refused by the VIOS; unload first.
4. **Authorization and binding.** The client LPAR passes the ADR 0092 ownership rule, and the
   virtual target device must be the `VirtualOpticalTargetDevice` of a mapping on that VIOS whose
   client is that LPAR, read from the VIOS mapping group whether or not a medium is loaded.

## Consequences

- One transport covers load, unload and release. The VIOS must be running with an active RMC
  connection for `viosvrcmd`; otherwise the HMC's refusal is returned.
- The REST `Storage`-replace path stays uncaptured on V11R2 and unused; nothing here depends on it.
- The `-release` form is unexercised against a real stale reservation: the capture's partition
  had no operating system to issue one (README Q3).
- Media names outside the repository rule cannot be loaded by this tool, though the VIOS may
  accept them.

## Considered & rejected

- **REST `Storage`-replace POST for load and unload.** verified: the #1284 capture set
  (`tests/fixtures/live/README.md`, Q1) records only a replace, no POST that empties a mapping's
  `Storage` without removing it, and the replaced mapping body
  (`rest-vios-scsi-mapping-replace-storage.json`) carries no reservation-release field, so neither
  unload-without-detach nor release is evidenced over REST.
- **REST for load, `viosvrcmd` for unload and release.** judgment: two transports for one device,
  each with its own failure handling, where one already covers all three actions.
- **A CLIRunner REST job running the same VIOS command.** verified: README Q2 records it as not
  exercised; judgment: a second transport for the same command, while the SSH one is captured.
- **Do nothing; operators keep using `hmc_run_command`.** verified: `configure_arbitrary_command_tool`
  registers that tool only when enabled (`server_tools/command.py`); judgment: it bypasses the
  ownership rule and records no release.
- **Quote the medium and device names inside the `-c` string.** judgment: how the VIOS restricted
  shell parses quotes inside `viosvrcmd -c` is uncaptured; refusing unsafe names is decidable
  offline.
