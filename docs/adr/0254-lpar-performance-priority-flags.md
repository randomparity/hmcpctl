# ADR 0254: LPAR performance, priority and restart flags

## Status

Accepted for issue #1762 implementation under the approved C01 scope.

## Context

The POWER10/POWER11 mksyscfg/chsyscfg corpora define five scalar partition
creation/update fields. REST declarations do not independently prove write
permissions. Existing create owns resource checks/stamping and update owns
partition containment/authorization.

## Decision

Use the documented typed CLI grammar. Add optional LparSettings to existing
creation inputs; preserve existing REST creation when an explicit keylock
cannot be represented by mksyscfg, then configure settings as a separately
reported post-create step. Preserve every requested create option or refuse
before effects. Existing update ownership resolves the scoped target before
a fixed chsyscfg record. Verify supplied settings by scoped lssyscfg readback.
Partial/unknown outcomes remain visible and are not automatically retried.

## Consequences

Settings are configuration evidence, not proof of guest runtime, clock,
remote-restart or migration readiness. time_ref can affect system time while
running and requires explicit system-wide impact approval for shared-hardware
True probes. Live maturity remains unevidenced without commit-bound execution.

## Considered & rejected

- **Guess REST field write attributes.** verified: retained schema declarations
  do not settle kb semantics; issue #1762 expressly permits documented CLI.
- **Drop keylock when selecting CLI.** verified: existing LparCreation exposes
  keylock and REST builds KeylockPosition; losing a requested value breaks it.
- **Create first and configure settings in every case.** judgment: unnecessary
  partial-effect window when mksyscfg expresses the complete supported request.
- **Do nothing.** verified: the twenty #1762 source bindings remain unsupported
  at merged core f646a9f119870cc6a0668838f259ec5ef0e2297c.
