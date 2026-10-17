# ADR 0231: Explicit consent at existing storage CLI boundaries

## Status

Accepted for #1703 under the operator-approved #1695 campaign scope.

## Context

Storage mutation prompts use two affirmative option names and can consume piped
input. The requested migration uses one explicit flag and refuses noninteractive
input without that flag. Existing storage operations own the safety guards.

## Decision

Keep consent policy in the storage CLI adapter. Its twelve existing consent
boundaries use --yes/-y; remove --confirm. Non-TTY stdin without consent fails
before dispatch with a shell-quoted storage retry command retaining supplied
scope and target. Nonprintable arguments use reversible Bash/Zsh ANSI-C quoting
with filesystem-encoded byte escapes before terminal encoding; printable arguments use ordinary shell quoting. Root connection options must be reused separately, avoiding
password disclosure. Interactive prompts show that scope/target using existing
stderr terminal encoding. Decline and EOF cannot dispatch. Attach-disk dry-run
remains exempt; upload-iso retains its existing no-prompt behavior.

## Consequences

Scripts using --confirm must switch to --yes. Piping y no longer supplies
consent. Explicit yes bypasses only the prompt, not ownership, containment,
mapped-disk or mounted-media checks; ownership override stays separate.

## Considered & rejected

- Keep separate prompt gates: judgment: duplicated stdin policy would drift
  across twelve callers.
- Preserve --confirm as an alias: judgment: the approved pre-release migration
  asks to replace inconsistent syntax rather than retain compatibility paths.
- Infer consent from redirected input or JSON: verified: #1703 Expected
  explicitly forbids both.
- Do nothing: verified: #1703 Expected requires the consent migration.
