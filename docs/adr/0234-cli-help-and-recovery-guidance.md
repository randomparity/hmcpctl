# ADR 0234: CLI help and recovery guidance

## Status

Accepted.

## Context

Issue #1702 requires task-oriented help, useful parser failures and CLI-specific
lookup recovery without changing the domain guidance consumed by MCP.

## Decision

Use existing Typer help panels and parser hooks at CLI composition. Aggregate
only missing inputs the installed parser and parameter metadata can determine
without callbacks, prompts or callable defaults. Keep unknown-option and invalid
value precedence. Render usage failures through the CLI output owner, including
usage and help repair, retaining exit 2 and typo suggestions.

Translate typed resource misses in the CLI output boundary using literal CLI
inventory commands. Leave domain exception messages unchanged for MCP callers.

## Consequences

Help and parser rejection stay offline. Existing domain errors remain useful to
MCP users; CLI users receive installed CLI syntax. Conditional command-body
requirements retain their current validators.

## Considered & rejected

- Bespoke argv parsing: judgment: duplicates Typer and exceeds the requested scope.
- Replace tool names throughout domain messages: judgment: would make MCP recovery
  less useful and place presentation ownership in shared operations.
- Validate commands speculatively after parse errors: judgment: risks side effects
  and conflates unknown-option failures with missing inputs.
