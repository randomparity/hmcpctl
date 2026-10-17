# ADR 0229: Encode terminal controls at CLI field boundaries

## Status

Accepted for #1275 under the operator-approved #1695 campaign policy (2026-10-16).
Supersedes ADR 0187 only for control-character passthrough; its markup and style policy remains.

## Context

Rich `Text` and `markup=False` prevent markup interpretation but leave terminal controls active.
A real PTY probe of `raw_get` on base `b6f63f1b` emitted ESC, BEL and bidi characters unchanged.
HMC names, returned messages and arguments cross the same human-output boundary.

## Decision

`cli_commands/output.py` owns visible encoding: keep printable Unicode, render each other
character as Python's `ascii(character)[1:-1]` escape (including C0/C1, ESC and bidi controls).
Human message and prompt fields use the actual destination's terminal state, independently
for stdout and stderr. Encode fields before composing trusted layout, never an entire
formatted multiline message. Table fields use the same encoder; explicit human tables are
safe even when redirected, retaining the existing refcode-table contract.

Raw REST responses use encoding only on terminal stdout; redirected responses keep their
existing bytes and trailing newline. Captured console bytes and session handling are unchanged.
JSON uses JSON escaping on terminals so decoded values remain identical. Explicit TSV remains
the #1698 machine format, including its existing reversible delimiter encoding; it is not a
human display format. CLI documentation explains these boundaries.

Route direct print, styled Text, confirmation and raw-response consumers through the owner.
The existing AST guard rejects unencoded dynamic fields and unowned text-output entry points.
No operation result or persisted value is rewritten.

## Consequences

Human fields show escapes instead of embedded line breaks or terminal commands. Trusted
layout, literal brackets, emoji-like tokens and existing styles remain. Redirected raw data,
capture bytes and explicit machine formats remain suitable for consumers; displaying those
files in a terminal later is the consumer's responsibility.

## Considered & rejected

- **Global post-render filtering.** judgment: loses field/layout provenance and can remove
  the application's own terminal styling.
- **Strip only ESC.** verified: Python `str.isprintable` classifies C1 and bidi controls as
  nonprintable too; removing ESC alone leaves other control characters active.
- **Escape raw pipes too.** judgment: violates the operator-approved byte-fidelity exception.
- **Keep markup-only protection.** verified: the base PTY probe above still emits OSC/BEL/bidi.
