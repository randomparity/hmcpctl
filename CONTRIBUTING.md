# Contributing

Contributions should be focused, tested, and easy to review.

1. Fork the repository or create a feature branch.
2. Install the locked development environment and hooks with `just setup`.
3. Make one focused change and add or update tests for its behavior and error paths.
4. Run `just verify` and `UV_NO_SYNC=1 uv run --no-sync prek run --all-files`.
5. Open a pull request that explains the current behavior of the change.

CI runs static gates through the real hooks once, then `just verify-runtime`.
Standalone `just verify` remains the complete local check; `verify-runtime` alone
omits static checks. Validate hooks separately with the command above.

`just test` uses at most two workers on eligible Linux hosts with at least two
effective CPUs and 3 GiB remaining memory; otherwise it runs serially. The runner
checks affinity, visible ancestor cgroup quotas and available memory, including
current cgroup charges. Parallel success reports `workers=2`. Use
`uv run --no-sync python scripts/run_tests.py --serial` to force serial execution;
add `--timings` for diagnostics. Direct pytest and `just test-verbose` remain serial.
The coverage gate is identical in both modes.

Suspected vulnerabilities do not belong in a public issue or pull request. Follow the
[security policy](SECURITY.md) to report them privately.

Keep dependencies pinned and avoid adding one unless the change requires it. Follow the repository
instructions in `AGENTS.md`, including its commit and verification conventions.

## Changelog

Every user-facing change that ships in a release must be recorded under `[Unreleased]` in
`CHANGELOG.md`, which follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. When a release is cut, its entry keeps those changes and gains scope, **Verified** and
**Known issues** sections ([releasing](docs/development.md#releasing)).

- The newest `vX.Y.Z` release tag must have a matching `## [X.Y.Z]` entry, so a release cannot
  ship without one. `tests/unit/test_changelog.py` enforces this, and the release procedure
  runs it on the tag before pushing it.
- By convention, not by a test, changes to the six-name `hmcpctl.api` facade (ADR 0118) are
  recorded under ordinary changelog categories; domain-module APIs remain pre-release.

## Coupled dependency bumps

Dependabot updates one requirement at a time, so it cannot move pins that must change together.
`.github/dependabot.yml` therefore ignores `fastmcp-slim`, `mcp` and `mcp-types`, and they are
bumped by hand:

- `fastmcp-slim` is pinned twice, as `fastmcp-slim[server]` in the `app` extra and as
  `fastmcp-slim[client]` in the `dev` group; both pins carry the same version.
- `mcp` and `mcp-types` are pinned in the `app` extra, and each `mcp` release requires the
  `mcp-types` release of the same version.

Dependabot alerts and security updates are not enabled for this repository, so nothing reports
an advisory against these packages automatically. Bump them before cutting a release
([releasing](docs/development.md#releasing)), when a change needs a newer release, and when the
GitHub Advisory Database lists an advisory affecting a pinned version. Check it at each release
with `gh api "advisories?ecosystem=pip&affects=<package>@<pinned version>"` for each of the
three. To bump:

1. Choose the newest release of each package that is at least 14 days old on PyPI, and check
   that the chosen `fastmcp-slim` accepts the chosen `mcp` and `mcp-types`. A newer release
   that is still too young is picked up by the next bump after it turns 14 days old; nothing
   tracks it in between.
2. Edit all four pins in `pyproject.toml`; keep them exact.
3. Run `uv lock` with the uv version CI pins (`version:` under `setup-uv` in
   `.github/workflows/ci.yml`), for example `uvx uv@<version> lock`, so the lock format does not
   churn; then run `just setup`. Never run a bare `uv sync`.
4. Run `just tool-docs` in case the rendered tool reference changed, then `just verify` and
   `UV_NO_SYNC=1 uv run --no-sync prek run --all-files`, and read every leg of the CI matrix.
