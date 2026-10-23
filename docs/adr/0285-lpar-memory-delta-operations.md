# ADR 0285: LPAR memory delta operations

## Status

Accepted for issue #1774 implementation within frozen scope q1774-4f3dda1d.

## Context

The existing absolute setter edits Desired/Minimum/MaximumMemory under If-Match.
Documented chhwres quantity operations use current memory, shared-only I/O
entitlement and native partial-effect semantics. Conflating those contracts
would change the existing setter and misrepresent native resource changes.

## Decision

Retain the absolute setter and expose a mutually exclusive typed quantity mode
through the existing memory MCP operation and a dedicated CLI command. Keep
validation/readback in a memory operation and fixed native grammar in SSH.
Do not emulate native deltas with an unconditional absolute update. Validate
contained ownership, mode/state/RMC, quantities, alignment and available capacity;
submit once and independently read current/runtime quantity and entitlement mode.
Report partial/unverified outcomes and profile-sync/restart warnings explicitly.

## Consequences

The CLI quantity path has no invented If-Match or cross-transport transaction.
A final pre-submit recheck limits stale decisions; native refusal and partial
outcomes remain visible without replay. Shared logical memory is not capped to
physical system memory; entitlement uses its own pool capacity. Automatic mode
uses HMC admission rather than an invented entitlement formula. Shutdown reset
can clear processor assignments and must be visible in confirmation and readback.
Live maturity is not promoted by schemas or simulated tests.

## Considered & rejected

- **Translate every request to the existing setter.** verified: at base
  1373d713, dlpar.py writes Desired/Minimum/MaximumMemory; the P10/P11 chhwres
  reference specifies current-value deltas and shared I/O entitlement.
- **Replace the absolute setter.** judgment: breaks a working, independently
  guarded contract without simplifying the distinct native semantics.
- **Add an arbitrary chhwres proxy.** judgment: unnecessary input and authority
  surface for a finite typed operation.
- **Use system available memory for AMS entitlement.** verified: published
  SharedMemoryPool child resource and served schema define separate
  CurrentAvailablePoolMemory; logical overcommit is a separate resource contract.
- **Do nothing.** judgment: leaves the operator-requested native quantity and
  entitlement gap unresolved.
