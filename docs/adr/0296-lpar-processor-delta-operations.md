# ADR 0296: LPAR processor delta operations

## Status

Accepted for #1778, frozen scope q1778-1db8f45e; native evidence remains separately qualified.

## Context

Native processor quantities use current resources and distinguish whole dedicated CPUs,
whole shared virtual processors and decimal shared entitlement. The existing REST setter
writes configured Desired/Minimum/Maximum resources under If-Match and names shared
entitlement `procs`. Native shutdown reset can also clear memory assignments.

## Decision

Retain the existing absolute setter and add a mutually exclusive typed native change mode
on hmc_dlpar_proc, plus lpars processors. Name native fields processors and processing_units;
mode comes from contained readback and is never switched. Put admission/results in the
processor operation, fixed native grammar in SSH and reuse landed containment/memory snapshot
helpers. Use a native filtered CPU row to verify current/pending/runtime counts alongside
REST identity, memory, capacity and profile-sync observations. Submit once, then independently
classify applied, no-op, rejected, partial or unverified; never replay or compensate.
Add an optional byte limit on the existing SSH transport; the processor commands and their
required ownership-description read opt in through the existing authorization guard. Drain
both binary streams under one budget with fixed reads and native backpressure;
abort on overflow and preserve uncertain-effect semantics. Existing callers retain defaults.

## Consequences

The original REST concurrency guarantee survives. Native changes have a final evidence
recheck but no invented ETag, cross-transport transaction or causal attribution. Precise
Decimal entitlement and mode-specific integer counts avoid unit conflation. Coupled reset
requires complete CPU and memory postconditions and visible confirmation. Runtime verification
needs native readback where the captured REST shape lacks a shared runtime vCPU leaf.
The byte cap limits retained payload, with bounded reader/window lookahead; it does not
claim a wire-byte or process-RSS ceiling. Cancellation/abort cannot undo remote effects.
Live evidence remains separate from offline correctness and source coverage.

## Considered & rejected

- **Route native deltas/set through the old setter.** verified: dlpar.py:217 and
  documents/lpar.py:626 at commit40f9f856 write DesiredProcessingUnits and
  DesiredVirtualProcessors; P10 chhwres.md:226–227 defines current-based native quantities.
- **Reuse LparResources for the native request.** verified: documents/lpar.py:245–277
  names shared entitlement procs and counts vcpus; native --procs names counts in both modes.
- **Use only captured REST readback.** verified: rest-lpar-entry.json exposes
  AllocatedVirtualProcessors and RuntimeProcessingUnits but no distinct shared runtime
  vCPU value; captured native vocabulary supplies run_procs.
- **Create a generic processor/pool command proxy.** judgment: unnecessary authority
  and parsing surface for this finite quantity family.
- **Replace the old setter.** judgment: loses its working If-Match and configured-resource
  contract without removing the need for native current-resource semantics.
- **Cap only after connection.run.** verified: AsyncSSH2.24.1 process.py:1468
  disables stream backpressure inside communicate before collecting output; a later parser
  size check cannot bound that allocation.
- **Make every SSH caller bounded now.** judgment: unnecessary compatibility expansion;
  the new processor route and its required read dependency enforce a finite budget; unrelated
  callers retain their defaults.
- **Do nothing.** judgment: leaves #1778's requested native quantity gap unresolved.
