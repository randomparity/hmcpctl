# 0199 — LPAR power is one journaled job per call, reconciled from partition state

## Status

Accepted (2026-10-03), issue #1223. Implements the H1 spec's `hmc_power_lpar` row under ADR 0189
Decision 2 and ADRs 0190 and 0195. It narrows the spec's selector rule for this tool only:
`system_name_or_uuid` is required (Decision 2). ADR 0189 and the H1 spec otherwise stand.

> **Amended by [0372](0372-ibm-i-graceful-shutdown-without-rmc.md)** (2026-11-13): Decision 5's
> graceful RMC rule no longer applies to `OS400` partitions.

> **Note** (2026-10-04, issue #1314): the `hmc_power_off_lpar` docstring this record cites no
> longer calls `immediate=False` graceful, and ADR 0164's amendment now refuses `shutdown` with
> `restart` and without `immediate`.

## Context

The H1 spec gives `hmc_power_lpar` the inputs `lpar`, `action` (`start` / `stop` / `restart`)
and `mode` (`graceful` / `immediate`), and says a restart resumed after its power-off was
applied must not power off again. Four facts shape how:

- The HMC's PowerOff job takes `operation` (`shutdown` / `osshutdown` / `dumprestart`),
  `immediate` and `restart` (`jobs/requests.py`). `osshutdown` asks the operating system and
  needs an active RMC connection; `shutdown` without `immediate` is the delayed power-off, and
  with `restart` but without `immediate` it is a dump restart (`docs/refs/hmc-commands-p10/
  commands/chsysstate.md:106`). With `restart=true` one job powers off and on again.
- The ADR 0190 partition guard is keyed by system UUID and partition UUID and must be held
  before the first write.
- The engine records an effect's identity only with its outcome (ADR 0195), so an `uncertain`
  effect never carries a JobID. The spec's "plus the HMC JobID if one was recorded" therefore
  applies to replay of an `applied` effect, not to classification.
- A queued power job and no job read the same: the partition stays in its old state until the
  job runs.

## Decision

1. **Mode mapping.** `graceful` is PowerOff `operation=osshutdown`; `immediate` is
   `operation=shutdown` with `immediate=true`. `restart` is the same job with `restart=true`.
   `shutdown` is never sent without `immediate`, so no input reaches a dump restart. `start`
   takes no mode; `mode=immediate` with `start` is refused. Nothing escalates from graceful to
   immediate: not a timeout, a failed job, or a resume.
2. **The system selector is required**, and its UUID is lower-cased, so the guard key is the
   same however the caller spells it. The partition must be in that system's partition list.
3. **One effect per call.** Each action writes at most one HMC job, journaled under key
   `power` with kind `lpar.power_on`, `lpar.power_off` or `lpar.restart` and identity
   `{job_id}`. A replay that finds it `applied` reuses the recorded partition, polls the JobID
   when there is one and never resubmits; with no JobID (applied by classification) it goes
   straight to the state check.
4. **Classification.** An open `lpar.power_on` effect is `applied` when the partition is
   activated (`running`, `starting`, `open firmware`); an open `lpar.power_off` effect is
   `applied` when it is `not activated`. Any other reading needs attention, because a queued
   job is indistinguishable from none. `lpar.restart` registers no classifier: a completed
   cycle and no cycle both read as running, so an open restart always pauses for attention
   (ADR 0195 Decision 4). Resume therefore never writes a second power job.
5. **States.** Before writing, a `start` on an activated partition and a `stop` on a
   `not activated` one complete with `already_in_state=true` and record no effect. `start`
   needs `not activated`. `stop` and `restart` need an activated partition, or `error` with
   `mode=immediate`; graceful also needs RMC `active`. Any other state fails, naming it.
6. **Verified state, bounded.** After a successful job the body re-reads the state until it is
   the action's target (activated for `start` and `restart`, `not activated` for `stop`) or
   120 seconds pass. A job not terminal after 300 seconds, or a state that never settles,
   pauses with `needs_attention`; `resume` re-polls. On a replay a job the HMC answers 404 for
   is skipped for the state check, except for `restart`, which then pauses: its state check
   alone carries no evidence of a cycle. Any other job-read error pauses. A failed job, or `error` after a start, is
   `failed`; `not activated` after a start may be lag, so it pauses instead. For `restart` the evidence is the job's
   success plus an activated reading, not proof that a cycle happened. Both bounds are
   assumptions until a live run measures them.
7. **ADR 0011 ownership** applies as for the specialists: with
   `HMC_AUTHORIZE_POWER_OPERATIONS` set, a partition another agent owns is refused before any
   write. `ownership_override` is not offered; the specialists keep it.

## Consequences

- An interrupted write always needs a human unless the partition already shows the target
  state: inspect it, then resume or abandon. That is the cost of never writing twice.
- A submit the HMC refused also needs attention, because the engine cannot tell a refused job
  from one that ran (ADR 0195 Considered & rejected).
- A job that ends with warnings, or is cancelled while running, makes the operation `failed`
  even though it may have acted; inspect the partition before retrying, above all a restart.
- A pause the body returns (job not done, state not settled, replayed restart without its
  job) carries no reason of its own; `job_id` and `observed_state` are the evidence. A
  restart that pauses again on every resume needs `abandon`.
- Callers who know only a partition name must now name its system.
- A long IBM i or graceful shutdown can outlast the bounds and pause; `resume` continues it.
- Effect keys, kinds and the identity shape are a persisted contract (ADR 0195).

## Considered & rejected

- **Do nothing: leave agents on the two specialists.** judgment: fit. #1223 and the H1 spec
  require the logical tool.
- **`graceful` as `shutdown` without `immediate`.** verified: that is the delayed power-off,
  and with `restart` it is a dump restart (`docs/refs/hmc-commands-p10/commands/
  chsysstate.md:106`). The H1 spec's graceful is the operating system's. `hmc_power_off_lpar`'s
  docstring calls `immediate=False` graceful; this record follows the vendor text.
- **Restart as a stop effect then a start effect.** judgment: complexity. Two jobs mean two
  open-effect windows to reconcile, where the HMC offers the cycle as one job.
- **Classify an open restart from uptime or a boot counter.** verified:
  `rg -il 'uptime|bootcount' src/hmcpctl` returns nothing (main `726f29e6`); refcodes are #1224's.
- **Resubmit an open PowerOn found `not activated`.** judgment: fit. A queued first job would
  make the second fail and the operation end `failed` while the partition starts.
- **Keep the system selector optional.** judgment: cost. Keying the guard before the first
  write would then need the ADR 0094 fleet walk whenever the selector is omitted.
- **Expose `ownership_override`.** judgment: fit. The spec's inputs omit it, and an override is
  an operator exception the audited specialists already carry.
