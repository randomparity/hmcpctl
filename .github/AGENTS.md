# CI instructions

Read this also for `justfile` and `.pre-commit-config.yaml` changes.

- Every `static` dependency needs one prek hook: identical `id`,
  `entry: just <recipe>`, `language: system`, and `pass_filenames: false`.
  Do not narrow hooks with `stages`, `exclude`, `files`, `types`, `types_or`,
  or `always_run`. `tests/test_ci_pipeline.py` checks this contract.
- Use `uv run --no-sync`; pinned prek runs as
  `uv run --no-sync prek run --all-files`. Setup installs the hook, not a
  global prek binary. Hooks and CI invoke canonical recipes.
- Read the [CI execution model](../docs/workflow/agent-operations.md#what-a-green-just-verify-does-not-cover)
  before changing the graph. Preserve per-version producers, exactly four
  matching coverage artifacts, failure/skip/cancellation guards, isolated native
  coverage reads, combined coverage floors, and downstream wheel/library checks.
- `just verify` remains a standalone local check. A successful shard still
  awaits combined coverage; a local pass does not establish every Python leg.
  Inspect the actual matrix for signature introspection and rendered-type changes.
- Hosted CI targets amd64; it does not provide native arm64 evidence (ADR 0210).
  Nothing in CI or `just` recipes may access an HMC.
