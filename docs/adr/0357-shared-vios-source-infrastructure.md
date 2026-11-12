# ADR 0357: Share finite VIOS source infrastructure

## Status

Accepted, 2026-11-10. The operator approved this bounded ownership transition
during the November cleanup. Supersedes only ADR 0306's AME helper ownership.

## Context

The boot-source arm inherited AME transport accounting and called its snapshot
and record helpers. This coupled independent diagnostic policies through an arm
module and made its source witness depend on that other arm's implementation.

## Decision

`scripts/live_test/vios_source_support.py` owns the existing physical budgets,
counted REST/SSH transport, bounded collection and snapshot validation, identity
projections, fixed capacity admission, and private stage receipt admission.
Both arms import this concrete owner directly; it imports neither arm.

Each arm owns its fixed fixture resources, native mutation commands, authority,
reserves, journals and recovery. AME retains two once-only fixture deletions;
boot retains one deletion after its durable intent, held latch and final authority
recheck. Arm overrides consume these tokens before counted transport. The shared
client admits only reads and once-only session logon/logout on its own.

Boot builds its fixed native creation record with the existing resource and
attribute builders. It neither fabricates an AME case nor imports AME policy.
Its literal encoding, inactive preservation and source-only evidence stay fixed.

## Consequences

Both exact selected source closures include the new owner and its existing
dependencies; boot no longer includes the AME arm. Earlier packets require a
fresh commit-bound capture. Missing, extra or stale selected source hashes fail
closed. Tests enumerate each closure independently and mutate the shared owner
and native codec for both arms.

This changes no product API, packet format, diagnostic scope, mutation budget,
qualification or maturity claim. It introduces no configurable workflow framework
and authorizes no live hardware operation.
