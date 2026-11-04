# 0194 — Ubuntu 26.04.1 LTS and Rocky Linux 9.8 are the initial installation profiles

## Status

Accepted (2026-10-01), issue #1216. Selection made by the operator on 2026-10-01 from the
evidence below. ADR 0328 records Decision 3's two arms and the VIOS level they observed, which
enters Decision 2's envelope.

## Context

Epic #1215 requires one POWER-compatible release each of Ubuntu and Rocky. The producer's
first native work targets POWER9 (iso-chain-loader#6, #8). hmcpctl's live vocabulary fixtures
span V10R3 on POWER8/9 and V11R2 on POWER9/10/11. The verified HMC is V10R3 M1060
(ADR 0177). No VIOS level is recorded anywhere in the repository.

Evidence observed on 2026-10-01:

- `https://changelogs.ubuntu.com/meta-release` lists 24.04.5 and 26.04.1 LTS as supported.
  `https://ubuntu.com/download/server/power` gives POWER9 as the floor since 22.04 and names
  26.04 the recommended Power release. 26.04 standard support runs to May 2031.
- Launchpad bug 2142695 (26.04 ISO boot on PowerVM POWER9/POWER10) was fixed in grub2
  2.14-2ubuntu2 on 2026-03-06.
- The Rocky 9 release notes (`https://docs.rockylinux.org/10/releases/release_notes/9_0/`) and
  the RHEL 9.8 notes give POWER9 as the floor. The Rocky 10.1 and
  RHEL 10.2 notes give POWER10. The Rocky wiki gives Rocky 9 active support to 2027-05-31 and
  security support to 2032-05-31.

## Decision

1. **Profiles.** `ubuntu-26.04.1` (`ubuntu-26.04.1-live-server-ppc64el.iso`, checked against
   `cdimage.ubuntu.com/releases/26.04/release/SHA256SUMS`) and `rocky-9.8`
   (`Rocky-9.8-ppc64le-dvd.iso`, checked against the folder's `CHECKSUM`). Installer sources
   are pinned by digest in the producer's profile. hmcpctl accepts these two profile names
   and refuses any other.
2. **Native envelope.** PowerVM partitions on POWER9 and POWER10 managed systems, through an
   HMC at V10R3 M1060 or V11R2, are in scope. A VIOS level enters the envelope once a native
   arm records it. Until then the envelope names none, and planning reports the VIOS level as
   unverified.
3. **Proof arms.** Two arms, `native-ubuntu-boot-started` and `native-rocky-boot-started`,
   each separately authorized under `docs/live-testing.md` against the deployed commit. QEMU
   runs and skipped arms are not native evidence.

## Consequences

- Rocky 9 leaves active support on 2027-05-31. A POWER10-only follow-on to Rocky 10.x
  needs its own record and arm.
- Adding a profile is a producer change plus one accepted name here. It is not a hmcpctl
  code path per distribution.

## Considered & rejected

- **Ubuntu 24.04.5.** judgment: fit. Supported, but 26.04 is Canonical's recommended Power
  release and is supported two years longer.
- **Rocky 10.2.** verified: `https://docs.rockylinux.org/10/releases/release_notes/10_1/` and the
  RHEL 10.2 release notes overview list POWER10 as the ppc64le floor (observed 2026-10-01). The
  POWER9 lab could not prove it.
- **Accept any distribution the producer offers.** judgment: fit. Each profile needs its own
  native proof before it is supported.
