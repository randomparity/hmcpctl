# ADR 0305: Partition GZIP accelerator QoS

## Status

Proposed for #1781. Design preparation only; no implementation or live authority.

## Context

D08 allocates the partition GZIP set selector from both published command corpora.
The current native quantity owners concern memory and processors, whose reset,
range and runtime contracts do not establish accelerator behavior.

The 2026-10-26 operator decisions permit nonnegative integer QoS input with HMC
final allocation-limit and undocumented state-eligibility validation. They retain
advertised capability/type, known state, containment, ownership, audit and the
published RMC/force restrictions. The approved exclusion/owner set is empty.

## Decision

Add one finite partition accelerator operation with typed QoS, optional wait/detail
and explicit force. Reuse the existing transport, selected-system identity feeds,
ownership guard and presentation authorization. Keep accelerator admission and
native current/pending observations in its own owner; do not generalize memory.

Submit at most one `chhwres -r accel --rsubtype gzip -o s` command. Observe native
`curr_qos` and `pend_qos` independently afterward, including after native refusal
or uncertain transport. Report command disposition separately from postconditions.
Current/pending equality establishes only the named native readback contract.
Effective guest runtime and profile persistence remain unverified.

## Consequences

Zero is an explicit quantity, with no default, clearing or reset interpretation.
No local constant derived from the sampled `128` limits accepted QoS. Unknown
capability, malformed identity or unknown state refuses before the command.
Known state is not a promise of native mutation eligibility. Explicit force
acknowledges intentional HMC/guest network isolation and the documented restart
consequence; it is unavailable for IBM i. No automatic retry, compensation,
activation or restart follows a submitted request.

Native concurrent administrators can change the target after rechecks. Independent
observations describe the result; they cannot prove atomicity or causation.
A rejected native command with unchanged observations is a rejection observation,
not a proof that nothing happened between observations.

## Considered & rejected

- **Do nothing.** judgment: leaves the approved D08 behavior without its typed path.
- **Extend memory/processor setters.** verified: at base
  `1aad137647baa771497870e01fbc875149666140`,
  `src/hmcpctl/operations/lpar/memory.py:_targets` and `_postconditions` encode
  memory envelopes, coupled resets and runtime fields; those are not QoS fields.
- **Use XML absence as QoS zero or effective-runtime proof.** verified: the retained
  2026-10-26 native-source capture reports native zero current/pending while the
  corresponding XML accelerator collection has no QoS member; that sample
  establishes neither representation equivalence nor guest execution.
- **Expose arbitrary command flags.** judgment: fixed typed fields meet D08 without
  introducing a proxy or another authorization boundary.
