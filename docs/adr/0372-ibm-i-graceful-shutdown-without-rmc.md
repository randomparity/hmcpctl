# ADR 0372: IBM i graceful shutdown does not need RMC

## Status

Accepted (2026-11-13), issue #1814, under the operator-confirmed campaign dispatch. Amends
ADR 0199 Decision 5's rule "graceful also needs RMC `active`" for `OS400` partitions only;
ADR 0199 otherwise stands.

## Context

`hmc_power_lpar` refused every graceful stop or restart unless the partition's
`ResourceMonitoringControlState` read `active`, because ADR 0199 recorded that `osshutdown`
needs an RMC connection. Neither HMC reference says so: the `chsysstate` `osshutdown` entry
(`docs/refs/hmc-commands-p11/commands/chsysstate.md:106`) and the PowerOff job's `operation`
parameter (`docs/refs/hmc-rest-api-p11/jobs/logicalpartition-jobs/038-poweroff_logicalpartition-job.md:30`)
carry no RMC condition, and the former names IBM i's normal OS shutdown explicitly. pypowervm
`8613035`, `pypowervm/tasks/power_opts.py:337` (`can_os_shutdown`), admits OS shutdown when the
partition type is `OS400` or RMC is active, and `pypowervm/tasks/power.py:123` raises
`OSShutdownNoRMC` only for the rest. The lab has no IBM i partition, so no live run can test it.

## Decision

The graceful precondition admits a partition whose `PartitionType` leaf text (read through
`leaf_text`, since the leaf may carry a `ksv` attribute) is exactly `OS400` — the enumeration key
`docs/refs/hmc-rest-api-p11/managed-system/165-logical-partition.md:92` names — in any RMC state. Every other value — `AIX/Linux`, `Virtual IO Server`, an absent or any unrecognised
type — still needs RMC `active`, and the refusal still writes nothing. Graceful never falls back
to `immediate`. The IBM i path is implemented and unevidenced: the lpar-power arm records it as
a SKIP gap naming its prerequisite (ADR 0212: live execution is not a merge gate).

## Consequences

- An IBM i graceful job the OS cannot honour now reaches the HMC and fails or times out there,
  reported through ADR 0199's existing failed / `needs_attention` outcomes, instead of a precheck.
- An honoured IBM i normal shutdown "will take longer to complete" (`chsysstate.md:106`), so it may
  outrun the 300 s job / 120 s settle bounds and pause as `needs_attention`;
  `continuation=resume` finishes it without a second job. The bounds do not change.
- `hmc_power_off_lpar` is unchanged; it never had an RMC precheck.
- A case or spelling variant of `OS400` is treated as unknown and keeps the RMC requirement.

## Considered & rejected

- **Keep the guard until live evidence exists.** judgment: the references and pypowervm agree,
  the operator excluded acquiring IBM i, and a refusal with no source behind it blocks a
  supported path indefinitely.
- **Drop the RMC precheck for every type.** verified: pypowervm `8613035`
  `pypowervm/tasks/power.py:123` refuses OS shutdown without RMC on non-IBM i partitions.
- **Normalise the type case-insensitively.** judgment: the reference names the key `OS400`;
  widening the match admits values no source names.
