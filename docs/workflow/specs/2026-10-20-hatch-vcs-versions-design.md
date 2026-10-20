# Git-derived package versions with hatch-vcs — design

Issue #1471. Decision record: [ADR 0250](../../adr/0250-git-derived-versions-with-hatch-vcs.md),
superseding ADR 0033.

## Problem

`[project] version = "0.1.0"` is static, so every build after the `v0.1.0` tag reports
`0.1.0`. A dev build cannot be told apart from the release or from another dev build.

## Design

1. **Metadata.** `pyproject.toml` drops `version`, declares `dynamic = ["version"]`, adds
   `hatch-vcs==0.5.0` and `setuptools-scm==10.3.4` to `[build-system] requires` and to the dev
   group, and adds `[tool.hatch.version]` with `source = "vcs"` and
   `raw-options = { dist_name = "hmcpctl" }`. `uv lock` records the project as dynamic. No
   `[tool.uv]` table, no `fallback-version`. `src/hmcpctl/__init__.py` is unchanged.
2. **CI.** The `ci` job's checkout gains `fetch-depth: 0`. The other four active checkouts stay
   shallow. The `wheel-smoke` job lists `dist/` before the smoke step so each leg's log shows
   the wheel filename, which carries the derived version.
3. **Tests.**
   - `tests/test_package_version.py` keeps its Git-less copied-tree build, now with
     `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL` set to a sentinel, and asserts the sentinel
     reaches wheel, sdist rebuild, installed and editable metadata. A new test asserts the
     same build without the variable fails and names it. These tests and the history test
     remove every inherited `SETUPTOOLS_SCM_*` and `VCS_VERSIONING_*` variable first. A new history test clones
     `PROJECT_ROOT` into a temporary directory with `GIT_CONFIG_GLOBAL=/dev/null` and
     `GIT_CONFIG_NOSYSTEM=1`, and asks the project's own `[tool.hatch.version]` table, through
     `hatch_vcs`'s `VCSVersionSource`, for the version at: `v0.1.0` (`0.1.0`); a commit one
     past it (`0.1.1.dev1+g<sha>`); `v0.1.0` with a modified tracked file (a `.dYYYYMMDD`
     local marker); and a `--no-tags` clone (`0.1.devN+g<sha>`).
   - `tests/test_release_artifacts.py` copies the tree into a fresh `git init` without a
     commit, which would build `0.1.dev0+dYYYYMMDD`; its subprocess environment sets the
     pretend variable and its fixture asserts the wheel carries it. `tests/test_ci_pipeline.py`'s
     dirty-command test builds the same shape but asserts nothing about the version, so it
     only has its ADR 0033 comment updated.
   - `tests/test_ci_pipeline.py`'s checkout-depth test becomes: exactly the `ci` job's
     checkout has `fetch-depth: 0`, the others have none.
   - `tests/unit/test_changelog.py` reads the latest reachable release tag
     (`git describe --tags --abbrev=0 --match 'v[0-9]*'`) instead of `[project] version`.
   - `tests/test_supply_chain.py` needs no change: its parametrized pin tests cover the new
     requirements. The plan confirms that by running it.
4. **Docs.** `docs/development.md` Releasing: a release is the annotated tag; step 1 no longer
   bumps or re-locks; the freeze bullet stops listing `pyproject.toml`/`uv.lock`; step 4
   creates the tag locally, runs `just build` (expect `hmcpctl-X.Y.Z-*.whl`),
   `tests/unit/test_changelog.py` and release-mode artifact validation on it, and only then
   pushes. `CONTRIBUTING.md`'s changelog bullet (outside the dispatched file hint, flagged in
   the PR) says the newest release tag needs a heading. AGENTS.md
   gains one sentence that builds of an untagged copy need the pretend variable. ADR 0033 gets
   the superseded banner. CHANGELOG `[Unreleased]` gets a `### Changed` entry.

## Success

- S1. The history test's four cases pass with the exact forms above. (AC 1)
- S2. None of the four test modules this change edits or adds to (`test_package_version.py`,
  `test_release_artifacts.py`, `test_ci_pipeline.py`, `unit/test_changelog.py`) passes a
  `commit`, `commit-tree`, `tag` or `merge` argument to `git`. AC 2 is read as the version
  machinery's fixtures (#237); commit fixtures in the live-runner and release-window tests
  predate this change, build no package, and are out of scope. (AC 2)
- S3. `just verify` and `uv run --no-sync prek run --all-files` pass; each CI `wheel-smoke`
  leg's log shows a wheel named with a `.dev` version. (AC 3)
- S4. ADR 0250 supersedes ADR 0033 and `just adr-numbering` passes. (AC 4)
- S5. The Git-less build with the variable yields the sentinel everywhere, and without it
  fails naming the variable; the sdist rebuild yields the sdist's version. (dispatch hazard)

## Failure model

1. **Actors and deployments**
   - developer at a full clone with tags; GitHub Actions `ci` job; test fixtures building
     copied trees.
2. **Invariants and assets at stake**
   - a build from a committed checkout or an sdist has a valid PEP 440 version that is the
     exact release or names its commit; the `ci` job must never build from a shallow checkout.
   - fixtures must not commit (the #237 hook failure).
3. **Accepted failure classes**
   - shallow or tag-less checkouts outside CI build `0.1.devN`: nothing is published
     (ADR 0024); ADR 0250 records it.
   - editable `__version__` lagging until `pyproject.toml` changes or a reinstall: nothing
     reads it in development.
   - GitHub release source archives need the pretend variable to build: no documented install
     path uses them (ADR 0250).
   - a commit-less `git init` builds `0.1.dev0+dYYYYMMDD`: only test fixtures do this, and the
     one that inspects artifacts sets the pretend variable.
   - build-environment transitive dependencies resolving fresh: unchanged from hatchling today.
   - the history test and `test_changelog.py` require a reachable `v*` tag; a tag-less clone
     fails them with a message to fetch tags.
4. **Covered elsewhere**
   - publishing and its version policy: ADR 0024 (excluded).
   - exact-pin and lock agreement of build requirements: `tests/test_supply_chain.py`.
   - artifact internal consistency: `tests/validate_release_artifacts.py`.

Threat model: the change alters a build dependency and a CI checkout setting. Boundary added:
two pinned PyPI build plugins, executed at build time (control: exact pins equal to the lock,
`test_supply_chain.py`). Boundary widened: the `ci` job fetches all refs (control: unchanged
`persist-credentials: false` and `contents: read`). Out of scope: build-time transitive
dependencies, already unpinned for hatchling.
