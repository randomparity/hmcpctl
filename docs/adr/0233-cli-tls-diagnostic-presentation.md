# 0233 — CLI TLS diagnostic presentation

## Status

Accepted. Implements #1700 within #1695; complements ADR 0224.

## Context

The CLI prints Python warning locations and source lines, and unconfigured
logging prints TLS audit JSON through `lastResort`. The migration warning also
suggests a Boolean assignment the parser rejects. Warnings and audit events
have different lifetimes: logon warnings are throttled, audit is per construction.

## Decision

The existing CLI coroutine boundary temporarily formats the two TLS warning
categories as concise prose, retaining separate migration information and
executable `--verify-ssl` / `--no-verify-ssl` suggestions. Library warning
categories, warning filters, and client throttle ownership remain unchanged.

The audit sink owner supplies a scoped fallback handler only when the reserved
logger has no reachable configured handler. It retains the logger's level and
propagation policy. The handler uses synchronous stderr delivery, like the previous
`lastResort`,
and is removed and closed on exit. It adds no queue or drop policy. For actual stderr TTYs, the known TLS event becomes a
compact audit line containing its event identity, timestamp, host and source.
For redirected stderr, unknown shapes and other events, preserve the original
JSON message. The emitted LogRecord remains unchanged; configured sinks retain
it unchanged. This is a CLI display policy, not a change to ADR 0040's record
grammar or the server sink. No record is filtered or deduplicated here.

## Consequences

Interactive TLS diagnostics lose JSON punctuation and Python source boilerplate.
Redirected audit records remain machine-readable. Repeated clients still emit
separate audit events. The synchronous CLI invocation scopes formatter changes;
embedding concurrent CLI invocations in one interpreter is not supported.

## Considered & rejected

- **Leave output unchanged.** verified: a controlled `CliRunner` invocation on
  c11fc37e emits two filename/source warning blocks and audit JSON before its
  error; `--verify-ssl=false` exits 2 under Python 3.11.15.
- **Suppress audit or install a quiet switch.** verified: #1700 and ADR 0224
  require retained audit emission and security warnings.
- **Add a log file destination.** judgment: a new retention and failure contract
  is unnecessary when the existing logger and sink preserve the events.
