# ADR 0250: Derive package versions from Git tags with hatch-vcs

## Status

Accepted

Supersedes [0033](0033-static-package-version.md).

## Context

ADR 0033 declared the version statically because no tag existed and because Git-derived
versions forced test fixtures to commit, which inherited global hooks broke (#237). `v0.1.0` is
now tagged (#382), so every build between releases reports `0.1.0` and cannot be told apart from
the release or from another dev build (#1471).

## Decision

The version comes from Git through the stock `hatch-vcs` version source:

```toml
[project]
dynamic = ["version"]

[build-system]
requires = ["hatch-vcs==0.5.0", "hatchling==1.32.4", "setuptools-scm==10.3.4"]

[tool.hatch.version]
source = "vcs"
raw-options = { dist_name = "hmcpctl" }
```

- Release tags are annotated `vX.Y.Z`. The setuptools-scm defaults (`guess-next-dev`,
  `node-and-date`) give these PEP 440 forms:

  | Build | Version |
  |---|---|
  | clean checkout of `vX.Y.Z` | `X.Y.Z` |
  | N commits after it | `X.Y.(Z+1).devN+g<sha>` |
  | tracked files modified | `X.Y.(Z+1).devN+g<sha>.dYYYYMMDD`, N from 0 |
  | Git history but no reachable tag | `0.1.devN+g<sha>` |

- An sdist carries its version in `PKG-INFO`, and rebuilding from it needs no Git.
- A tree with no Git metadata and no sdist metadata has no version. The build fails with
  setuptools-scm's message naming `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL`; tests that
  build a copied tree set that variable. `dist_name` is what lets the per-distribution
  variable match. There is no `fallback-version`.
- `hatch-vcs` and `setuptools-scm` are pinned exactly in `[build-system] requires` and the dev
  group, under the existing supply-chain rule.
- The CI `ci` job, the only job that builds or installs from source, checks out with
  `fetch-depth: 0`. The other jobs install the retained wheel and keep the shallow default.
- `uv.lock` records the project as dynamic, so a release no longer edits `pyproject.toml` or
  re-locks. A release is the tag; `tests/unit/test_changelog.py` requires the latest
  reachable release tag to have a changelog heading.

## Consequences

No test commits or tags. Tests clone the checkout's own history with global Git configuration
isolated, and build copied trees with the pretend variable; the #237 path stays closed. Those
history tests need the `v0.1.0` tag, which `fetch-depth: 0` provides in CI.

A shallow or tag-less checkout builds a `0.1.devN` version, with at most a setuptools-scm warning.
CI is held to full history by `tests/test_ci_pipeline.py`; elsewhere this is accepted because
nothing is published (ADR 0024).

An editable install records its version when `just setup` last built it; `__version__` there
can lag the checkout. `just build` always recomputes.

The build environment resolves the plugins' own dependencies (`vcs-versioning`,
`setuptools`, `packaging`) fresh, as it already does for hatchling's.

ADR 0127 rejected history-based staleness partly because no CI checkout had history. The `ci`
job now has it; that rejection's other grounds are unchanged.

## Considered & rejected

- **Keep the static version.** verified: `git describe --tags` on `1bc81224` prints
  `v0.1.0-790-g1bc81224`, while the wheel built there is `0.1.0`: 790 commits share the
  release's version.
- **Stock `versioningit`.** judgment: equivalent output with a second plugin family to learn;
  the issue asks for `hatch-vcs` on the existing hatchling backend.
- **A `fallback-version` for Git-less trees.** judgment: a silent placeholder version hides a
  broken checkout; the pretend variable makes the choice explicit.
- **`scm.git.pre_parse = "fail_on_shallow"`.** verified: vcs-versioning 2.6.0 documents
  `fail_on_shallow` as "experimental, may change at any time" (`_backends/_git.py`).
- **The generic `SETUPTOOLS_SCM_PRETEND_VERSION`.** judgment: it would also version any
  other setuptools-scm build in the same environment.
- **Full history in every CI job.** judgment: four of the five jobs never build from source.
