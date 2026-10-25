# ADR 0286: Native LPAR memory moves

## Status

Proposed for issue #1775, frozen scope q1775-ecd59f74; design review and audit precede build.

## Context

A native memory move mutates two partitions. D01's model and MCP tool name only one
endpoint; adding an unseen nested destination would evade its target selectors.
The command reference provides one mem m operation, but no atomicity, replay or
rollback contract. Both endpoints' observed state is needed to judge the result.

## Decision

Reuse D01's snapshot/state/result helpers and SSH owner, with one small two-endpoint
operation and strict move request. Add a dedicated MCP tool with explicit source,
destination and system selectors, and a command in the existing memory CLI module.
Preserve the existing quantities and absolute setter contracts. Submit one native
move and attempt both readbacks even when submission or one read fails.

Client admission checks observed bounds, units, capabilities, modes and ownership.
Native firmware/guest admission governs reservation constraints not independently
readable through the sourced contract. Report observed applied, partial, rejected,
unverified or zero-request noop state without claiming precomputed guest minima.
No automatic compensation, profile mutation, replay or native transaction is added.

## Consequences

The second endpoint is visible to the existing single-grant policy decision. Each
endpoint retains its own profile-sync/deferred-effect warning. A missing endpoint
readback cannot establish completion, and an available peer observation is retained.
Native races and capacity/reservation refusal remain possible after the final reads;
manual recovery starts with inspection of both endpoints and fresh authorization.

## Considered & rejected

- **Extend hmc_dlpar_mem with an optional destination.** verified: at base
  40f9f856, tool_registry.py extracts declared scalar or nested selectors and target_scope.py
  treats missing optional selectors as absent under scoped grants. Making the
  destination a selector changes existing scoped calls; hiding it in a model
  without registering it fails the required two-endpoint policy boundary. A nested
  selector alone does not solve optional-target absence on existing calls.
- **Compose D01 remove and add.** verified: P10/P11 commands/chhwres.md:90–96 and
  215–219 publish native mem m with source and destination, while the inspected
  reference supplies no rollback contract for a composed pair.
- **Require free system memory equal to the transfer.** judgment: an unrelated
  growth prerequisite overconstrains a same-system transfer; native admission
  remains responsible for transient capacity and reservation details.
- **Calculate guest reservation from firmware --maxmem.** verified:
  commands/lshwres.md:99 explicitly distinguishes firmware minimum from possible
  higher OS requirements; IBM dedicated-memory guidance names IBM i QBASPOOL.
- **Add a general two-resource transaction framework.** judgment: speculative
  machinery does not provide a native atomicity guarantee.
- **Do nothing.** judgment: leaves the explicitly requested D02 behavior absent.
