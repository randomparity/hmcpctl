# ADR 0187: CLI output prints HMC and argument text verbatim

## Status

> **Control-character passthrough superseded by [0229](0229-cli-terminal-control-policy.md)** (2026-10-16); markup and styling decisions remain in force.

Accepted on 2026-10-01 for issue #1029.

## Context

`hmcpctl` prints through one shared Rich `Console` in `cli_commands/output.py`. Rich
parses every `str` it renders, whether a `print` argument or a table title, header or
cell, as console markup and emoji codes. HMC names, raw REST bodies and CLI arguments
that contain `[word]` or `:word:` were silently altered. A crafted argument could also
rewrite a confirmation line (#1029, after #965). Roughly 100 print sites and every
listing table were affected, and the CLI's own styling, such as `[green]...[/green]`,
shares that console.

## Decision

1. Both shared consoles are built with `emoji=False`.
2. `output.VerbatimTable` is the only table class in `cli_commands/`. It wraps each
   `str` title, caption, column header and row cell in `rich.text.Text`, which Rich
   never parses as markup.
3. A print that interpolates a value passes `markup=False`, with colour from `style=`;
   mixed styling is built with `Text.assemble`. `raw get`/`raw post` write the body with
   `typer.echo(..., color=True)`, so neither Rich nor click alters it.
4. An AST test over `cli_commands/` enforces items 2 and 3 for the markup-parsing
   `console`/`err_console` methods, `from_markup`/`render` calls, and imports from `rich`
   other than `rich.text`, and names the offending file and line.

## Consequences

- A new print site that interpolates a value fails the test until it passes
  `markup=False` or uses `Text`. A new `rich` import other than `rich.text` outside
  `output.py` also fails it. `lpar/profiles.py` keeps one `escape()` line until
  #1248 changes it.
- Styling written in code keeps working: the console is still markup-enabled.
- `raw get`/`raw post` output is uncoloured and no longer wrapped at 80 columns; HMC
  control characters and ANSI codes in the body reach the terminal unchanged.

## Considered & rejected

- **Global `Console(markup=False)`, with styling moved to `style=`.** verified: under a
  `markup=False` console, `print(f"[green]Boot order set to: {escape('a[b]')}[/green]")`
  printed `[green]Boot order set to: a\[b][/green]` (rich 15.0.0, at this change's base).
  That is `lpar/profiles.py:75`, which #1248 owns concurrently, so the flip would break
  its output outside this change's surface. Per-site `markup=False` reaches the same
  verbatim output everywhere else without touching that file.
- **Wrap each interpolated value in `rich.markup.escape`.** verified: with rich 15.0.0,
  `print(f"[green]A '{escape('x' + chr(92))}'[/green]")` printed `A 'x\\'`; `escape()`
  appends a backslash that Rich removes only directly before a tag.
- **Wrap every table cell in `Text` at the call site.** judgment: about 36 `add_row`
  calls with several cells each, and every future cell becomes a new place to forget.
- **Do nothing beyond the issue-listed sites.** judgment: it leaves the remaining sites
  with the same defect and no guard, which is how #965 left this issue behind.
