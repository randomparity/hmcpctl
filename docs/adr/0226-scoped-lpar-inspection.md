# ADR 0226: Scoped LPAR inspection

## Status

Accepted for issue #1696.

## Context

Explicit system scope already constrains exact-name lookup, but shared UUID
resolution bypasses it. Ownership alone checks LPAR UUID containment, leaving
inspection and VIOS-backed storage inconsistent.

## Decision

Shared resource identity owns explicit-system containment for LPAR and VIOS
UUID selectors, using the existing selected-system feeds and case-insensitive
UUID membership. Move the LPAR ownership check there and remove duplicate
verification. Ownership override does not override supplied scope. Omitted
scope retains existing behavior. LPAR show/state expose --system/-s.
Required scope on delete/decommission/capture-console remains required.

This supersedes ADR 0015 only for supplied-scope UUID passthrough and ADR 0094
only for supplied-scope override containment; omitted-scope discovery and
ownership policy remain unchanged.

## Consequences

Scoped UUID reads now perform a selected-system membership read and refuse
wrong-system targets before effects. Existing feed errors retain disconnected
system diagnostics. No UUID-only client or stable facade API changes.

## Considered & rejected

- CLI-only checking. judgment: duplicates policy and leaves operation/MCP callers
  inconsistent under the same explicit-scope contract.
- Keep containment only in ownership. verified: at 0cdb4aea,
  resource_identity.py returns UUIDs before checking system_name_or_uuid,
  while operations/lpar/ownership.py alone checks the selected partition feed.
- Fleet discovery for scoped UUIDs. judgment: unnecessary reads outside a known
  selected system and a wider failure surface.
