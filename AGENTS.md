# Repository instructions

## Scope and references

Read applicable nested instructions before editing a subtree:

- [tests/AGENTS.md](tests/AGENTS.md): test isolation and fixture conventions.
- [scripts/AGENTS.md](scripts/AGENTS.md): script/test pairing and live runners.
- [docs/AGENTS.md](docs/AGENTS.md): generated documents, reference material,
  and decision-record numbering.
- [.github/AGENTS.md](.github/AGENTS.md): CI and coverage topology. Also read it
  before changing `justfile` or `.pre-commit-config.yaml`.

Read the relevant section of [agent operations](docs/workflow/agent-operations.md)
before dependency changes, merge classification, environment-isolation diagnosis,
or verification timing/CI work. Keep instructions concise: mandatory rules here,
subtree-specific rules in nested files, examples and troubleshooting in references.

## Shell, Git, and merges

- Commands must be non-interactive. Use `git --no-pager`, `GIT_EDITOR=true` for
  commands that may open editors (especially `rebase --continue`), and `--no-edit`
  when retaining a message. Supply titles and bodies to `gh` creation commands.
- Resolve conflicts with exact, targeted edits, never whole-file regex replacement.
  Verify the affected module imports with `uv run --no-sync python` before staging;
  do not combine staging and rebase continuation until syntax is verified.
- Never squash code, tests, config, or scripts. Use merge or rebase merging.
  Squash is permitted only when no changed file's content is asserted by tests or
  static gates; repo-wide link/secret scans do not count. Read the content-asserted
  inventory in the operations reference before choosing squash. If uncertain,
  use `--merge`. Preserve this manual control; merge availability is intentional.
- Compare branches with `git --no-pager diff "$(git merge-base HEAD origin/main)"`,
  not local `main`, which can advance independently.
- Set `git config blame.ignoreRevsFile .git-blame-ignore-revs` once per clone.
  Mechanical reformats need their own commit and full SHA in that file; merge
  those PRs with `--merge` so the recorded SHA survives.

## Environment and dependencies

- Bootstrap worktrees only with `just setup` (locked app extras, copy link mode,
  pinned prek hook, reference-corpus link). Never bare `uv sync`, bare `uv run`,
  or `pip install` into this venv. Use `uv run --no-sync` throughout.
- Add dependencies with `uv add --no-sync`, then `just setup`. Runtime ranges
  need a floor and cap and entries in `LIBRARY_DEPENDENCIES` in
  `tests/test_supply_chain.py` and ADR 0068. Dev tools, app extras, and build
  requirements need exact pins matching the lock. Use `uv lock` when the lock
  needs refreshing; it does not synchronize the environment.
- Source is editable; dependency drift calls for `just setup`. Git tags determine
  package versions (ADR 0250). To refresh installed version metadata use
  `uv sync --locked --extra app --link-mode copy --reinstall-package hmcpctl`.
  A copied tree without Git needs `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL`.
- `just setup` links an optional local-only API reference corpus when available
  and reports absence. Cite it as `docs/refs/<path>:<line>` and report searches;
  never copy its prose into tracked files.

## Verification

- Prefer `just test` (tests and exact coverage), `just smoke` (MCP handshake),
  and `just verify` (static, tests, smoke, build, artifact and CLI checks).
  Quiet test output is replayed on failure. Use verbose/timing recipes only
  when needed; see the operations reference for timing and worker semantics.
- Before pushing, run **both** `just verify` and
  `uv run --no-sync prek run --all-files` inside the branch worktree. Local
  verification does not run prek or establish every CI Python-version result.
- Narrow static failures by running their named `just` recipe. On collection
  failures, run `just smoke` to expose import errors.
- Regenerate, never hand-fix generated output: `just tool-docs` for `docs/tools/`,
  the banner's recipe for other generated documents, and
  `uv run --no-sync ruff format .` for Python formatting.
- Fix pre-existing guardrail failures in the same PR; determine whether the
  test or implementation is wrong and apply the minimum correction. Do not ship
  over a known failure. Environment-leak diagnostic failures are diagnostic
  output; see tests guidance before changing assertions.
- New `static` recipes require matching prek hooks; read `.github/AGENTS.md`.
  Introspection/rendered-type changes require attention to every CI version leg.

## Contracts and workflow

- `hmcpctl.api` is the six-name stable facade (ADR 0118); preserve exact exports
  in `tests/unit/test_public_api.py`. Domain modules are pre-release APIs;
  add no compatibility re-exports.
- Every non-epic issue needs a release milestone: the matching theme or latest
  open milestone for unscheduled work. Blockers belong to the same or an earlier
  milestone. Cross-milestone epics carry no milestone.
- Plans in `docs/workflow/plans/` are local, gitignored, and transient. Never
  commit them; delete completed plans and preserve decisions in ADRs or specs.
- Each top-level `scripts/*.py` entry point requires
  `tests/scripts/test_<name>.py`. Exceptions: `check_env_vars.py` uses
  `tests/test_env_var_guard.py`; `live_test_runner.py` uses
  `tests/test_live_runner.py`. Nested live-test library modules use behavior groups.

## Live testing and evidence

Live testing follows [docs/live-testing.md](docs/live-testing.md), always.
A live run creates, mutates and deletes real partitions. Do not invent an invocation:
run `scripts/live_test_preflight.py` first, dispatch one arm through its named
script, then run `scripts/live_test_recovery.py`, including after apparent success.
Nothing in CI or any `just` recipe reaches an HMC; never add such access.

A live matrix is evidence only for the commit it ran on.
Generate evidence with `scripts/live_test_evidence.py`, which stamps the commit and
filters HMC-derived fields. Never transcribe a matrix from a terminal. Before citing
it in an ADR, PR, or issue, verify it matches the tested commit and identify subsequent
branch changes. Never publish `test-results-*.json`: rows contain hostnames, accounts,
and location codes; only FAIL rows are redacted by the runner.
