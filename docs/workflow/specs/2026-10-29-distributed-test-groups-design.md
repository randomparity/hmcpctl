# Distributed test-group design

Authority: operator request to split PR #1823's test groups, followed by approval
of four groups, per-version combined coverage and a twelve-minute 3.12 target.
Decision: [ADR 0309](../../adr/0309-distributed-test-groups.md).

## Scope and ownership

`scripts/run_tests.py` remains the owner of pytest selection, resource admission,
child cleanup and summaries. Add explicit group and coverage-combination modes;
no environment switch can silently replace the default full run.
The workflow owns fanout/dependencies/artifact transport; just recipes remain
the command boundary. Existing CI guards migrate with this graph.
Previous checkpoint and definition-owned schema changes remain intact.

| Group | Selection | Current cases |
| --- | --- | ---: |
| application | `tests/app/`, unit registry/schema-template modules | 2,531 |
| tooling | `tests/scripts/` | 8,069 |
| procedure-arms | root `tests/test_live*.py` files | 3,369 |
| domain-and-unit | pytest `tests/`, ignoring precisely the preceding selections | 10,570 |

Use pytest's existing filename/collection rules. The remainder group includes
future directories and alternative pytest filenames. No physical test move,
parameter rewrite, fixture relocation or application/library dependency change.

## Interfaces and flow

- `python scripts/run_tests.py --group GROUP [--timings] [--serial]` runs one
  whitelisted group. `--cov-fail-under=0` is confined to this explicit mode;
  branch measurement stays enabled. Emit `.coverage` and `dist/test-results.xml`.
  Success says group passed and combined coverage pending, never full gate passed.
- `python scripts/run_tests.py --combine-coverage PYTHON DIRECTORY` validates
  the current interpreter matches PYTHON (3.11–3.14), then the exact artifact set
  `test-coverage-amd64-pyPYTHON-GROUP/.coverage` below DIRECTORY.
  Refuse missing, extra, empty, symlinked or nonregular inputs. Native coverage
  JSON reads must succeed and identify branch data for each input before combine.
  Copy each exact input to its own fresh temporary data directory before any
  native read. Keep the aggregate in a separate fresh temporary directory and
  pass its explicit `--data-file` to combine and report. Native report can otherwise
  discover/delete unrelated suffixed shards. Preserve original inputs and caller
  coverage files. Use native `coverage combine --keep` and `coverage report`, preserving the
  configured exact floor/precision; propagate any failure. Sanitize the same
  coverage-steering environment variables as the pytest producer.
- `just test-group GROUP` invokes the existing supervisor with timings enabled.
  `just test-collected PYTHON` combines `coverage-parts/`.
  `just verify-collected PYTHON` composes that gate with the existing MCP smoke,
  build, artifact and CLI checks. `just test` and `just verify` remain full-suite.

Three CI stages retain amd64 Ubuntu 24.04 × Python 3.11–3.14. `static` runs the
existing real hook command. All three source-installing stages fetch full history
for Git-derived versions. `test-groups` depends on static success and fans out
four groups per version, at most sixteen jobs, each keeping the existing
two-worker eligibility and hard timeouts. Every test job uploads its exact
coverage file (explicit hidden-file inclusion) and JUnit report after success.
Retain JUnit/timing diagnostics on failures when present; no success artifact
is produced by a failed test invocation.

The existing `ci` matrix keeps its per-version verify check names. It runs with
`always()` and an explicit success guard for both producer stages. Matrix-wide
dependency results conservatively fail all aggregators if any producer failed,
was cancelled or skipped. A cancelled entire workflow is not a green check.
Download only the current run's artifact prefix for that version, without merging
directories. Validate before report; coverage CLI commands have finite timeouts.
Only successful verification retains the version's wheel. Existing downstream
wheel/library jobs and their needs remain unchanged.

## Success

1. The current 24,539 baseline node IDs appear in exactly one group; subsequent
   added tests are accounted for separately. Unknown group names fail before pytest.
2. Each group passes on 3.11–3.14; original worker/signal/timeout tests still pass.
   Group failures cannot be rendered as success or a passing coverage gate.
3. Four individually readable branch data files of the selected version are
   required. Missing, extra, malformed, statement-only, wrong-version and failed
   producer cases refuse. Combined below-floor coverage fails; passing union passes.
4. Static hooks, complete runtime verification, stable verification names and
   fresh-wheel consumers retain coverage of all supported versions. No partial
   artifact or failed aggregate can release a validated wheel.
5. In confirming CI, each Python 3.12 test-group job takes at most twelve minutes,
   measured from job start through completion, excluding queue time. All required
   checks must pass at the final head. No promise applies to arbitrary future loads.

## Failure model

- Actors/deployment: offline developer and GitHub-hosted CI runs of trusted
  repository test/tooling code; no HMC access. CI artifacts are from this workflow
  run and checkout. This is not protection against malicious PR code rewriting gates.
- Invariants/assets: complete test selection, exact per-version branch coverage,
  original subprocess ownership, and truthful verification/wheel lineage.
- Accepted classes: runner scarcity can delay queued jobs; shared fixtures retain
  app imports; matrix-wide dependency failure can conservatively fail other versions.
  A whole-workflow cancellation remains non-success rather than a recovery promise.
- Covered elsewhere: existing supervisor owns worker admission and child closeout;
  existing smoke/build validators own installed behavior and distribution contents.
  GitHub check administration stays maintainer-owned; no administrative change.

## Threat model

Boundaries are operator group/version inputs, pytest child execution, cross-job
coverage artifacts and the final verification/wheel gate. Whitelists, existing
supervision, current-run/version artifact names, exact input-set validation, native
data reads and explicit dependency guards hold them. Permissions stay read-only;
reuse pinned actions and locked tools. No network destination or hardware effect.

## Verification and rollback

Use real miniature pytest/coverage projects for selection, JUnit and union/failure
proofs, plus workflow route/failure tests. Compare actual full/group collection IDs;
retain group durations and reports. Run affected tests, lint/types, full verify and
pinned hooks, independent reviews and the actual distributed CI graph.
Revert the isolated grouping commits to restore the previous producer graph;
retain the earlier checkpoint/schema fixes. No product data migration or cleanup.
