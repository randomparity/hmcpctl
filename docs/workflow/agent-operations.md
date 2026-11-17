# Agent operations reference

Detailed procedures supporting the concise [root instructions](../../AGENTS.md).
Read the relevant section before dependency changes, merge classification,
environment-isolation diagnosis, or verification timing/CI work.

## Non-interactive shell discipline

All shell commands must be non-interactive. A command that opens an editor, a
pager, or a prompt will stall the agent with no recovery path.

**git**

- Always set `GIT_EDITOR=true` when a git command may open an editor:
  ```sh
  GIT_EDITOR=true git rebase --continue
  GIT_EDITOR=true git commit --amend
  GIT_EDITOR=true git merge --continue
  ```
- Prefer `--no-edit` on `commit --amend` and `merge` when the message is unchanged.
- Never run `git rebase --continue` without `GIT_EDITOR=true` — the continue
  step opens the editor to confirm the commit message even when there are no
  remaining conflicts.
- Suppress the pager: `git --no-pager <subcommand>` or set `GIT_PAGER=cat`.
- **Never squash-merge** (`gh pr merge --squash` / `git merge --squash`) unless
  the PR is documentation-only. Squash merges collapse commit history and break
  `git bisect`. For all code, test, config, and script changes, use either
  `gh pr merge --merge` (merge commit) or `gh pr merge --rebase` (rebase merge).
  - **Documentation-only** means **no changed file's content is asserted by a
    test or a `static` gate.** *Content*, specifically: repo-wide scans like
    `just secrets`, which reads every tracked file, and `just doc-links`, which
    reads every tracked Markdown body, would make the exception dead if they
    counted.
  - **Decide it by reading, not by a pattern or a grep.** Both have been tried
    here and both are unsound, because the guards reach their files three
    different ways: by literal path, by basename
    (`tests/test_project_metadata.py` opens
    `ROOT / "docs" / "hmc-cli-cheatsheet.md"`), and by directory —
    `scripts/gen_tool_reference.py` writes `_REPO_ROOT / "docs" / "tools"` and
    `scripts/check_adr_numbering.py` globs `docs/adr/*.md`, so no search for a
    path or a filename can see those at all. Work from this floor instead:
    - **Content-asserted:** `docs/tools/` (diffed by `just tool-docs-check`),
      every page carrying a generation banner (regenerated and diffed by
      `just doc-freshness`), `docs/environment-variables.md` (checked field by
      field by `just env-vars`), `docs/hmc-cli-cheatsheet.md` and
      `docs/authorization-audit.md` (opened by `tests/`), ADR 0029's inventory
      block (parsed by `tests/unit/test_public_api.py`), and `CHANGELOG.md`,
      `CONTRIBUTING.md`, `README.md`, `SECURITY.md`, and root `AGENTS.md`
      (live-evidence wording in `tests/test_live_testing_doc.py`).
    - **Name-asserted only:** `just adr-numbering` checks an ADR's *filename*
      and H1, never its body — so editing an ADR's prose is not caught by that
      gate. Most ADRs are in this class, which is why a prose-only ADR edit is
      one of the few real documentation-only changes here.
    - **Content-unasserted** today: the `docs/scorecard-*` and `docs/workflow/`
      pages. Test modules cite some of those specs in module docstrings, which
      asserts nothing about the file. `just doc-freshness` does read every
      tracked Markdown file's *first line*, looking for a generation banner —
      so a page here can still redden it by opening with something
      banner-shaped, and `just doc-links` reads its body only to resolve links
      and anchors — a repo-wide scan, as above.
  - **When the answer is not obvious, use `--merge`.** It costs one merge
    commit. A wrong `--squash` is not reversible once it is on `main`.
  - Two shell mechanics, for whatever check you do write over
    `gh pr diff <PR> --name-only`: `grep -Ev` **exits 1 when it filters every
    line out**, so a naive `… | grep -Ev '…'` reports failure on exactly the
    empty-result case and is fatal under `set -e` or inside an `&&` chain; and a
    failed `gh pr diff` — wrong number, expired auth — prints nothing, which
    looks identical to a clean pass. Check its exit status before reading its
    output, and treat an unanswered query as "not documentation-only".
  - The policy is prose, not a gate: **nothing in the repo enforces it** —
    `rg -ni squash` over `.github/`, `tests/`, `scripts/`, `justfile`, and
    `.pre-commit-config.yaml` returns nothing. The maintainer has intentionally
    retained squash-merge availability and selected this documented human
    pre-merge check as the sufficient control; your own reading before the merge
    is the only control.
  - **A single-parent commit on `main` is not evidence of a squash.** This repo
    has also landed PRs with `--rebase`, which replays each commit onto `main`
    and preserves the per-commit history the policy protects — seven commits
    from PR #455 landed that way as `aec6125^..f528e94`. To tell a squash from a
    rebase, compare the commit's own diff with the PR's: on a squash they are
    equal, on a rebase the commit carries only its own slice. This paragraph is
    forensic — how to read history after the fact. It does not prefer one
    permitted non-squash strategy over the other: both `--merge` and `--rebase`
    preserve the per-commit history this policy protects.

**Other common interactive traps**

| Command | Safe form |
|---------|-----------|
| `git log` | `git --no-pager log --oneline` |
| `git diff` | `git --no-pager diff` |
| `gh pr create` | always supply `--title` and `--body` (or `--body-file`) |
| `gh issue create` | always supply `--title` and `--body` |
| `uv add` / `pip install` | non-interactive, but never bare — see *Worktree venv hygiene* |

## Merge-conflict resolution

**Resolve conflicts with precise, targeted edits — not regex over the whole file.**

A regex that matches `<<<<<<< HEAD … >>>>>>> sha (message)` across the entire
file is brittle: it fails silently when the conflict block spans a Python
f-string boundary (the closing `"""` is inside the conflict region and gets
consumed), leaving a syntax error that only surfaces at import time.

Preferred approach:

1. After a failed rebase, read the exact conflict block with `grep -n` and
   `read_file` (specifying the conflict line range).
2. Write a targeted Python script that replaces the exact literal string
   (conflict markers included) with the resolved text — or use `apply_diff` /
   `search_and_replace` with the conflict markers escaped.
3. Verify syntax before staging:
   `uv run --no-sync python -c "import hmcpctl.server"` (or the relevant
   module) must succeed **before** `git add`.

Never call `git add <file>` and `git rebase --continue` in the same step unless
you have already confirmed the file is syntax-clean.

## Worktree venv hygiene

**Bootstrap a new worktree with `just setup`, and use nothing else.** It is the
repo's only sync recipe (`justfile`):

```sh
just setup   # uv sync --locked --extra app --link-mode copy; prek install;
             # then link the local reference corpus into the worktree
```

**Never run a bare `uv sync`.** `pyproject.toml` declares no `[tool.uv]` table
and no `default-extras`, so a bare `uv sync` installs `[project] dependencies`
plus the default `dev` group and then **prunes the `app` extra** — `typer` and
fastmcp's server-extra transitive dependencies (`cyclopts`, `openapi-pydantic`,
`websockets`, `watchfiles`, `shellingham`, and more). `uv sync --dry-run` prints
the exact list for the current lock and writes nothing, so check there rather
than trusting a list in this file. Losing `typer` alone breaks `just typecheck`,
which covers every `src/hmcpctl/cli_*.py` module, in a way whose cause is
nowhere near the error. A bare `uv sync` also drops `--locked` and can silently
rewrite `uv.lock`.

**`uv add` syncs by default and prunes the same way.** Always pass `--no-sync`,
and then `just setup` — the exact form depends on where the dependency goes,
below. `pip install` into this venv is worse: the next `uv sync --locked`
reverts it silently. When `uv sync --locked` refuses because `uv.lock` has
fallen behind `pyproject.toml`, refresh the lock with `uv lock` — that is the
one sync-adjacent command the rule above does not cover, because it resolves
without touching the environment.

**Where a new dependency goes decides how it must be written**, and
`uv add`'s default `>=` floor is wrong for both cases. `tests/test_supply_chain.py`
enforces this in `just test`, before any CI job runs:

- A **runtime** dependency goes in `[project] dependencies` and needs both a
  floor and a cap: `uv add --no-sync "<pkg>>=x,<y"`, writing the range out
  rather than letting `uv add` pick a bare floor. It must also join
  `LIBRARY_DEPENDENCIES` in
  `tests/test_supply_chain.py` and ADR 0068's policy notes — the exhaustiveness
  test compares the two sets and says so in its own failure message. CI's
  `library-range-floors` job re-checks the range shape, long after and far from
  the `uv add` that caused it.
- A **development tool** goes in the `dev` group and must be **exactly pinned**:
  `uv add --dev --no-sync "<pkg>==<version>"`. The `app` extra and
  `[build-system] requires` are pinned the same way, and a pin that does not
  equal the locked version fails too.

**Never run a bare `uv run` either.** Every `uv run` in the `justfile` passes
`--no-sync`, and `tests/test_ci_pipeline.py` asserts that as an invariant. A
bare `uv run` may re-sync and undo the extras state `just setup` established —
the same breakage as above, arrived at sideways.

Package code in an older worktree is **not** stale: the project is installed
editable (`.venv/lib/python3.11/site-packages/_editable_impl_hmcpctl.pth`), so
`src/` is what imports. Only dependency and extra state drifts, and only
`just setup` restores it.

**The package version comes from Git tags** (ADR 0250), so the editable
install's `__version__` is whatever its first build computed; `uv sync --locked
--extra app --link-mode copy --reinstall-package hmcpctl` refreshes it. A build of
a copied tree with no `.git` fails unless
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL` is set.

**Reading a `SyntaxError`.** Under an editable install, a `hmcpctl` syntax
error surfaces at a `src/` path — that one is yours to fix in source. A
`SyntaxError` at a `.venv/lib/…/site-packages/` path is third-party code, so it
means the extras or lock state is wrong, or the interpreter does not match what
the environment was built for; `just setup` is the fix for that one.

**A local-only reference corpus may exist beside this repo.** A companion file
this repository does not track can name a vendored API reference corpus kept
on one operator host only; `just setup` symlinks it into a linked worktree
when the main checkout has it, and otherwise prints that it is unavailable —
so its absence is discoverable rather than silent. When it is present, cite
it as `docs/refs/<path>:<line>` and report what a search over it returns;
never reproduce its prose into a tracked file.

## Pre-existing test failures

When running the guardrail suite and discovering a failing test that predates the
current branch, **fix it in the same PR**. Do not leave it failing and do not
defer it to a follow-up. A failing test is either wrong (the test must be
corrected to match the current contract) or right (a latent bug that must be
fixed). Determine which, apply the minimum targeted fix, and include it in the
commit history for this branch. Documenting a known-broken test as "pre-existing"
and shipping over it is not acceptable.

Common causes worth checking first:

- **Ambient `HMC_*` variables supplying credentials** — `HMCConfig` is a
  pydantic-settings `BaseSettings` with `env_prefix="HMC_"`: its normal
  constructor reads environment values for fields the caller leaves unset.
  The suite protects test bodies from workstation exports with autouse fixtures
  in `tests/conftest.py`. `enable_tls_verification_for_tests` derives the field
  names from `HMCConfig.model_fields`, clears their environment variables and
  `HMC_PROFILE` in every casing, then sets `HMC_VERIFY_SSL=true`.
  `default_power_ownership_guard_off` also clears power authorization;
  `no_ambient_hmc_settings` additionally clears `HMC_AGENT_ID`,
  `HMC_SCHEMA_VERSION`, `HMC_ISO_URL_ALLOWLIST`, `HMC_ISO_BUILD_ENTRY`, and
  `HMC_ISO_BUILD_ARGUMENTS`. Tests can set their own environment after setup.

  Tests that need a credential-free `HMCConfig` should construct it with
  `HMCConfig.from_mapping({...})`, which reads no environment variable and no
  dotenv file and gives every omitted field its declared default (ADR 0096):

  ```python
  HMCConfig.from_mapping({})                      # every field at its default
  HMCConfig.from_mapping({"host": "h", "user": "u"})
  ```

  **`_env_file=None` is not this.** It is a private pydantic-settings parameter
  that suppresses a dotenv source and never touches `os.environ`; and
  `HMCConfig` declares no `env_file` at all, so it currently does nothing
  whatsoever. Existing call sites that pass it are inert. Do not add new ones,
  and do not delete a `monkeypatch.delenv` on the strength of one — the
  `delenv` is what is actually isolating that test. Where a test needs to
  exercise the environment-reading constructor on purpose (`load_profile`'s
  env-over-TOML precedence, the CLI path), keep using
  `monkeypatch.setenv`/`delenv`; `from_mapping` is the wrong tool there,
  because the environment is the behaviour under test.

  Use `HMCConfig.model_fields` as the current field inventory; `just env-vars`
  checks that the environment reference documents it. No fixed count is needed.
  The bounded probe below starts the config tests in a populated process. It
  sets non-default values for the fields other than `HMC_VERIFY_SSL` and
  `HMC_AUTHORIZE_POWER_OPERATIONS`, whose test baseline is true and unset,
  respectively. The other exports are cleared by the fixtures too: this does
  **not** inject workstation values into each test body. When adding a field,
  extend the probe with a valid non-default value.

  ```sh
  HMC_AGENT_ID=a HMC_AUDIT_MEMENTO=m HMC_CA_BUNDLE=/ca.pem \
  HMC_CERT_FINGERPRINT=AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA \
  HMC_HOST=h HMC_ISO_BUILD_ARGUMENTS='["{target}"]' HMC_ISO_BUILD_ENTRY=/build \
  HMC_ISO_URL_ALLOWLIST=iso.example.test HMC_MAX_RESPONSE_BYTES=1048576 \
  HMC_PASSWORD=p HMC_PORT=12443 HMC_SCHEMA_VERSION=V1_0 HMC_SSH_KEY_FILE=/k \
  HMC_SSH_TIMEOUT=111 HMC_SSH_VERIFY_HOST_KEY=false HMC_TIMEOUT=17 \
  HMC_TRUST_SYSTEM_STORE=false HMC_UPLOAD_TIMEOUT=222 HMC_USER=u \
    uv run --no-sync pytest tests/unit/test_config.py --no-cov -q
  ```

  Each listed value differs from its field default. Keep the ISO build pair
  together: supplying only one setting fails config construction, and the
  arguments must be a JSON string array containing exactly one `{target}`.

  A green run is expected: it shows this bounded config-test group survives
  these ambient exports. The config tests also set their own values after
  fixture setup: `test_from_mapping_leaves_no_field_to_the_environment` checks
  mapping isolation over `model_fields`, while
  `test_plain_constructor_still_reads_the_environment` preserves intentional
  environment loading. A green probe does not certify unselected tests or
  code outside fixture setup, including import/collection-time reads. It is
  separate from the normal `just verify` shipping gate.

  If the probe fails, report the failing cases and actual cause; a construction
  error is not a wrong-value assertion, and neither automatically proves a
  per-test leak. Preserve the fixtures rather than removing them to force red.

  Outside fixture protection, `HMC_PROFILE` selects a TOML profile independently
  of `HMCConfig` fields, and `HMC_HOST` also reroutes profile and authorization
  decisions. When testing those paths, control the variables explicitly with
  `monkeypatch.setenv`/`delenv`. Do not change an access-policy assertion to
  match ambient credentials; unset `HMC_HOST` and diagnose that decision first.
- **Import name drift** — a tool or function renamed in source but still
  referenced by the old name in a test or fixture.

## Guardrail commands

Use the quiet recipes by default so successful checks do not consume agent
context. `just test` buffers pytest's output and replays the whole capture when
the run exits non-zero (`scripts/run_tests.py`), so a failure loses nothing.
`just smoke` does no buffering at all — it is already terse, and there is
nothing held back to replay.

```sh
just test          # tests + exact coverage gate; compact success summary
just smoke         # MCP handshake; tool count only
just verify        # full pre-push guardrail
```

Use verbose recipes only when live progress or expanded diagnostics are needed:

```sh
just test-verbose   # live pytest output + missing-lines coverage
just smoke-verbose  # list every exposed MCP tool
just test-timings   # retain pytest output, including the 30 slowest test phases
just verify-timings # canonical verify with per-recipe wall time and pytest timings
```

`just test` and its verification callers use at most two pytest workers on Linux
when at least two effective CPUs and 3 GiB remaining memory are visible. Affinity,
visible cgroup ancestor quotas, host available memory and ancestor memory charges
bound eligibility; unavailable resource or child-ownership facilities select serial.
The success summary includes `workers=2` when parallel execution ran. Force serial
with `uv run --no-sync python scripts/run_tests.py --serial`; `--timings` also works.
Direct pytest and `just test-verbose` remain serial. Both modes keep the exact
coverage gate. See [the evaluation](bounded-pytest-evaluation.md).

Timing diagnostics preserve the configured coverage gate and subprocess lifecycle.
`HMCPCTL_TEST_TIMINGS=1` enables the runner's timing presentation through the
verification graph; the runner removes this switch before launching pytest so
nested invocations keep their own defaults. Only the exact value `1` enables it.
`just --time` measures each recipe body, excluding dependency time; its `verify`
line is not the total verification elapsed time. Measure overall elapsed time
separately when comparing runs, and record commit, environment, resources and caches.

Before pushing, run `just verify` inside the branch worktree. If pytest fails
during collection, run `just smoke`; it imports `hmcpctl.server` directly and
can expose an import-time syntax error that collection obscures.

`just verify` is `static test smoke build verify-artifacts` plus a CLI-group
load check. Running a `static` sub-recipe by name is how you narrow a `static`
failure to its cause instead of re-reading the umbrella output:

```sh
just lint              # ruff check .
just format-check      # ruff format --check . over Python files; Markdown is excluded
just typecheck         # ty check
just secrets           # detect-secrets-hook against .secrets.baseline
just workflow-security # zizmor over .github/workflows/, no online audits
just env-vars          # every HMC_* field in HMCConfig is documented
just nicknames         # the committed config fixture's nicknames table is well-formed
just tool-docs-check   # docs/tools/ still matches the MCP tool registry
just adr-numbering     # every docs/adr/ record's number is unique and matches its H1
just doc-freshness     # every generated document matches its declared command
just doc-links         # every relative Markdown link and #anchor in a tracked file resolves
```

Two of those have a **regenerator, not a hand fix**. When `tool-docs-check`
fails, run `just tool-docs` and commit the result; editing `docs/tools/*.md` by
hand only makes the next run fail again. `doc-freshness` is the same pattern
generalized: it re-runs the recipe each banner-carrying document names and
diffs the output, so the fix is always to run that recipe. `format-check` is
fixed the same way, by `uv run --no-sync ruff format .`, never by hand.

`build` and `verify-artifacts` are the other two `verify` members: `build`
produces a fresh wheel and sdist into `dist/`, and `verify-artifacts` validates
what is already there without rebuilding.

## What a green `just verify` does not cover

**A green local run does not predict a green CI run.** Locally `just verify`
uses the worktree's one `.venv`, built on the `3.11` that `.python-version`
pins. CI's `ci` job is **five legs** — amd64 `ubuntu-24.04` ×
{3.11, 3.12, 3.13, 3.14, 3.15} — and `library-wheel-smoke`,
`library-range-floors`, and a `wheel-smoke` matrix of the same five legs all
depend on it. This has bitten a change that was locally green
and red on 3.12+, because `inspect` renders `Annotated` differently across
versions. When a change touches signature introspection, generated
documentation, or anything whose output is a rendered type, expect the version
legs to disagree with your machine and read the CI matrix rather than re-running
locally. Hosted CI no longer supplies native arm64 evidence (ADR 0210).

**CI distributes tests and combines coverage per Python version (ADR 0309).**
Each version runs `UV_NO_SYNC=1 uv run --no-sync prek run --all-files` once
and, starting at the same time (ADR 0383), four separate test groups:
`domain-and-unit`, `application`, `procedure-arms` and `tooling`.
`just test-group <group>` retains timings, JUnit and branch data; its success
means combined coverage is still pending.
The existing per-version `ci` verification checks require successful producers
and exactly four matching artifacts, then run `just verify-collected <python>`
to enforce the configured coverage floor before smoke/build checks and wheel
retention. Explicit dependency guards fail on failed, skipped or cancelled
producers. Native coverage reads use isolated copies so unrelated shards cannot
inflate or be deleted by reporting. Hooks remain separately identifiable;
permissions, pins and downstream wheel/library checks stay intact.
`just verify` remains complete standalone local verification and does not execute
prek. `verify-runtime` still owns a full local test run; both it and
`verify-collected` share `verify-runtime-checks` for the remaining runtime gates.

Before pushing, run both `just verify` and `uv run --no-sync prek run --all-files`.
Use the latter instead of bare `prek`: the dev group pins its version, while a
global executable may differ. `just setup` installs the git hook script, not a
`prek` on `PATH`.
