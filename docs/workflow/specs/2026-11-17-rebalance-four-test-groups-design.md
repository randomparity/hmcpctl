# Rebalance four test groups (#1998)

## Problem

Successful normal CI runs [44039914716](https://github.com/randomparity/hmcpctl/actions/runs/44039914716)
and [44038622015](https://github.com/randomparity/hmcpctl/actions/runs/44038622015)
use the same tree as main `3f4a390c7`. Application summed test costs still dominate several
legs after #2004. Moving the entire CLI prefix can overfill the receiving group.

## Scope and ownership

Keep `_group_arguments(group: str, root: Path = Path(".")) -> list[str]` as the selector.
Move exactly `tests/app/test_cli_commands.py` and `tests/app/test_cli_e2e.py` from
`application` to `domain-and-unit`: these are the broad direct-command and HTTPS CLI
integration suites; feature-specific presentation suites remain with application tests.
Use one literal tuple and complementary exclusions: application ignores these two files;
domain retains the broad tests root and ignores other immediate application children.
This avoids overlapping pytest roots; a private domain-argument helper owns that complement.
Keep the default catch-all, application units, procedure glob and tooling directory.
No test files, node IDs, fixtures, entry points or public group names move on disk.
The two existing callers (`just test-group` and normal CI) need no migration.
Replace the old application directory-only cut, without a compatibility branch.
[ADR 0387](../../adr/0387-rebalance-four-test-groups.md) supersedes this part of ADR 0309.

## Success

1. The full repository collection equals the disjoint union of its four group collections
   on the installed local interpreter and each normal hosted Python leg, including future
   nonmatching application, domain and live-arm test files in synthetic fixtures.
2. The selected CLI suites belong only to `domain-and-unit`; the remaining application
   files and two application-unit files retain their owner.
3. Publish baseline and one actual after-cut normal CI run's per-group job and test-step
   times, commit identities, completeness and sampling limitations in the PR.
4. Preserve the four names/checks/artifacts, two-worker cap and per-version combined
   coverage gate. No fifth group, floor change, dynamic timing loader or new dependency.

## Global constraints

Python >=3.11; hosted amd64 Ubuntu 24.04, Python 3.11–3.15; local arm64 is separate evidence.
Use locked app extras via `just setup`, then `uv run --no-sync`. Preserve four groups,
worker cap two and per-version configured combined coverage floor. No HMC access.

## Failure model

- Actors and deployments: trusted checkout operators, normal GitHub CI amd64 jobs on the
  five declared Python versions, and local macOS arm64 verification.
- Invariants and assets: exact collected test ownership; coverage producer completeness;
  bounded existing subprocess supervision; truthful hosted timing evidence.
- Accepted failure classes: hosted resource/cache/noise can prevent a speedup in one leg;
  this change promises measured reporting, not a runtime SLA or representative median.
  Renamed selected suites follow application/default ownership until the measured cut is
  updated; the source-review and synthetic membership check hold the current literal cut.
  New nonmatching files follow their existing directory/default owner.
- Covered elsewhere: existing runner tests and CI-pipeline tests own supervisor, worker
  admission and four-artifact coverage rejection; unchanged CI owns cancellation guards.

## Threat model

- Boundary inventory: no new boundary; changed existing pytest argv contains only trusted
  repository literal paths and existing local glob results. No new input format or secret.
- Actors: repository contributors control tracked files; CI executes reviewed checkout code.
- Controls: existing group enum validation, literal tuple, subprocess list arguments and
  no shell execution; existing file-empty/unknown-group failures remain tested.
- Out of scope: malicious repository code execution, owned by normal code review/platform
  permissions; timing artifact XML is read only in private bounded profiling, not production.

## Validation

One coupled Cast task proves selection and collection together. Update the existing synthetic
partition test with both selected CLI files, an unmatched future CLI file and a nested application file; assert exact
owners and disjoint union. First run must fail on the old application-only cut.
Add a repository collection-union regression using five isolated `pytest --collect-only`
subprocesses with coverage disabled and bounded timeouts. Reject empty/duplicate collections
and report set differences. This test executes in tooling on each hosted version.
Deliberately remove an application ignore or add the whole app directory to domain exclusions and require the appropriate test to fail,
then restore. Retain existing unknown/empty-arm, subprocess and coverage failure-path tests.
Run focused runner plus CI-pipeline tests and static/doc gates before committing. Commit the
candidate before final `just verify` and pinned all-file prek; refresh SCM metadata as prescribed.
The normal candidate CI matrix supplies the declared-host proof and after-cut timing sample.
The human timing report/ADR/operations prose has no executable consumer; review claims against
captured successful artifacts instead of inventing prose assertion tests.
