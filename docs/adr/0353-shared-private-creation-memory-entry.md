# 0353 — Use one private creation-memory entry boundary

## Status
Accepted (2026-11-10), cleanup of duplicated custody enforcement.

## Context
Creation-memory independently validates private results. Five sibling wrappers already use a
fixed-group helper and canonical private validator. The independent wrapper also checks UID.

## Decision
Reuse the helper, make its custody validation unconditional, and preserve the existing UID
restriction in the shared validator. Keep selector policy unchanged and restore umask in finally.

## Consequences
One owner enforces wrapper custody and all six entries use the same private mask policy.
Unsafe relative/symlink/repository paths are rejected consistently; atomicwriter is unchanged.

## Considered & rejected
- Keep duplicate wrapper. judgment: permits safety policy drift and misses shared mask handling.
- Add a second creation-only validator. judgment: repeats the same boundary behind a new name.
- Change numbered-task selectors. judgment: independent of consolidating fixed wrapper entry.
