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
- Activation without a profile uses the current configuration
  (`docs/refs/hmc-commands-p10/commands/chsysstate.md:54`, `:188`), and `hmc_power_lpar start`
  submits a PowerOn with no profile (`operations/lpar/power.py`, `_submit`).
- `update_partition_profile` is a typed If-Match write with readback facts (ADR 0242).
- The engine replays a body; reads between writes re-run, so a plan derived from live reads must
  be recorded through an effect (ADR 0195 Consequences).

## Decision

1. **Patch meaning.** `name` is the partition name (`PartitionName`); profile names never
   change. `memory_mib` is desired memory. `processors` is desired dedicated processors in
   dedicated mode and desired virtual processors in shared mode; processing units and the
   processor mode are never changed, and a shared-mode value that leaves desired, minimum or
   maximum units above virtual processors is refused before any write.
2. **One target profile:** the partition's `DefaultProfileName`. A non-empty
   `LastActivatedProfile` that differs is refused before any write: a write to one would not
   persist through activation of the other.
3. **Classification**, per resource, from one read of the partition and profile. A resource
   changes when the value differs from the profile's desired value or the current value; the
   profile is written when it differs, the current configuration when that differs, and
   `before` reports both:
   - `not activated` → **`profile`**: the profile and the current configuration are both
     written, so activation from either source applies it;
   - `running`, RMC `active`, the value within current min/max, and either an increase or
     `allow_disruption=true` → **`live`**: profile written, then the current configuration;
   - any other non-empty state (`running` without the above, `open firmware`, `starting`, …) →
     **`pending_activation`**: profile only; `activation` names the profile to activate. Hmcpctl
     never reboots. An empty or unreadable state is refused;
   - a value outside the profile's min/max widens that bound in the same profile write, and the
     change is then `pending_activation` in every state; on `not activated` the current
     configuration is still written too, as for `profile`. A rename is always `live`;
   - with `CurrentProfileSync` not `Disabled`/`Suspended`, an activated partition's plan holding both
     a `pending_activation` change and a current-configuration write (a `live` change or a
     rename) is refused before any write: the HMC would copy the current configuration over the
     pending profile values. The refusal names `hmc_sync_lpar_profile` and splitting the patch.
4. **Effects, in order** (keys and kinds are persisted): `plan` (`lpar.reconfigure_plan`, no HMC
   write; identity is the plan, including the profile ETag; its classifier answers `not_applied`,
   so an interrupted plan is recomputed); `profile` (`lpar.profile_write`, target
   `lpar:<uuid>/profile:<uuid>`); `current` (`lpar.current_config`, memory and processors in one
   partition write); `rename` (`lpar.rename`).
5. **Resume.** `lpar.profile_write` classifies by profile readback: the requested values →
   `applied`, the recorded `before` values → `not_applied`, anything else → `needs_attention`. It
   is a shared kind whose classifier factory takes a reader of the expected and `before` values,
   for #1225 to reuse.
   `lpar.rename` classifies by the partition name. The two current-configuration kinds register no
   classifier: an interrupted one always pauses `needs_attention` (H1 spec *Resume*). A
   pre-write refusal from `update_partition_profile` or a 412 there, and a `LparUpdateError` with
   `submission="rejected"` or an error before the POST from `update_logical_partition`, are
   `EffectNotApplied`.
6. **Authority.** Before anything is recorded the handler permits and authorizes, for this
   partition and system, `hmc_dlpar_mem` and `hmc_update_partition_profile` for `memory_mib`,
   `hmc_dlpar_proc` and `hmc_update_partition_profile` for `processors`, and `hmc_modify_lpar`
   plus `hmc_rename_lpar` for `name` (the
   served `hmc_modify_lpar` cannot rename) — the tools the patch can
   use, whatever the plan later selects. The body re-resolves the partition by UUID within its
   system and re-runs the ADR 0011 check before every write; no `ownership_override`.
7. **Stale plan and verification.** Before the first write not yet applied, whichever key it is,
   the body re-reads what the plan rests on — partition state, RMC, `CurrentProfileSync` and the
   current values; the recorded ETag is also the profile's concurrency check (a 412 or ETag
   refusal is `not_applied`, then `failed`). A difference fails the operation without writing.
   An applied effect is not re-checked. After the writes the body polls up to 120 seconds until
   the profile holds every planned value and the current configuration every value the plan
   wrote there; a
   mismatch pauses `needs_attention`, and `resume` re-verifies.

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
- **Write only the profile for a not-activated partition.** verified: `hmc_power_lpar start`
  sends no profile (`power.py` `_submit`), and that activation uses the current configuration
  (`chsysstate.md:54`), so the change could be lost.
- **Live write first, profile second.** judgment: fit. Under sync the live write rewrites the
  profile, so the plan's ETag goes stale and the profile write refuses after a live change.
- **Order writes to survive sync instead of refusing.** judgment: complexity. No order keeps a
  pending value through a later current-configuration write; ADR 0201 has no capture of sync on
  an activated partition to design against.
- **Separate memory and processor current-configuration effects.** judgment: complexity. Neither
  is classifiable, so two keys add a write and an interruption window for no recovery gain.
- **Refuse values outside the profile bounds.** judgment: fit. The H1 spec reserves a
  `pending_activation` class for min/max changes; refusing would leave it unreachable.
- **Encode the expected values in the effect target.** judgment: complexity. A 256-character
  target cannot carry a provision patch; a classifier factory can.
