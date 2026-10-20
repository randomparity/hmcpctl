# README links resolve per revision — design

**Issue:** #1481
**ADR:** [ADR 0251](../../adr/0251-readme-links-resolve-per-revision.md)

## Problem

Twelve `README.md` links into this repository's `docs/` are absolute `blob/main` URLs, so a
reader of a tag or older commit lands on `main`'s documentation.

## Scope

- `README.md`: rewrite each `https://github.com/randomparity/hmcpctl/blob/main/<path>` link
  to the relative `<path>`, keeping any `#anchor`. No other README text changes.
- `tests/test_project_metadata.py`: one new test, `test_readme_links_repository_files_relatively`,
  asserting (a) the README contains neither `github.com/randomparity/hmcpctl/blob/` nor
  `github.com/randomparity/hmcpctl/tree/`, and (b) every Markdown inline link target
  `](target)` without `://` and not starting with `#` names, before any `#`, a path that
  exists under the repository root.
- `tests/scripts/test_gen_tool_reference.py`: `README_POINTER` becomes
  `](docs/tools/index.md)`, and its comment cites ADR 0251 instead of the package-page reason.
- `docs/development.md`: unchanged. The release step already bumps only the install pins,
  and relative links need no release edit (ADR 0251, Consequences).
- Not changed: `pyproject.toml` `[project.urls]`, non-README docs, CHANGELOG links
  (exclusions in the issue's `WORK:SCOPE`).

## Failure model

1. Actors and deployments: a reader viewing README on GitHub at any ref; a local reader of a
   checkout; CI running `just test`.
2. Invariants and assets at stake: every README link to a repository file opens that file at
   the viewed ref; the release procedure is unchanged.
3. Accepted failure classes:
   - Long description in built wheel/sdist metadata carries unresolvable relative links:
     no package index publishes it (ADR 0024); recorded in ADR 0251.
   - A stale `#anchor` passes the test: anchors were unchecked before; bounded to a scroll.
   - Reference-style links (`[x]: target`) escape check (b): README uses none today.
4. Covered elsewhere: `[project.urls]` form, separate owner per `WORK:SCOPE`; package-index
   publishing, ADR 0024.

## Success

- `grep -c 'hmcpctl/blob/' README.md` prints `0`.
- The new test and `test_the_readme_points_at_the_generated_reference_and_keeps_no_tool_table`
  pass, as do the existing `test_readme_*` tests.
- Reintroducing one `blob/main` link, or a relative link to a missing file, fails the new test.

## Validation

- Contract: README carries no absolute self-repository file link. Mode: focused-test —
  `test_readme_links_repository_files_relatively`, red before the README edit.
- Contract: every relative README link names an existing path. Mode: focused-test — same
  test; bite checked by a temporary missing-path link.
- Contract: tool-reference pointer is relative. Mode: focused-test —
  `test_the_readme_points_at_the_generated_reference_and_keeps_no_tool_table`, red before the
  README edit.
- Contract: ADR 0251 decision record. Mode: task-test-not-applicable — prose rationale with no
  executable consumer; `just adr-numbering` checks only its name and H1.
