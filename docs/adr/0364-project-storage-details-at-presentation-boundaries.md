# ADR 0364: Project storage details at presentation boundaries

## Status

Accepted following explicit operator approval on 2026-11-11.

## Context

Map and optical mount build application envelopes and normalize those envelopes
again. Detach and unmount pass serialized ChangeLocation into a resource decoder.
CLI attach fabricates a storage result solely to call its printer. These paths
mix known domain facts with output shape, although typed result fields exist.

## Decision

Keep StorageMutationResult and its fields. Map raw is the provider entry; mount
raw is the existing extracted provider body. Interpret provider resource/job
once and carry authorized identity and ChangeLocation independently. Detach and
unmount retain no provider payload and return raw/resource/job=None with location.
Remove lowercase application-envelope decoding; retain provider Resource handling.

MCP map and CLI map/mount construct their previous envelopes at presentation.
MCP mount retains typed-resource flattening. CLI detach/unmount serialize typed
location. The shared CLI printer consumes explicit details payload, job and
location, including attach without a synthetic result. Other operations retain
existing diagnostic raw values and provider normalization.

## Consequences

The four internal raw contracts change together with direct consumers and fakes.
MCP schemas, output values, CLI summaries/details/exits, authorization and remote
request order remain unchanged. No fallback shim or result hierarchy is added.

## Considered & rejected

- Keep old envelopes: judgment: preserves the duplicated presentation/decoding cycle.
- Only change detach/unmount: judgment: leaves the approved map/mount boundary unfinished.
- Add specialized result subclasses or an output framework: judgment: existing typed
  fields and adapter-local envelopes express this small set without more surface.
