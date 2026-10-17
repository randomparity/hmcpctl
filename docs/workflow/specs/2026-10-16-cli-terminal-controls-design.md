# CLI terminal control boundaries

## Problem and authority

#1275 identifies terminal injection after ADR 0187's markup fix. The frozen scope is
issue #1275 comment 6226247956 (q1275-c5210b08), under native epic #1695 and the operator's
2026-10-16 approval. [ADR 0229](../../adr/0229-cli-terminal-control-policy.md) records policy.

## Scope and design

One owner, `cli_commands/output.py`, provides character encoding and destination-aware field
formatting. Keep printable Unicode; escape each nonprintable with `ascii(char)[1:-1]`.
`terminal_field(value, *, err=False)` converts one field after its ordinary formatting,
selecting actual stdout/stderr `isatty()` independently, not Rich color-capability overrides. `escape_controls(value)` supplies the same encoding
for human tables regardless of redirection, retaining refcode behavior. `print_raw(body)`
selects terminal stdout versus unchanged redirected `typer.echo(color=True)` output.

Migrate dynamic fields in the CLI's current print and confirmation sites, including
Text.assemble fragments, legacy profile markup, capture diagnostics, configuration diffs
and resource summaries. Literal text, layout newlines, indentation and style arguments stay
trusted. Existing VIOS/vNIC quoted prompt encodings stay safe and retain their presentation.
Table strings are encoded centrally; explicitly styled status cells encode their value.
Remove the duplicate refcode character loop in favor of the owner.

`fail` encodes exception text; `usage_error` encodes its dynamic message. Known multiline
startup guidance must remain separately composed trusted layout rather than disappearing
inside the encoded exception. Move config validation guidance out of the exception and into the two CLI callers, using
an explicit guidance parameter. Serve uses the same parameter for separately composed
guidance whose fields are encoded first. Explicit CLI-created BadParameter messages also encode external
fields before handing them to Typer. The parser's general error presentation remains #1702.

JSON rendering uses JSON escapes at the final serializer (including Rich print_json), not
replacement of values, on terminal stdout. Route config JSON through the same owner. Explicit TSV
retains #1698's machine contract (including when explicitly selected on a TTY). Captured
console byte writes and raw redirected responses remain unchanged. No operation, schema,
authorization, confirmation consent or session behavior changes.

Extend the existing AST guard with a terminal-field predicate for dynamic print/prompt/Text
fields and raw echo ownership, plus negative snippets proving it rejects bypasses. This is a
regression guard over documented source forms, not a Python taint analyzer. Keep meaningful
behavior tests for every boundary; do not infer safety from AST approval alone.

## Success

The enumerated human output sinks render C0/C1/ESC/bidi and embedded field newlines visibly;
printable Unicode/brackets remain literal. Trusted multiline startup guidance and styles remain.
PTY and redirected raw tests prove the policy split. Independent stdout/stderr terminal tests
prove correct destination selection. JSON decoding reproduces the original values; inventory
TSV and capture-byte regressions remain green. Existing refcode tables retain sanitation.
No new direct output sink in the guarded forms can omit field encoding unnoticed.

## Failure model

- Actors and deployments: local CLI operators with terminals, redirected subprocess/agent
  consumers, CI running offline fakes; HMC resource authors and caller arguments are untrusted.
- Invariants/assets: terminal display integrity, truthful prompts, stdout/stderr separation,
  byte fidelity for raw/capture, stable JSON/TSV values, preserved trusted layout and styles.
- Accepted failure classes: explicit machine streams shown on a terminal may contain controls
  under their preserved machine contracts; downstream display of captured files belongs to
  their consumer. AST alias/flow analysis beyond the bounded source forms is not a guarantee.
- Covered elsewhere: TSV delimiter/schema #1698; mutation JSON schema #1699; console bytes and
  session semantics #1692; parser diagnostics/help #1702; final workflow composition #1704.

## Threat model

- Boundary inventory: HMC strings and operator arguments into human terminal print/prompt/table
  fields; raw body into terminal stdout; JSON into terminal stdout. No new remote entry point.
- Actor model: resource names/messages may be controlled by another HMC user; CLI arguments
  may come from files or automation. Code literals and style specifications are trusted.
- Controls: visible nonprintable encoding at human field boundaries; JSON serialization keeps
  values while escaping controls; no terminal decoding. Errors retain meaningful field content.
- Out of scope: terminal emulation vulnerabilities in printable text, Unicode confusables,
  deliberate byte-stream consumers, live HMC mutations, and global logging/framework redesign.

## Validation

Use real PTYs and subprocess pipes with synthetic data, never a live HMC. Add unit cases for
C0/C1/ESC/bidi, Unicode, quotes/backslashes, newlines and empty fields; real commands cover raw
GET/POST, representative table/messages/prompts and stderr errors. Test trusted layout separate
from field newlines, styled Text, refcodes, JSON round trips, and existing TSV/capture contracts.
Include TTY_COMPATIBLE=0 on real PTYs and FORCE_COLOR=1 on real pipes.
Extend AST snippet tests and prove a controlled unsafe sink fails. Run focused CLI tests while
iterating, then final `just verify` and `uv run --no-sync prek run --all-files`, followed by CI.
