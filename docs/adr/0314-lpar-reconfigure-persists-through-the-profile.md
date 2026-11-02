# 0314 — LPAR reconfigure persists through the default profile and classifies each change

## Status

Accepted (2026-11-02), issue #1226. Implements the H1 spec's `hmc_reconfigure_lpar` row under
ADRs 0189, 0190 and 0195, with ADR 0199 as the wiring precedent. ADR 0242 stands unchanged: the
profile write still requires `exclusive_writer_window=true`, which this tool takes as an input
(operator decision, 2026-11-02). The operator delegated the rename, classification and DLPAR
resume choices below to this record.

## Context

- A partition write changes the current configuration; a profile activation replaces it with
  the profile (`hmc_dlpar_mem` docstring, #1170). With `CurrentProfileSync` on, the HMC copies
  the current configuration to the last-activated profile (`operations/lpar/profile_sync.py`).
- Activation without a profile uses the current configuration, and `chsyscfg -r lpar` changes it
  before activation (`docs/refs/hmc-commands-p10/commands/chsysstate.md:94`).
- `update_partition_profile` is a typed If-Match write with readback facts (ADR 0242).
- The engine replays a body; reads between writes re-run, so a plan derived from live reads must
  be recorded through an effect (ADR 0195 Consequences).

## Decision

1. **Patch meaning.** `name` is the partition name (`PartitionName`); profile names never
   change. `memory_mib` is desired memory. `processors` is desired dedicated processors in
   dedicated mode and desired virtual processors in shared mode; processing units and the
   processor mode are never changed.
2. **One target profile:** the partition's `DefaultProfileName`. A non-empty
   `LastActivatedProfile` that differs is refused before any write: a write to one would not
   persist through activation of the other.
3. **Classification**, per changed resource, from one read of the partition and profile:
   - `not activated` → **`profile`**: the profile and the current configuration are both
     written, so activation from either source applies it;
   - `running`, RMC `active`, the value within current min/max, and either an increase or
     `allow_disruption=true` → **`live`**: profile written, then the current configuration;
   - otherwise `running` → **`pending_activation`**: profile only; `activation` names the
     profile to activate. Hmcpctl never reboots;
   - a value outside the profile's min/max widens that bound in the same profile write; on a
     running partition that change is always `pending_activation`;
   - any other partition state is refused, naming it. A rename is always `live`.
4. **Effects, in order** (keys and kinds are persisted): `plan` (`lpar.reconfigure_plan`, no HMC
   write; identity is the plan; its classifier answers `not_applied`, so an interrupted plan is
   recomputed); `profile` (`lpar.profile_write`); `memory.current` (`lpar.current_memory`);
   `processors.current` (`lpar.current_processors`); `rename` (`lpar.rename`). The profile goes
   first so a sync-on copy after the live write cannot race the If-Match write.
5. **Resume.** `lpar.profile_write` classifies by profile readback of the requested values; it is
   a shared kind whose classifier factory takes the expected patch, for #1225 to reuse.
   `lpar.rename` classifies by the partition name. The two current-configuration kinds register no
   classifier: an interrupted one always pauses `needs_attention` (H1 spec *Resume*). A
   pre-write refusal from `update_partition_profile`, or a 412, is `EffectNotApplied`.
6. **Authority.** Before anything is recorded the handler permits and authorizes, for this
   partition and system, `hmc_update_partition_profile` plus `hmc_dlpar_mem` for `memory_mib`,
   `hmc_dlpar_proc` for `processors`, and `hmc_modify_lpar` for `name` — the tools the patch can
   use, whatever the plan later selects. The body re-runs the ADR 0011 check every run; no
   `ownership_override`.
7. **Stale plan and verification.** Before each write the body re-reads the value it changes; one
   that differs from the plan's `before` fails the operation without writing. After the writes,
   the profile must hold every planned value and the current configuration every `live` and
   `profile` value; a mismatch pauses `needs_attention`, and `resume` re-verifies.

## Consequences

- A `pending_activation` change waits for an explicit profile activation; `hmc_power_lpar start`
  activates the current configuration and does not apply it.
- Requiring the DLPAR permissions for an unrunning partition is stricter than the plan needs.
- An interrupted DLPAR always needs a human: inspect, then `abandon`.

## Considered & rejected

- **Do nothing: keep agents on the specialists.** judgment: fit. #1226 and the H1 spec require
  the logical tool and persistence.
- **Rename the profile with the partition.** judgment: fit. The partition name is the selector
  every tool uses; a profile name is a separate, unshared identity.
- **Write only the profile for a not-activated partition.** verified: activation without a
  profile uses the current configuration (`chsysstate.md:94`), so the change could be lost.
- **Live write first, profile second.** judgment: cost. With sync on the HMC rewrites the profile
  after the live write, so the If-Match profile write would 412 after a live change.
- **Refuse values outside the profile bounds.** judgment: fit. The H1 spec reserves a
  `pending_activation` class for min/max changes; refusing would leave it unreachable.
- **Encode the expected values in the effect target.** judgment: complexity. A 256-character
  target cannot carry a provision patch; a classifier factory can.
