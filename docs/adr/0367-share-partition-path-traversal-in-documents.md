# ADR 0367: Share partition path traversal in documents

## Status

Accepted (2026-11-11), following explicit operator approval.

## Context

Boot-order interpretation imports a private client-mixin helper for exact
namespace-sensitive traversal. Both this operation and client editing need its
existing missing-path and ambiguity semantics.

## Decision

Move that implementation to `documents.lpar.exact_partition_path`. Migrate the
three client and five boot-order sites directly and remove the old helper.
Use `documents.common.UOM_NS` directly for boot-order root checks.

## Consequences

Document interpretation has one existing owner without a new layer. The old
private Python helper disappears; no alias or stable facade export is added.
Namespace, duplicate, missing-path and error behavior remain unchanged.

## Considered & rejected

- Duplicate in boot order: judgment: two implementations would own one policy.
- Keep the private client import: judgment: document interpretation should not
  depend on the client implementation.
- Reuse saved-profile traversal: judgment: its required-field and ValueError
  policy is a different contract from this optional HMCError path.
