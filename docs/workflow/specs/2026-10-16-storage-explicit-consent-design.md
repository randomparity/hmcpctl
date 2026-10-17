# Explicit storage consent

## Problem and scope

Issue #1703, within #1695, standardizes the twelve existing storage consent
boundaries: create-vg, create-disk, delete-disk, attach-disk, map,
create-media-repo, create-media, delete-media-repo, delete-media,
mount-optical-media, unmount-optical-media and detach-mapping.
Upload-iso retains its existing no-prompt contract. Inventory is unchanged.
[ADR 0231](../../adr/0231-storage-explicit-consent.md) records the boundary.

## Design

Keep policy local to storage/resources.py: a single `_confirm_storage(yes, text)`
helper replaces the repeated prompt gates. Existing operation functions remain
owners of ownership, containment, mapped-disk and mounted-media guards.
`--yes/-y` skips only the prompt; replace both `--confirm` options outright.
Attach-disk skips consent only when its existing dry-run flag is true.

The helper returns immediately for explicit yes. Otherwise construct a shell-quoted
storage invocation from the current Typer command's parsed parameters using its
existing argument/option metadata. Include supplied scope, target and operation
parameters, omit false/None values and yes, encode enum values, place arguments
after `--`, and append `--yes` before that separator. Use canonical long options.
Printable values use shlex.quote; nonprintable values use Bash/Zsh ANSI-C quotes
with escaped backslash/apostrophe and filesystem-encoded byte escapes for controls,
independent of the shell locale. Thus terminal
encoding cannot change the executable argument. Label the retry as Bash/Zsh.
C and UTF-8 locale round-trip tests execute only a synthetic argv recorder, never a live CLI mutation.
Do not reconstruct root credentials: say to reuse the same connection options.
The displayed invocation is `hmcpctl storage COMMAND OPTIONS --yes -- ARGUMENTS`.
This preserves the scope/target without echoing root passwords into diagnostics.

Check real `sys.stdin.isatty()` before requesting input. Non-TTY stdin, including
an open pipe and EOF, emits an actionable error and the invocation on stderr,
then exits 2 before with_client. JSON/format/details do not grant consent.
For terminal stdin, keep existing action text and add the invocation identifying
the complete supplied scope/target. Pass dynamic text through existing
`terminal_field(..., err=True)` and prompt through `confirm_on_stderr`.
Decline or terminal EOF aborts with exit 1 before with_client. Stdout stays clean.

## Success

C1: The twelve commands accept --yes/-y; unmount and detach reject --confirm.
C2: Non-TTY stdin refuses before operation dispatch, regardless of supplied pipe
content or JSON mode, with a parser-valid scoped retry invocation on stderr.
C3: Terminal prompts identify provided VIOS/system/VG/LPAR/object selections and
use the existing actual-destination terminal policy without Rich markup parsing.
C4: Consent does not bypass operation guards or imply ownership override;
attach-disk dry-run still validates without prompting or mutation.
C5: Direct tests cover acceptance, decline, EOF, pipe and dry-run; affected help,
CLI migration notes and recipe examples use the new spelling.

## Global Constraints

Python 3.11–3.14; amd64 CI. No new dependency. No live HMC mutation.
Do not change selectors, mutation output schemas, terminal policy, ownership
override semantics, bulk deletion or automatic consent. The final composed
journey belongs to #1704. Reuse existing stderr and terminal encoding owners.

## Failure model

- Actors and deployments: local terminal operators and agents/CI invoking the
  installed CLI with stdin pipes, closed stdin, redirected outputs or a PTY.
- Invariants and assets: a refusal dispatches no mutation; explicit yes does not
  remove storage/ownership/containment protections; machine stdout stays parseable.
- Accepted failure classes: concurrent changes after consent are held by existing
  operation-level guards; no new transport behavior or hardware proof is claimed.
- Covered elsewhere: selectors #1697; mutation output #1699; terminal policy #1275;
  final journey #1704; existing operations own storage safety and authorization.

## Threat model

- Boundary inventory: stdin/CLI flags cross the consent gate; user-supplied scope
  and target enter human diagnostics. No new external capability is added.
- Actor model: the local operator controls arguments and stdin; HMC names may
  contain hostile terminal characters. Explicit yes is trusted consent only.
- Controls: stdin TTY check before prompt; shell quoting for copyable retry syntax;
  existing terminal_field for actual stderr destination; never include root
  passwords; unchanged operation validation/authorization after consent.
- Out of scope: shell credential management and hostile local process takeover;
  library/MCP callers and unrelated command groups retain their existing contracts.

## Validation

Focused storage tests enumerate twelve boundaries and prove rejection before
with_client, both yes spellings, obsolete option refusal, prompt scope, parser
round-trip of corrective commands and unchanged upload behavior. Real subprocess
pipes prove prompt-free return even when a producer holds stdin open; PTYs prove
yes/decline/EOF. Existing operation guard tests and attach dry-run tests remain.
Final `just verify` and `uv run --no-sync prek run --all-files`, then exact-head CI.
