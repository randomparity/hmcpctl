# ADR 0097: Generating the tool reference from the registry

## Status

> **Superseded in part by [0207](0207-ci-gates-once.md)** (2026-10-08): hosted orchestration only; remaining guarantees stand.

> **Amended in part by [0251](0251-readme-links-resolve-per-revision.md)** (2026-10-19): the README pointer is relative; remaining guarantees stand.

Accepted (2026-08-25)

## Context

`README.md` carried a hand-maintained MCP tool reference: 18 bold-headed sub-tables,
126 rows, each row a tool name and a curated one-line description. The registry it
described holds 148 tools.

It had drifted in both directions. 31 registered tools had no row at all. Nine rows
named tools that no longer exist anywhere in `src/` — `hmc_configure_ldap`,
`hmc_get_ldap_config`, `hmc_remove_ldap_config`, the six password-policy tools, and
`hmc_assign_profile_io_slot`. #477 established that all nine were **removed** with
decision records behind them (ADR 0076 and ADR 0055); none was aspirational. The
README simply outlived the deletions.

Nothing guarded any of it. The drift was already measurable on every CI run —
`scripts/smoke_mcp.py` performs an MCP `list_tools()` walk and `just smoke` prints
"147 tools exposed" — but nothing compared that number, or the names behind it, to
the document. A reader had no way to tell a current row from a stale one, and
documenting tools that do not exist is precisely what the repository's
no-phantom-features rule forbids.

The registry can answer the question the table was trying to answer.
`TOOL_SECURITY` (`src/hmc_mcp/server.py:304`) is a module-level mapping assembled at
import time from every module's `tool_security()` projection
(`src/hmc_mcp/tool_registry.py:546`) plus an `extra` mapping for `hmc_run_command`
and `hmc_effective_permissions`. Each value is a `ToolSecurity`
(`tool_registry.py:160`) whose `effect`, `operation` and `target_kind` have no
defaults, so a tool cannot reach the registry without them. `list_tools()` supplies
the name, description and input schema. Neither needs a credential or a network
call, and `tool_module()` keeps its `definitions` list closure-private, so none of
this requires opening the handler surface.

## Decision

**Generate `docs/tools/` from the registry, delete the README table, and gate the
result on a diff.**

`scripts/gen_tool_reference.py` composes the application, walks `list_tools()`,
joins each description to its `ToolSecurity` record, and renders one page per
operation domain plus an index. `just tool-docs` writes the tree; `just
tool-docs-check` regenerates in memory and fails on any difference. The generated
pages are committed, so a reader on GitHub sees the reference without running
anything, and a reviewer sees registry changes and their documentation in one diff.

Six decisions inside that:

- **The reference documents the registered set — all 148 tools — not the 147 a
  default deployment exposes.** `hmc_run_command` is registered but withheld: it is
  the arbitrary-command escape hatch, and reaching it needs both
  `--enable-arbitrary-command` and an access policy that names it (ADR 0036's
  conjunctive gates). Documenting only the exposed set would leave the single
  highest-risk tool in the package absent from the package's own reference, which is
  the opposite of what a security-relevant document should do. Every page carries
  that scope in its banner, and `build_records` **raises** when the walk does not
  cover `TOOL_SECURITY` exactly — in either direction — so a tool cannot fall out of
  the document silently. Exposure is derived, not asserted: the generator compiles
  the default legacy-equivalent policy and asks `permits_tool`, and names whatever
  that withholds in a blockquote on the tool's page and on the index.

- **One page per operation domain, with no catch-all for the singletons.** 34
  domains hold the 148 tools and 10 of them hold exactly one. Folding the singletons
  into an `other.md` would make a page's membership depend on *other* tools' counts:
  adding a second `capacity` tool would move `hmc_capacity_report` off `other.md`
  and rewrite two pages plus every link into them. With one page per domain, a
  tool's page is a pure function of its own `operation`, so a link into the
  reference stays valid until the tool's operation itself changes. The index carries
  a full 148-row `Tool -> page` table, so a one-tool page is never the only way to
  find a tool.

- **Grouping is a parameter, not a rule.** `render_pages(records, *, group_key=...)`
  takes the grouping function, defaulting to the record's operation domain. A group
  that would be named `index` raises rather than having its page silently overwritten
  by the index page. The tool count is itself
  a known concern and workflow tools that hide low-level ones are planned, so when a
  `tier` field arrives the change is a different `group_key`, not a rewrite of the
  renderer. **No such field is added here.** The default derives the domain by
  splitting the operation string: `_OPERATION` (`tool_registry.py:46`) is private
  and constrains only the shape — two lowercase dot-separated segments — never the
  vocabulary.

- **Summaries are the first line of the handler docstring.** All 146 module-level
  tool functions have multiline docstrings with an `Args:` block, so kdive's
  raise-on-newline rule does not transfer; the generator takes the first non-empty
  line and raises on a blank description. This retires the README's curated
  one-liners in favour of docstring summaries. The tone changes visibly — "HMC
  version/network info; cheap connectivity check" becomes the docstring's own first
  sentence — and that is the trade for a document that cannot be wrong.

- **The README pointer is an absolute repository URL.** `pyproject.toml:5` makes
  `README.md` the PyPI long description and the sdist include list ships
  `/LICENSE`, `/README.md`, `/pyproject.toml`, `/src/hmc_mcp` — not `docs/`. Adding
  `docs/tools` to the sdist would not help: PyPI does not rewrite relative links, so
  the link dangles on the package page whether or not the files are in the archive.
  An absolute URL works from both GitHub and PyPI and leaves the packaging
  configuration untouched.

- **The check is wired into CI as its own step as well as through
  `static` -> `verify`.** This is a **new pattern for this repository**, adopted
  deliberately and not copied from anything. Every existing script gate — `env-vars`,
  `nicknames` — reaches CI only through the umbrella recipe, and `ci.yml` invokes
  only `just setup` and `just verify`. A named step reports generated-docs drift as
  its own failed check with its own name, ahead of a 20-minute verification suite,
  which is the difference between "regenerate and commit" and reading a build log.
  The recipe is the single source of truth; only its invocation is duplicated,
  which is what `CLAUDE.md` asks of hooks and CI alike. Membership in `static` is
  what keeps a local `just verify` honest, and a prek hook — one per `static`
  member, as every other gate already has — is what makes the drift a failed commit
  rather than a failed CI run twenty minutes later.

## Consequences

- The tool reference cannot drift. `just tool-docs-check` compares a fresh
  generation against the committed tree from both sides — a hand edit to a page and
  a registry change without a regeneration both fail it — and
  `tests/scripts/test_gen_tool_reference.py` makes the same comparison inside the
  suite, so the gate holds even for someone who runs only `just test`.
- **The diff gate proves the copy is current, never that it is right.** Both sides
  of the comparison come from the same generator, so it is structurally incapable of
  catching a rendering bug. The generator's own tests carry that weight instead:
  determinism over an unordered registry, one test per raise path, the pipe-escaping
  contract, and the banner-and-scope contract, all against synthetic registries.
- Adding a tool now changes two committed files instead of one, and forgetting the
  second is a failed check rather than a silent omission. `just tool-docs` is the
  whole remedy and the failure message says so.
- The reference is 35 files where it was one section. Renaming a domain renames a
  page; `write_pages` deletes the pages it emitted before and no longer emits, so
  the directory cannot accumulate orphans, and `--check` reports one it finds. It
  identifies its own output by the banner and **raises** rather than deleting a
  Markdown file it did not write, so a mistaken `--output` cannot destroy
  hand-written documentation.
- Descriptions are now whatever the docstring's first line says. A vague first line
  is now a documentation defect with a visible blast radius, which is the intended
  pressure.
- A per-domain page URL is now a public-ish surface. It is stable under tool
  additions and removals and moves only when an operation's domain segment changes.

## Alternatives considered

- **Keep the table and add a check that it matches the registry.** Rejected: it
  keeps the maintenance burden and adds a gate, and the check would still have
  nothing to say about the 126 descriptions, which are the part that actually
  carries information. Generating is strictly less work than validating a copy.
- **Generate only the 147 exposed tools.** Rejected above: it hides the escape
  hatch from its own reference.
- **Generate at build time and do not commit the pages.** Rejected: the pages are
  the deliverable a reader reaches from the README, and an uncommitted artifact is
  invisible on GitHub and absent from a review diff. Committing them is what makes
  the diff gate possible at all.
- **A catch-all page for the ten singleton domains.** Rejected above, on page
  stability.
- **A separate CI job rather than a step in the existing matrix job.** Rejected:
  it needs its own checkout, uv and just setup, and `tests/test_ci_pipeline.py`
  pins the runner and checkout counts precisely so an extra job is a reviewed
  change. A step inside the matrix job costs nothing extra and additionally proves
  the generator is deterministic across Python 3.11-3.14 and both architectures.
