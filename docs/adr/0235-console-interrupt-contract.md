# ADR 0235: Console capture interruption

## Status

Accepted (2026-10-16). Extends ADR 0175 for #1692, under the operator-approved
SIGINT, partial-file and exit-code policy in epic #1695.

## Context

Console capture streams and flushes raw bytes (#1674). Cancellation already runs
session release, but the CLI discards its output file and cannot report release proof.

## Decision

Ctrl-C (SIGINT) stops capture early. Retain the flushed partial `--output` file.
Exit 130 when release is proven; exit 3 when it is not. Release uncertainty outranks
interrupt and write failure. Raw stdout remains a byte stream; diagnostics use stderr.

The CLI opts into a cancellation outcome in the existing bounded capture path.
The default library and MCP cancellation behavior remains exception propagation.
`ConsoleSession` remains the only release owner, including its independent proof and
foreign-holder protection. For this opt-in interrupted outcome only, interruption
takes precedence over ADR 0172's late-contention exception: keep the partial bytes
and release-based exit, carrying contention detail in the outcome error. Ordinary
and default-library late-contention behavior remains unchanged. An interrupt before a capture outcome exists conservatively
reports unproven release. Interrupted files are partial even when no bytes arrived;
write failures still report incomplete output and never imply successful capture.

## Consequences

Operators can stop a long capture without losing its flushed bytes. An unproven
release still needs investigation before retry. This defines SIGINT only; SIGTERM,
SIGHUP, standalone release and foreign-session takeover remain outside this change.
Offline process-signal tests establish control flow, not live hardware behavior.

## Considered & rejected

- **Only document the existing behavior.** judgment: it does not satisfy the operator's
  explicit retained-partial-output policy.
- **Add a global signal handler or a separate release path.** judgment: unnecessary
  ownership and lifecycle complexity when the existing cancellation cleanup owns release.
- **Always exit 130 on interrupt.** judgment: it hides the operator-approved precedence
  of release uncertainty.
