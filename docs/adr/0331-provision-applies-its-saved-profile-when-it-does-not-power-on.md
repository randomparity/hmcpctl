# 0331 — Provision applies its saved profile when it does not power on

## Status

Accepted (2026-11-04), issue #1854. Amends ADR 0313: a run that does not power on gains one
effect. Records a consequence for ADR 0314 Decision 2.

## Context

ADR 0313 has provision save the current configuration as a new profile `hmcpctl_<12 hex>`,
make it the default, and, with `power_on`, activate it. A REST create sets the partition's
current configuration without activating anything, and leaves a non-empty
`LastActivatedProfile` naming the creation profile.

ADR 0314 Decision 2 refuses `hmc_reconfigure_lpar` when `LastActivatedProfile` differs from
`DefaultProfileName`: a write to one profile would not persist through activation of the other.

So `hmc_provision_lpar` with `power_on=false` ended `configured` on a partition that
`hmc_reconfigure_lpar` then refused before any write. Observed natively on 2026-11-04 (V10R3,
POWER9). The logical-workflow arm's reconfigure ended `failed` in phase `validating` with that
refusal.

A follow-up capture the same day, on one owned scratch partition provisioned with
`power_on=false`, read:
- **after provision:** REST `LastActivatedProfile` `default_profile`, `DefaultProfileName`
  `hmcpctl_…`, CLI `curr_profile` `default_profile`, state Not Activated;
- **after `chsyscfg -r lpar -o apply -p <partition> -n hmcpctl_…`** (exit 0): both REST fields
  and `curr_profile` named `hmcpctl_…`; the state stayed Not Activated; the client network
  adapter, the vSCSI client adapter and the root-disk mapping were all still there.

## Decision

1. **When.** A provision without `install` that does not power on runs one more effect after
   `default_profile`: `apply_saved` (kind `lpar.profile_apply`, target `lpar:<uuid>`, step
   `apply_saved_profile`). Two cases are unchanged:
   - with `power_on`, activating the saved profile already makes it last-activated;
   - with `install`, every `boot` choice ends by activating the saved profile.
2. **How.** The body re-reads the partition and requires it Not Activated. Otherwise, or when
   the partition or its system has no name, the effect is definitely not applied and nothing is
   written. It then runs
   `chsyscfg -r lpar -m <system> -o apply -p <partition> -n <saved>` through
   `apply_lpar_profile_via_cli`. No activation is requested.
3. **Verification.** After the apply the body requires all of these:
   - `LastActivatedProfile` and `DefaultProfileName` both name the saved profile;
   - the state is still Not Activated;
   - a client network adapter on the requested VLAN is listed;
   - the root disk is still mapped to the partition.

   If any is missing, the step errors and the run pauses `needs_attention`. Nothing is rolled
   back, as in ADR 0313; decommission removes what the run created.
4. **Resume.** The classifier reads the partition:
   - `LastActivatedProfile` equal to the saved profile → `applied` only when Decision 3's
     whole proof holds, otherwise `needs_attention`;
   - otherwise, a state other than Not Activated → `needs_attention`;
   - otherwise → `not_applied`, and the apply runs again. Applying the same profile twice is
     the same write.
5. **Authority.** The apply runs as part of `hmc_create_lpar`, which already applies a profile
   when it creates through `mksyscfg`. No delegated tool is added and no policy changes.
6. **Disclosure.** `hmc_reconfigure_lpar`'s description states ADR 0314 Decision 2's refusal, so
   an agent can anticipate it for partitions provisioned elsewhere.

## Consequences

- A `power_on=false` provision now needs the SSH transport, as `install` and
  `minimum_affinity_policy` already do. `dry_run` does not check that transport.
- The native capture covered a partition with one client network adapter, its vSCSI client
  adapter and the root disk. With `assignments` (dedicated slots, SR-IOV logical ports, vNICs),
  the apply also brings the saved profile's assignments into the current configuration. That
  path has no native proof, and Decision 3 does not verify those assignments.
- `provision.lpar`'s closure changes, so live evidence recorded before this change is stale for
  release claims.
- ADR 0314 Decision 2 is unchanged. Every partition provision leaves `configured` now satisfies
  it.
- Known gap: an `install` run with `boot: deferred` stops at `ready_to_boot` without activating.
  A reconfigure before `continuation=boot` is still refused. The boot itself activates the saved
  profile.

## Considered & rejected

- **Relax ADR 0314 Decision 2 for a not-activated partition.** verified: after
  `hmc_power_lpar start` (the current configuration), `LastActivatedProfile` still names the
  creation profile, so the next live or pending reconfigure is refused again. It would also widen
  the check for every inactive partition, including ones whose separate profiles are deliberate.
- **Save into, or rename, the creation profile.** judgment: fit. It reverses ADR 0313's
  per-operation profile name, which the classifiers and decommission rely on.
- **Have the live arm provision with power on.** judgment: fit. It would hide a documented input
  that does not compose with reconfigure.
