# ADR 0368: Separate storage, optical and ISO transfer owners

## Status

Accepted (2026-11-11), following explicit operator approval.

## Context

The storage resource module mixes volume-group/disk/mapping operations with
optical device orchestration and bounded HTTP ISO transfer. Changing transfer
policy requires navigating unrelated device and disk workflows.

## Decision

Keep core disk/mapping operations, shared result and provider projections in
storage.resources. Move optical repository/device workflows and optical models
to storage.optical; move URL policy, download and web-file upload to
storage.iso_upload. Consumers import the actual owner, without re-exports.
Both new modules depend on core; optical reuses its three provider-field readers.
Update current source citations in ADR0092's explicitly maintained §§3–5 inventory
and its executable inventory paths, preserving policy and historical Context.

## Consequences

Three pre-release dataclass module paths change; fields and serialized results
remain unchanged. Existing request order, authorization, bounds, fatal TLS
cleanup cutoff and logger channel remain intact. No common framework is added.

## Considered & rejected

- Extract only ISO transfer: judgment: leaves optical device orchestration mixed
  with disk and mapping ownership instead of following its own lifecycle.
- Keep one module: judgment: preserves the reviewed responsibility concentration.
- Add generic models/common modules: judgment: the existing storage core already
  owns the shared result and provider projections.
- Re-export old paths: judgment: retains ambiguous ownership without a protected
  compatibility contract on these approved pre-release module APIs.
