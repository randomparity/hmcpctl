# ADR 0238: Target-first single-partition operation APIs

## Status
Accepted (2026-10-18), operator-approved pre-release API cleanup.

## Context
Existing power, inventory and migration APIs put the partition after the client,
but remaining single-partition APIs still require system before partition.

## Decision
Complete target-first order for the34functions listed in the selector-family
specification. System scope becomes keyword-only, preserving required string
scope versus nullable defaultNone. Update repository consumers directly.
Bulk filtering and raw/prevalidated identity helpers keep distinct signatures.

## Consequences
Pre-release direct callers migrate; no compatibility aliases are retained.
Operation algorithms, authorization, persisted data and CLI/MCP wire shapes
remain unchanged. The stable six-name facade is unaffected.

## Considered & rejected
- **Retain mixed positional selector order.** judgment: invites system/partition
  binding mistakes when switching between related domain operations.
- **Accept both positional forms.** judgment: two strings cannot reliably express
  which selector the caller intended; an alias prolongs the ambiguity.
