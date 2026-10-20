# ADR 0033: Declare the package version statically

## Status

Accepted

> **Superseded by [0250](0250-git-derived-versions-with-hatch-vcs.md)** (2026-10-20)

Supersedes [0023](0023-git-derived-package-versions.md).

## Context

ADR 0023 derived the version from Git history via a project-owned `versioningit` plugin:
`scripts/versioning.py` (147 lines) selecting the highest canonical `X.Y.Z` tag reachable
from `HEAD`, computing the next release from a `release-line` setting, and rejecting dirty or
shallow repositories before producing metadata. `tests/scripts/test_versioning.py` (287 lines)
tested it.

Two facts, checked against the repository, undermine the trade:

- **There are no tags.** `git tag` returns nothing for the whole history, so the
  tag-selection code, the release-line bump, and the canonical-tag validation have never run
  against a real tag. Every build has taken the no-tag fallback, producing a version of the
  form `0.1.0.dev<distance>+g<sha>`.
- **Nothing is published.** ADR 0024 excludes publication; CI retains a wheel as a build
  artifact for a downstream install test. No index serves this package and no consumer pins a
  version. The version's whole consumer surface is `hmc_mcp.__version__` via
  `importlib.metadata`; the CLI has no `--version` flag.

The cost is not confined to those two files. Requiring a clean, complete Git repository for
every build forces four test modules to `git init` and `git commit` into ephemeral fixture
repositories, and those commits inherit the developer's global Git configuration. On machines
enrolled in the IBM Vault Radar Block Secrets pilot the pilot's `pre-commit` hook rejects them,
which is issue #237 — 108 test errors caused entirely by how this project names its builds.

## Decision

Declare the version statically in `pyproject.toml`:

```toml
[project]
version = "0.1.0"
```

Remove `versioningit` from `build-system.requires` and the dev dependency group, delete
`[tool.hatch.version]` and the `[tool.versioningit.*]` tables, drop `/scripts/versioning.py`
from the sdist include list, and delete that module with its tests.

`src/hmc_mcp/__init__.py` continues to read `importlib.metadata.version("hmc-mcp")`, which is
unaffected by where the build backend got the version.

Releases are cut by editing `[project] version` and running `uv lock` so the `hmc-mcp`
entry in `uv.lock` agrees — `uv sync --locked` in `just setup` fails otherwise — then tagging
to match. `tests/test_package_version.py` reads the declared version from `pyproject.toml`
rather than copying it, so nothing else needs editing.

## Consequences

A build no longer consults Git, so **no fixture commits**, which is what closes issues #237
and #238: an absent commit cannot invoke an inherited hook. Roughly 430 lines of project-owned
code and tests are deleted rather than maintained.

Two of the three surviving fixtures still create a repository, for reasons unrelated to
versioning: `just setup` runs `prek install`, which requires one, and `prek run --all-files`
resolves its file list through `git ls-files`. Both therefore keep a bare `git init` — and
`tests/test_release_artifacts.py` additionally pins `core.hooksPath` to the fixture's own
`.git/hooks`. **That line is load-bearing**: with `core.hooksPath` inherited from global scope,
`prek install` refuses outright with exit 2, so deleting it turns
`test_clean_checkout_runs_canonical_artifact_commands` red on exactly the machines this work was
about. Only `tests/test_package_version.py` is a plain copy with no repository, and it asserts
that absence so the property this decision is named for keeps a test.

Two properties are given up. There is no longer a unique version per commit between releases —
not currently in use, since there are no tags and no publication. And the build no longer
rejects a dirty or shallow tree; that check existed to guarantee version provenance, so it goes
with the version machinery. `test_dirty_checkout_build_fails_with_actionable_provenance` is
deleted with it. If clean-tree enforcement is wanted for its own sake, it can be reinstated as
an independent check in `just build` without reintroducing a version dependency.

The version and the Git tag can now drift, because nothing enforces their agreement. That is
the standard cost of a static version and the reason ADR 0023 existed; it is accepted here
because an untagged, unpublished project has nothing to drift against yet.

Reversibility is the reason this is safe to take now. If the project is distributed later,
adopting `hatch-vcs` or stock `versioningit` is roughly five lines of configuration plus a
tag — and, unlike ADR 0023, with no project-owned plugin to maintain.

## Considered & rejected

- **Keep ADR 0023 and fix the fixture-commit failure (#237).** Roughly 90 lines of test changes
  to isolate fixture repositories from inherited hook configuration, all of which this decision
  deletes. It also leaves the wider class live: `core.hooksPath` and `init.templateDir` were
  each found to deliver the failure independently, and `commit.gpgSign` reproduces it by a
  third route (#238). Fixing the naming of builds is cheaper than defending the fixtures that
  naming requires.
- **Replace the custom plugin with stock `versioningit` or `hatch-vcs`, and tag the repository.**
  Removes the 434 lines of project-owned machinery and keeps tag-derived versions, and it is the
  right destination if this is ever distributed. Rejected for now because the build still needs
  a Git repository with a commit, so three of the four fixture sites keep committing and #237
  survives in reduced form — paying most of the cost for a property with no consumer.
- **Static version plus an independent clean-tree check in `just build`.** Retains the
  provenance guarantee without the version coupling. Rejected as a separate concern: it is worth
  doing on its own merits or not at all, and bundling it here would obscure what this decision
  is actually for. Recorded in Consequences as available.
