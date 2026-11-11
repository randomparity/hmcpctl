# ADR 0359: Durable adapter continuation arguments

## Status

Accepted within the authorized behavior-preserving cleanup.

## Context

Three durable LPAR adapters duplicate missing-record refusal and argument overlay.
Provisioning also reads records for new requests and normalizes supplied JSON values.

## Decision

Own the pure refusal/merge in `server_tools/lpar/continuation.py`. All three adapters
import it directly and keep lookup timing, serialization, defaults and authorization.

## Consequences

One implementation governs missing versus empty records without a configurable
handler framework, new public contract or changed engine policy.

## Considered & rejected

- Move lookup into the helper. verified: at source `430a2643`, power/reconfigure
  read only on continuation; provision also reads for new requests to detect existence.
- Combine filtering/serialization. verified: only provision's `_given` uses
  `json_value` at `430a2643`; the other two retain raw values.
- A configurable effective-arguments framework. judgment: local operation defaults
  and required-field checks keep this change smaller and its policy visible.
