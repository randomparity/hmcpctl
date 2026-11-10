# Live verification and derived staleness design

Status: approved for implementation by issue #623, cycle-2 frozen scope annotation
`q623-d32263bc` (issue comment 5562652705).
Decision: [ADR 0127](../../adr/0127-derived-live-verification-staleness.md), superseding
ADR 0126's evidence model.

## Outcome

Know, per operation, whether a live test validated it; derive when that validation is
stale; warn at pull-request time and fail the weekly run. Make the runner's observations
honest enough to be worth recording: arguments validated against the served schema,
postconditions asserted rather than assumed, expected failures declared rather than
substring-matched.

## Boundaries

Extends `scripts/live_test_runner.py`, `scripts/live_test/`, and
`scripts/check_capability_inventory.py`. Changes no file under `src/hmc_mcp/`. Runs no live
test. Adds no real observation to `docs/capabilities/maturity.json`. Adds no CI permission.
The reference inventory (#621), discovery surfacing (#624) and scenario generation (#706)
are other issues' work.

## Current defects

Measured on `main` at `ded24a77`:

- 23 of 173 `state.call` dispatches send an argument no tool accepts or omit a required
  one, including the whole provisioning path calling a signature that no longer exists.
  Three route through `record_expected_or_real`, where a substring match over the exception
  text *and traceback* can record the harness's own defect as a known HMC limitation.
- `PASS` means the call returned. A job that came back `FAILED_BEFORE_COMPLETION` records
  identically to one that succeeded: the repository defines eleven terminal statuses
  (`src/hmc_mcp/jobs/core.py:13-27`, ADR 0081) and treats only `COMPLETED` and
  `COMPLETED_OK` as successful (`:28`). Assert membership in `SUCCESSFUL_JOB_STATUSES`, never
  equality with `COMPLETED_OK` — the latter would record every `COMPLETED` job as a failure.
- `REST000E` and `REST000B`, the only codes the runner matches, are absent from the IBM
  REST reference; they are field-observed. No documented grammar covers them.
- ADR 0126's fingerprint is one digest over 209 files; a stale one is a hard error. Detailed
  in ADR 0127's Context.

## Dispatch argument validity

Every `state.call(client, "<literal>", **kwargs)` must name a served tool, pass only keyword
names in its input-schema `properties`, and pass every name in `required`.

Enforced statically by extending the #485 name guard in `tests/test_live_runner.py` to
keywords, over `LIVE_WORKFLOW_MODULES` and the runner; a non-literal tool name or a `**`
splat fails the guard. Enforced at runtime in `RunState.call` before dispatch: a violation
records `failed` with reason `invalid-arguments` and never reaches the client. FastMCP would
reject the call anyway; the runtime check exists so the failure carries a stable reason
rather than a pydantic rendering. `scripts/live_test/arms/provisioning.py:163` is rewritten to
name its arguments.

## Failure classification and expected outcomes

`RunState.call` keeps its `(status, data)` return. On failure `data` is a `CallFailure`
carrying the exception class name, the message, the traceback text, an `http_status` parsed
as `HTTP \d{3}` from the message, and `denied`, true when the message matches
` is not permitted (?:on .+ )?by access policy ` — the concrete exception type does not
survive FastMCP's `ToolError`, and a test provokes a real denial through the composed
application to hold that coupling.

The `on <targets>` segment is optional because the denial templates are not uniform. Connection
scope always renders it (`src/hmc_mcp/authorization/connection_scope.py:47`) and so does one
target-scope template (`target_scope.py:86`), but three others render
`"{tool} is not permitted by access policy {policy}: …"` with no segment at all
(`target_scope.py:71`, `:76`, `:80`). A pattern requiring `on .+` classifies those three as
`denied=False`. That is closed rather than unsafe — an unmatched failure records `failed` and
never `passed` — but an `ExpectedOutcome(denial=True)` would silently never match them, and
the runner composes its policy with `include_arbitrary_command=True`, so target-scope denials
are reachable.

`ExpectedOutcome(reason, error_codes=frozenset(), denial=False)` replaces
`expected_fail_substrings`. It matches a `CallFailure` when any declared code occurs as a
whole token (`\b<code>\b`) in the **message**, or when `denial` and the failure is a denial.
It rejects construction with neither. There is no code-family regex: matching declared
literals is what a family regex was approximating, and a family regex is how the four-digit
`REST\d{4}` bug happened. `record_with_expected` records `skipped` on a match and `failed`
otherwise; an `invalid-arguments` failure never matches. The 17 sites migrate; `skip()` is
unchanged.

## Result vocabulary and postconditions

| `result` | Meaning | Can be an observation |
|---|---|---|
| `observed` | the tool returned; nothing asserted | never |
| `passed` | every declared assertion held; cleanup `passed` or `not-required` | yes |
| `failed` | error, unmatched denial, invalid arguments, or a false assertion | yes |
| `skipped` | a declared `ExpectedOutcome` matched | never |

`record` keeps its signature and never yields `passed`, so the 182 existing `record` sites
(plus 53 `skip` and 17 `record_expected_or_real`, all of which route through it) cannot
promote by omission. `record_verified(subtask, tool, *, operation, scenario, assertions, cleanup,
data)` is the only path to `passed`. `assertions` is a non-empty tuple of
`Assertion(id, holds)` where `id` matches `[a-z][a-z0-9-]{1,62}[a-z0-9]` —
the closing class rejects a trailing hyphen, which a `{2,63}` tail admitted; `operation` must exist in
`operations.json` (guard test); `scenario` matches `st\d+-[a-z0-9-]+`.

Converted now, so the path is exercised. Both job scenarios in
`scripts/live_test/arms/metrics.py` assert the same three ids over a
`hmc_mcp.jobs.JobOutcome`: `job-found` (`found`), `job-identity-matches` (`job_id` equals
the identifier passed), and `job-status-successful` (`status` in
`hmc_mcp.jobs.SUCCESSFUL_JOB_STATUSES`). `hmc_wait_for_job` is annotated `-> JobOutcome`, so
FastMCP serves an unwrapped seven-property `outputSchema` and `result.data` arrives as a
generated pydantic model, **not** a `dict` — verified against `fastmcp 3.4.7`, which returns
a `Root` instance for that return type. The scenario therefore normalizes by field name
rather than by `isinstance(data, dict)`, which would silently fail every real run.
`hmc_get_job` returns the raw HMC entry (or `null`) as a plain dict, which the scenario
normalizes with `hmc_mcp.jobs.job_outcome(job_id, data)` — the module's own reader, which
finds `Status` under the nested `Resource` and treats `None` as `found=False`. No scenario
reads a status key by hand. `hmc_get_console_info` in `scripts/live_test/arms/connectivity.py`
asserts `console-uuid-present`: `data.get("uuid") or data.get("UUID")` is a non-empty
string — the expression the runner already uses to capture `console_uuid` at
`connectivity.py:24`.

## Observation record

Format 4 ([ADR 0186](../../adr/0186-observation-schema-version-stamp.md)) adds a required
`schema_version` to this exact key set; see
[the schema-version design](2026-10-01-observation-schema-version-design.md).

An attempted live observation in `maturity.json` format 2:

```json
{
  "id": "st12-hmc-get-job",
  "channel": "live",
  "result": "passed",
  "scenario": "st12-job-inspection",
  "tested_commit": "<40 hex>",
  "observed_at": "2026-09-06T00:03:02Z",
  "hmc_release": "V10R3",
  "hardware_family": "POWER10",
  "cleanup": "not-required",
  "closure_fingerprint": "<64 hex>",
  "assertions": ["job-found", "job-identity-matches", "job-status-successful"]
}
```

Exact key set. `id` unique catalog-wide and matching `[a-z0-9][a-z0-9-]*`; it is derived, not
chosen — `f"st{subtask}-{tool}"` with `_` replaced by `-`, so subtask 12's `hmc_get_job` gives
`st12-hmc-get-job` as above. `channel` is always `"live"` on this path and is written by
`record_verified`, not by the emission step. `result` in `{passed, failed}`. `observed_at`
matches the validator's existing `TIMESTAMP` (`Z` form).

`assertions` lists the ids whose `holds` was true, in declaration order — the held subset,
not the declared set. A `passed` observation therefore lists every declared id (that is what
makes it `passed`); a `failed` observation lists the ones that still held and may list none.
The distinction is not cosmetic: writing the declared set instead would make a `failed`
record indistinguishable from a `passed` one on this field, and would let a reader conclude
an assertion held when it did not.
`tested_commit` matches `SHA_1`, `closure_fingerprint` matches `SHA_256`, every `assertions`
element matches `ASSERTION_ID` and the list is unique in declaration order, and `cleanup` is
in `CLEANUP` for a `failed` observation as well as a `passed` one. The validator applies all
of these, not just the runner: the file it guards is hand-copied and hand-editable, so a
check the runner performs on the way out is not a check on the way in. Both SHA patterns
already exist in the module for format 1's revision and fingerprint fields.

`hmc_release` and `hardware_family` are the only free text, and each has a real grammar
rather than a permissive character class: `hmc_release` matches `\AV\d+R\d+(?:M\d+)?\Z` and
`hardware_family` matches `\APOWER\d+\Z`. The `.env.example` values `V10R3` and `POWER10`
conform. A looser `[A-Za-z0-9 ._-]{0,39}` class with an IPv4 rejection was the first design
and is not enough: it admits `hmc01.lab.example.com`, `0644C7T`, `U78CB.001.WZS0044-P1-C2`
and `lab-hmc-3` verbatim — every identifier class the repository's privacy rule names —
while rejecting only a dotted quad, which is the one value nobody types into a field called
"HMC release". The narrow grammars admit the values these fields are for and nothing else. A `passed` observation has non-empty `assertions` and
`cleanup` in `{passed, not-required}`.

**Format 2 has one observation shape.** ADR 0126's `not-run` row is dropped, not carried
forward: `_validate_not_run` and its keys (`reason`, `prerequisites`, `obligation`) are
deleted whole. Those three were human prose, so retaining them would leave a committed record
with free text beyond the two environment strings — and would put that free text precisely
where a maintainer describes unavailable hardware, which is where hostnames, serials and
location codes get written. The catalog holds no `not-run` row today, so nothing is lost, and
an operation with no observation already derives `unevidenced`, which is what the row said.
Reinstating a placeholder with a closed vocabulary is future work for whatever needs it.

`admission_policy` and the implementation record are unchanged from format 1. Removed from
observations: `currency`, `invalidated_by`, `promotion`, `implementation_fingerprint`,
`implementation_revision`, `deployed_revision`, `environment`, `scope`, `provenance`.

## Closure fingerprint

`closure_fingerprint(repo_root, handler_module)` resolves the handler's module file under
`src/hmc_mcp/`, parses it with `ast`, and follows every import that can reach `src/hmc_mcp/`:

- `ImportFrom` with a `module`: `from .x import y`, `from ..x import y` (resolved by
  `level`) and `from hmc_mcp.x import y`; when `y` names a module file it is included too.
- `ImportFrom` with `module is None` — the `from . import y` / `from .. import y` form.
  Each `alias.name` resolves against the package named by `level` alone. This form is not
  optional: `src/hmc_mcp/server_tools/lpar/lifecycle.py:27-28` imports `lifecycle_boot` and
  `lifecycle_create` this way, and that module handles `hmc_delete_lpar` and
  `hmc_power_on_lpar`, so omitting it would leave both operations un-stale after an edit to
  either imported module — the silent omission ADR 0127 rejects hand-authored lists for.
  Five such statements exist in `src/hmc_mcp/` today.
- `Import` of a dotted `hmc_mcp.…` name. None exist in `src/hmc_mcp/` today (AST-verified),
  but the walk handles the form rather than assuming it stays absent.

**Resolution is defined for packages, not only modules**, because most real imports in this
tree name a package. A dotted target resolves to `<path>.py` when that exists, else to
`<path>/__init__.py`; every `__init__.py` on the resolution path is included and recursed
into, and an imported `y` resolving to `<pkg>/<y>.py` or `<pkg>/<y>/__init__.py` is included
and recursed into as well. Without this the closure is provably wrong where it matters most:
`src/hmc_mcp/server_tools/jobs.py:9` is `from ..jobs import JobOutcome`, where `hmc_mcp.jobs`
is a package and `JobOutcome` is a class, not a module. Under a module-file-only rule neither
`jobs/__init__.py` nor `jobs/core.py` would enter the closure of the handler module for
`hmc_get_job` and `hmc_wait_for_job` — so a change to `SUCCESSFUL_JOB_STATUSES`
(`src/hmc_mcp/jobs/core.py:28`), the very constant those scenarios assert against, would
leave their observations reading as current. The same applies to `from ..operations import
jobs as operations_jobs` and `from ..tool_registry import tool_module` at `:10-11`.

A name that resolves to no file under `src/hmc_mcp/` is skipped. Two containment rules go
with that: skip any path that is not a regular file or that is a symlink, and skip any
relative import whose `level` exceeds the containing package's depth (a naive `parts[:-level]`
yields an empty or negative slice and can resolve outside the package). The symlink rule
follows the module's existing convention — `check_capability_inventory.py` already guards
symlinks at `:400`, `:859`, `:872`, `:1001` and `:1020`, and `_implementation_paths`, the
function this walk replaces, is two of those. Both cases need a file committed under
`src/hmc_mcp/`, so this is reproducibility rather than a reachable attack: a symlink pointing
outside the repository makes the hash machine-dependent, so the runner's value and CI's
recomputation never agree and the observation reads `stale` forever.

**Traversal depth is part of the contract, not an implementation detail.** Follow `Import`
and `ImportFrom` nodes appearing in the module body and in the bodies of module-level `If`
and `Try` statements — that is what picks up `TYPE_CHECKING`-guarded imports, which sit
inside a module-level `If` and so are absent from `tree.body` itself. Do **not** follow
imports inside `FunctionDef`, `AsyncFunctionDef` or `ClassDef` bodies.

The distinction decides whether this design works at all. `src/hmc_mcp/__init__.py:15` holds
`from .cli import app` inside `main()`, and that file is on the resolution path of every
`hmc_mcp.*` name, so an `ast.walk` implementation pulls `cli.py` into every closure; `cli.py`
reaches `cli_commands/`, which reaches `server_tools.catalog`, which imports every tool
module. Measured over this tree, the two readings give:

| handler module | `ast.walk` | module body + `If`/`Try` |
|---|---:|---:|
| `hmc_mcp.server_tools.jobs` | 179 | 51 |
| `hmc_mcp.server_tools.lpar.lifecycle` | 179 | 75 |
| `hmc_mcp.server_tools.permissions` | 179 | 7 |

180 `.py` files exist under `src/hmc_mcp/`. The `ast.walk` reading collapses every closure to
the whole package, which is ADR 0126's repository-wide fingerprint wearing a different name —
it would make ADR 0127's "evidence stops counting when its operation's implementation
changes, not when an unrelated file gains a comment" false, and re-expose observations to the
1,441-commits-in-90-days churn this design exists to escape. A function-body import is a
deferred or cycle-breaking import, not part of the module's import-time implementation.

The walk recurses until closed, restricted to `src/hmc_mcp/`, and hashes the sorted
(path, bytes) sequence with the same length-prefixed SHA-256 construction 0126 used.
`scripts/` is excluded: the instrument changing is not the implementation changing, and the
instrument changes on most pull requests. The handler module is the `handler` field of
`discover_registry()`'s record with its last dotted component removed — `hmc_mcp.server_tools.jobs`
from `hmc_mcp.server_tools.jobs.hmc_get_job`, and `hmc_mcp.server_tools.permissions` from the
composed `hmc_effective_permissions` record, whose last component is `<composed>` rather than a
function name.

## Derived staleness and the report

**An operation carries at most one attempted live observation, and re-validating replaces
it.** The `id` is derived (`f"st{subtask}-{tool}"`) and so is identical on every run of the
same scenario, while `id` must be unique catalog-wide — so a catalog that accumulated
observations would reject the second run of any scenario. Re-validation after a stale mark is
the design's only forcing function, and it must not terminate in a validation error the first
time an operator uses it. Superseded observations are not history the catalog needs to hold:
the file is versioned, so the previous record is in `git log` where a reader can find it, and
nothing in the report ever reads a non-latest observation.

For each operation the report derives one state from that observation:

| State | Condition |
|---|---|
| `unrecorded` | no maturity record |
| `unevidenced` | record, no live observation |
| `stale` | the observation's `closure_fingerprint` ≠ recomputed, or `observed_at` older than 90 days |
| `failed` | the observation is `failed` and not stale |
| `current` | the observation is `passed` and not stale |

Each per-operation line carries the **implementation state beside the derived state** —
`verification: sriov.set_mode partial current`, not `verification: sriov.set_mode current`.
Format 2 drops the per-observation `scope`, and with it format 1's check that a current
observation names an implemented scope, so the derived state alone no longer says which
variant of a partly implemented operation was exercised. The catalog holds exactly this case
today: `sriov.set_mode` is `partial`, its implemented scope is `current-mode-confirmation`
("equals the adapter's current config_state; returns unchanged") and its missing scope is
`adapter-mode-transition`. A bare `current` would tell a reader that setting an adapter's
mode is live-verified when the only verified variant is the one that changes nothing.
`derive_states` already receives the maturity records, so this costs one column and no
schema change.

Staleness is never a validation error. `check_capability_inventory.py --verification-report`
prints one line per operation and a summary
`verification coverage: <n> unrecorded, <n> unevidenced, <n> stale, <n> failed, <n> current (of <total>)`,
exits 0, and — when `GITHUB_ACTIONS` is set — emits one `::warning::` per stale operation and
appends the table to `$GITHUB_STEP_SUMMARY`. `--fail-on-stale` exits 1 when any operation is
`stale`. The 90-day ceiling is one module constant.

ADR 0126's repository-wide `implementation_fingerprint` and its stale-fingerprint error
block are deleted; nothing consumes them.

## Runner emission

`LIVE_TEST_ENV_HMC_RELEASE` and `LIVE_TEST_ENV_HARDWARE_FAMILY` are read from `.env`; both
or neither, a lone one is a configuration error. `LiveTestConfig.from_env_file` skips the
`LIVE_TEST_ENV_` prefix, and `.env.example` carries both keys.

At the end of a run, when both are set, the tree is clean under `src` and `scripts`
(`git status --porcelain`), and at least one `record_verified` observation exists, the
runner writes `<results-stem>-observations.json`: a list of `{"operation": …,
"observation": {…}}` objects in exactly the catalog shape, `tested_commit` from
`git rev-parse HEAD`. A dirty tree or missing environment prints why and writes nothing.
`.gitignore` gains `test-results*.json`, which covers this path and closes the existing gap.
A human copies observations into `maturity.json`; the runner never writes there.

## CI

`justfile` gains `verification-report *ARGS`, running
`uv run --no-sync python scripts/check_capability_inventory.py --verification-report {{ARGS}}`.
It is not a `static` member and has no prek hook. `ci.yml` gains one job,
`verification-report`, shaped like `python-support-drift`: `ubuntu-24.04`, five-minute
timeout, pinned checkout and `setup-uv`, `just setup`, then two steps — `just
verification-report` guarded `if: github.event_name != 'schedule'` and `just
verification-report --fail-on-stale` guarded `if: github.event_name == 'schedule'`. No
expression appears in a `run:` line. `permissions:` is untouched. `tests/test_ci_pipeline.py`
gains one test for the job and updates the `ubuntu-24.04` and checkout-settings counts.

## Threat model

**Boundaries.** The validator parses `maturity.json` (already did); the runner reads two new
`.env` keys whose values reach a committed record; CI gains a job that reads the repository
and writes a job summary.

**Actors.** A contributor opening a pull request; a local operator with a real HMC. No
network actor: the validator and report are offline.

**Controls.** Every field of a committed observation is a closed vocabulary, hash, SHA, date
or pattern-bound id, except the two environment strings, which match `\AV\d+R\d+(?:M\d+)?\Z`
and `\APOWER\d+\Z` — grammars narrow enough to exclude a hostname, serial or location code
rather than merely a dotted quad. There is one observation shape, so that statement has no
exception.

There is no second shape to reason about. ADR 0126's `not-run` row carried three prose fields
and was the one place in the format where review rather than validation would have been the
control; it is deleted instead of bounded, so the closed-shape claim holds for every committed
observation without a carve-out.

The runner refuses to emit to a path `git check-ignore` does not claim, rather than trusting
a fixed pattern to cover every destination: `--results-file` accepts an arbitrary stem, and
the atomic write's `.{name}.<rand>.tmp` file — which holds the full results document,
including the verbatim HMC responses kept on the PASS path — needs its own `.test-results*.tmp`
pattern. The CI job has `contents: read` and no secret. Workflow `run:` lines carry no
`${{ }}` expression.

**Out of scope.** Proving an observation true; a maintainer committing a fabricated
observation together with a matching closure hash — the record is small and reviewed.

## Verification

| Contract | Evidence |
|---|---|
| dispatch keywords match served schemas; 23 corrected | argument guard, green |
| unreadable dispatch fails the guard | guard test over a `**` source |
| `CallFailure` classifies HTTP status and a real ADR 0038 denial | unit tests |
| `ExpectedOutcome` matches whole tokens in the message, not the traceback | unit test |
| `record` cannot yield `passed`; `record_verified` fails on a false assertion or failed cleanup | unit tests |
| job assertions use `SUCCESSFUL_JOB_STATUSES` | unit test with a `FAILED_BEFORE_COMPLETION` entry |
| format 2 shape; format 1 rejected | validator tests |
| closure fingerprint changes when an imported module changes, not an unrelated one | validator test over a fixture package |
| each derived state, the summary line, `--fail-on-stale` exit code | validator tests |
| env-string pattern and IPv4 rejection | validator tests |
| runner writes nothing on a dirty tree or missing env; writes catalog-shaped objects otherwise | runner tests |
| `from_env_file` accepts the extended `.env.example` | existing complete-example test |
| CI job shape and justfile recipe | `tests/test_ci_pipeline.py` |
| `.gitignore` covers `test-results-round2.json` | `git check-ignore` in a test |

## Global constraints

- Python 3.11 floor; CI runs 3.11–3.14 on amd64 and arm64.
- No new runtime or development dependency.
- Every `uv run` in `justfile` passes `--no-sync`.
- `ci.yml` keeps exactly one `permissions:` block, `contents: read`; no `workflow_dispatch`;
  no `fetch-depth`; every action pinned to the SHAs in `tests/test_ci_pipeline.py::ACTION_PINS`.
- Guardrails: `just verify`, then `uv run --no-sync prek run --all-files`.
