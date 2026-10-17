# ADR 0230: Concise storage mutation results

## Status

Accepted for #1699, within the operator-approved #1695 campaign.

## Context

Storage mutations printed success prose followed by full VG or VIOS documents.
That exposed unrelated metadata and prevented stdout from being one JSON value.
Attach-disk and upload-iso already offer intentional JSON result contracts.

## Decision

Use the existing inventory renderer for concise mutation key/value results:
TTY tables, redirected TSV, explicit JSON. Report operation, selected target,
parent context and actual outcome; preserve change-location and failure evidence.
Keep attach/upload explicit JSON envelopes. Offer `--details` for the original
operation result as JSON, documenting that it can contain hardware identifiers.
Resolve output conflicts before I/O. Retain operation safety and execution semantics.
Report `request_succeeded` and unverified final state when the operation discards
completion evidence. No-op results do not imply a new creation or deletion.
Route existing confirmation prompts to stderr without changing consent policy.

## Consequences

Scripts parsing old implicit dumps must select `--details`; scripts requesting
attach/upload JSON keep their fields. Accepted HTTP 202 responses can have empty bodies; a successful return alone
is not completion evidence. Returned jobs retain their status and failure evidence. Partial
attach failures stay nonzero and expose step evidence without successful raw dumps.

## Considered & rejected

- Replace intentional JSON envelopes: verified: #1695's compatibility constraint
  preserves externally consumed explicit JSON contracts.
- Keep unconditional raw dumps: judgment: unrelated metadata obscures the requested
  operation and makes a concise default impossible.
- New output framework or backend read-back requests: judgment: existing renderer
  and synchronous operation contracts suffice for this presentation change.
