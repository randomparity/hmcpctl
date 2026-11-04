# ADR 0337: Live failure session redaction

## Status

Proposed for #1896.

## Context

Live-test arms can record raw HMC error bodies. The shared runner currently
scrubs generic secrets and paths but misses named session headers and elements.
Issue #1896 requires this output boundary independently of #1884.

## Decision

Extend the runner's existing failure text redactor before generic path handling:
remove RequestHeaders and session-bearing XML regions, then session-bearing
header lines. Missing XML closers consume through end of input. Retain the
existing callers, recursion and output schema.

## Consequences

Failure status and diagnostics outside sensitive regions retain their existing
behavior. Other text in a redacted header line or truncated sensitive XML region
is lost conservatively. No new dependency or runtime interface is introduced.

## Considered & rejected

- **Rely only on HMCError sanitization.** verified: issue #1896 explicitly requires
  an independent results boundary; `scripts/live_test/runner.py` accepts direct
  FAIL data, and `scripts/live_test/vmedia_media_change.py` records raw bodies.
- **Drop complete failure bodies.** judgment: disproportionate diagnostic loss
  when named credential-bearing regions can be removed at the existing owner.
- **Parse XML then redact.** judgment: a separate text/malformed-input fallback
  would duplicate policy for the same output boundary.
