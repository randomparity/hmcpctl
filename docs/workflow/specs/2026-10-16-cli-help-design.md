# CLI help and recovery guidance

## Scope and success

Issue #1702 supplies C1–C5 in the frozen campaign charter. Help must lead with
inventory and expose lifecycle, console and storage tasks. Group help explains
root connection-option placement. Parser errors use stderr, usage, repair
advice and exit 2; a selected command reports its statically knowable missing
required inputs together. Unknown options retain parser priority, without
running speculative validation. CLI resource misses suggest executable CLI
syntax; shared MCP messages retain their present wording.

## Design

Extend the existing owners: `cli.py` composition assigns Typer command panels
and the existing command metadata's command class; `cli_commands/app.py` owns
small Typer subclasses for error interception and missing-input aggregation.
Use Typer's installed parser, parameter metadata and value-source facilities;
never tokenize argv ourselves. On MissingParameter only, parse the original
argument list with the existing parser and examine required inputs without
calling conversion callbacks, prompts, callable defaults or command bodies.
Environment and static defaults count as supplied; callable defaults are not
claimed missing. Invalid options and invalid values keep their normal error.
The root group intercepts usage errors from parsing and invocation, preserving
Typer's suggested-command message and normal no-argument help handling.

`output.usage_error` prints usage and a command-specific --help repair using
the active/error context, preserving existing terminal-field protection.
Misplaced --profile adds `hmcpctl --profile NAME GROUP COMMAND` explicitly.
`output.fail` recognizes ResourceNotFoundError and renders its typed kind and
selector with actual inventory commands (systems list, lpars list, vios list).
Unknown kinds retain their message. The domain exception remains unchanged.
`partition_not_found` also supplies `hmcpctl lpars list` guidance. Read failures
remain failures and never become empty inventories. Existing callers need no
migration; no new framework, dependency or parser is introduced.

## Failure model

- Actors and deployments: installed CLI users in local terminals and pipes;
  Python 3.11–3.14 on declared amd64; MCP users share domain resolution.
- Invariants and assets: C1–C5, no HMC activity during help or parser rejection,
  exit 2 for usage errors, preserved MCP guidance and domain failures.
- Accepted failure classes: callable defaults cannot be diagnosed without
  execution, so aggregate only statically knowable missing inputs; command-body
  conditional requirements retain their existing validation.
- Covered elsewhere: selectors #1696/#1697; formats #1698; TLS wording #1700;
  terminal policy #1275. Preserve those landed contracts.

## Threat model

- Boundaries: existing local argv/environment to Typer; HMC selector/errors to
  CLI stderr. No new network, privilege, file-writing or command execution path.
- Actors: local CLI user and remote HMC producing resource strings.
- Controls: Typer owns parsing; inspection never invokes callbacks/prompts;
  existing terminal_field protects dynamic stderr fields; literal recovery
  commands contain placeholders, not interpolated executable resource values.
- Out of scope: hostile Python embeddings and third-party monkeypatches;
  authentication and terminal policy remain their existing owners.

## Validation and constraints

Use existing Typer, no dependency changes. Python 3.11–3.14; amd64 CI.
Focused CLI tests normalize ANSI with click.unstyle, include forced color,
missing argument plus options, partial input, unknown option priority, typo
suggestions, group/help examples, typed resource misses and failed reads.
Patch REST/SSH creation to fail if help or parser errors touch HMC boundaries.
Use installed console-script subprocess help/error checks as integration proof.
Run focused tests, static gates, final just verify and pinned prek before push.
No live HMC run is authorized. Decisions: [ADR 0234](../../adr/0234-cli-help-and-recovery-guidance.md).
