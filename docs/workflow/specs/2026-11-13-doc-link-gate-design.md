# Relative Markdown link and anchor gate (#1972)

## Problem

No gate checks that a relative Markdown link or its `#fragment` resolves. A link to a
heading that never existed, or to a file at the wrong depth, passes every `static` gate
today. A prototype run over `main` at `25ba0c7b0` found 11 such links in 11 files.

## Design

`scripts/check_doc_links.py` is a stdlib-only guard, run by a new `just doc-links` recipe
that joins `static` and gets its matching prek hook (`id: doc-links`,
`entry: just doc-links`, `language: system`, `pass_filenames: false`).

1. **Inputs.** `git ls-files -z` runs at the repository root with every inherited
   `GIT_*` variable removed, as the sibling inventory gates do, so a redirected
   `GIT_DIR`/`GIT_WORK_TREE` cannot substitute a foreign index. Tracked paths still
   present in the work tree, and the directories containing them, are the resolvable
   targets; the tracked `*.md` files among them are the files checked. Untracked files —
   including a local-only reference corpus linked into the worktree — are neither read nor
   resolvable, and a tracked file deleted from the work tree resolves as `missing`.
2. **Link extraction.** Fenced code blocks (``` and `~~~`) and inline code spans are
   blanked first, keeping line breaks. Over the remaining text, so that link text wrapped
   across lines (but not across a blank line) is still seen, the guard reads inline links and images `[text](target "title")`,
   including `<target>` form, and reference definitions `[id]: target`.
3. **Skipped targets.** A target with a URI scheme (`https:`, `mailto:`, …) or starting
   with `//` is external and skipped.
4. **Path resolution.** The query string is dropped and the path percent-decoded. A path
   starting with `/` resolves from the repository root (GitHub's rule); any other path
   from the linking file's directory. An empty path means the linking file itself. A
   resolved path that is not a tracked file or directory is reported `missing`.
5. **Anchors.** A fragment is checked only when the target is a tracked `.md` file. The
   target's anchors are its ATX headings outside fenced code blocks slugged the GitHub way — inline code kept as
   text, link text kept and URL dropped, HTML tags and `*` removed, lower-cased, every
   character that is not a word character, space or hyphen removed, spaces turned into
   hyphens, and a repeated slug suffixed `-1`, `-2`, … in document order — plus the
   values of `id`/`name` attributes on `<a>` tags. Matching is exact, after
   percent-decoding the fragment. An unmatched fragment is reported `anchor`.
6. **Output.** Exit 0 with a one-line `OK` count; otherwise exit 1 and print
   `<file>:<line>: <missing|anchor>: <target>` per broken link to stderr, the line being
   where the link starts. Exit 1 with one stderr line, never a traceback, when
   `git ls-files` cannot run or fails, when it lists no Markdown file, or when a checked
   file is not UTF-8.
7. **Existing breakage** found by the first run is fixed in this change: wrong-depth ADR
   links are corrected, links into the gitignored `docs/workflow/plans/` become plain
   text, and the `docs/live-testing.md` link to a heading that never existed points at
   the vNIC retained fixture design spec that defines the packet it names.

8. **Documentation.** `AGENTS.md` lists `just doc-links` with the other `static` gates,
   and its documentation-only policy names it beside `just secrets` as a repo-wide scan:
   it reads every tracked Markdown body, so counting it as a content assertion would make
   that policy's exception dead.

No dependency is added: the slug rules above are a few lines of `re`.

## Failure model

1. **Actors and deployments**
   - developer running `just verify` / prek locally (macOS, Linux)
   - CI `static` job (four Python legs, after `just setup`), through
     `prek run --all-files`
2. **Invariants and assets at stake**
   - every relative link in a tracked Markdown file names a tracked path, and its
     fragment names an anchor GitHub renders for that target
   - the gate reads tracked files only; it never opens the local-only corpus
3. **Accepted failure classes**
   - setext headings (`Title` + `===`) are not anchors: none exist in tracked files; a
     link to one fails loudly and the fix is an ATX heading
   - fragments on non-Markdown targets (`file.py#L10`) are not checked: GitHub resolves
     them from rendered line numbers the guard cannot see
   - HTML `href`/`src` attributes and autolinks are not checked: only external URLs use
     them in tracked files
   - Markdown edge syntax the regexes do not model (nested brackets beyond one level,
     a link split by a blank line) may be missed: no tracked file uses it
   - `_`/`~~` emphasis inside a heading is not stripped before slugging, so a link to such
     a heading is reported `anchor`: no tracked heading uses it outside a code span, and
     the failure is loud
4. **Covered elsewhere**
   - generated pages (`docs/tools/`, banner pages) are regenerated by
     `just tool-docs` / `just doc-freshness`; a broken link there is fixed in the
     generator, which this gate then re-checks
   - external URL liveness: out of scope (issue: skip external URLs)

## Success

- `just doc-links` exits 1 naming the file, line and target for a tracked `.md` whose
  relative link resolves to no tracked path, and for one whose fragment matches no
  anchor of a tracked `.md` target (issue *Expected*).
- It exits 0 on the repository after the fixes in item 7.
- `static` and `.pre-commit-config.yaml` list `doc-links`, and
  `tests/test_ci_pipeline.py`'s `STATIC_GATES` and recipe-command table include it.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| missing target reported | focused-test | `tests/scripts/test_check_doc_links.py` missing-file case, red before the script exists |
| resolution forms (`/`-rooted, `#self`, `<…>`, title, reference definition, query, percent-encoding, image, wrapped text) | focused-test | same module, one parametrized case per form with a resolving and a missing variant |
| anchor slugging, duplicates, fenced `#` lines, code spans, HTML anchors | focused-test | same module; controlled faults: drop the duplicate counter, stop skipping fences |
| external, fenced and code-span links skipped | focused-test | same module; each fixture also holds one broken link that must be the only report; controlled fault: stop blanking code spans |
| untracked or deleted target, redirected `GIT_*` | focused-test | same module, git repo fixtures and `redirected_git_checkout` |
| git failure, empty inventory, non-UTF-8 file | focused-test | same module, exit 1 with one message |
| committed repository passes | focused-test | same module, `main([])` on the real tree |
| `static` ↔ prek wiring | focused-test | `tests/test_ci_pipeline.py` with `doc-links` added to `STATIC_GATES` |
| existing link fixes | task-test-not-applicable | prose edits; the committed-repository case above is what observes them |
