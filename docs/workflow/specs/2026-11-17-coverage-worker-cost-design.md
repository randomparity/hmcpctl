# Hosted coverage and worker cost measurements (#2000)

## Problem and scope

Issue [#2000](https://github.com/randomparity/hmcpctl/issues/2000) needs comparable
hosted numbers before a maintainer considers coverage or worker policy. Measure only;
keep the production runner, four groups and coverage floor unchanged. #1998 owns rebalancing.
The application group is the representative shard: producer run
[44034277166](https://github.com/randomparity/hmcpctl/actions/runs/44034277166) has
3,070 cases/12 skips on 3.12 and multiple files under loadfile scheduling.

## Success

C1: On one immutable candidate, compare application at two workers covered versus
`--no-cov` on each of Python 3.11, 3.12 and 3.13. C2: compare covered two, three and four
workers, recording wall time and fresh kernel cgroup memory.peak for each sample.
C3: Publish exact commit, dependency lock, interpreter/coverage versions, host resources,
case/outcome hashes, covered-file hashes, sample order and actual limitations.
Require matching case/outcome hashes across the eight samples per interpreter and matching
per-file coverage across its six covered samples before publishing a comparison.

## Architecture and experiment

One diagnostic workflow bootstraps with existing pinned actions and `just setup`.
One paired offline script reuses `run_tests._group_arguments("application")`; the production
runner remains the owner of membership. There is no ownership transition or runtime caller.
Owned same-user systemd services bound each sample to 720 seconds and 12 GiB;
preflight uses 20 seconds. Read each fresh cgroup peak before its destruction.
Validate native amd64/Python/commit, four effective CPUs, 12 GiB host available memory
and fresh cgroup-v2 accounting; retain ancestor limits/charges and package versions.

Order: 2 covered, 2 uncovered, 3 covered, 4 covered; then reverse (eight per version).
Reuse loadfile, no worker restarts, branch data and production partial-group floor zero.
Use one locked venv per version, no setup-uv cache, fresh VM, warming interpreter/OS caches.
Report samples and two-sample medians without significance claims.

Preflight-only first push: five minutes/job, ten overall including queue, 15 runner-minutes.
After review/audit send actual prerequisites, candidate and plan to controller before launch.
A deliberate `[measure-2000]` commit marker or explicit dispatch enables measurement.
Ceilings: 24 samples, 12 minutes/sample, 60/job, 180 runner-minutes, 60 overall including queue.
Start a monotonic controller deadline at dispatch; cancel on expiry. Errors, timeouts or
mismatches stop the affected job. Retain partial evidence; no automatic rerun or expansion.

## Global Constraints

Project Python >=3.11; production CI amd64 ubuntu-24.04, Python 3.11–3.15.
The diagnostic targets native amd64 ubuntu-24.04, Python 3.11–3.13 only.
Use existing pinned actions/tools and locked app/dev dependencies; add no dependency.
Use `just setup`, then `uv run --no-sync`; do not access an HMC.
Keep production worker cap two, coverage floors and four-group membership unchanged.

## Failure model

- Actors and deployments: trusted repository author and GitHub Actions ubuntu-24.04 runners;
  fixed offline application tests; no user-supplied workload or arbitrary remote command.
- Invariants/assets: truthful matched comparisons, finite hosted cost, owned descendant
  lifetime, unchanged production verification policy and sanitized published evidence.
- Accepted classes: two ordered observations per mode have warm-cache/order/runner noise;
  memory.peak includes charged cache and is not summed RSS; results cover one shard only;
  failed/partial observations have no comparison claim and require a new operating decision.
- Covered elsewhere: production eligibility/lifetime ADR0208; coverage ADR0034/0309;
  normal five-version CI and final local guardrails qualify shipping; #1998 owns grouping.

## Threat model

- Added boundaries: trusted workflow invokes sudo/systemd with fixed properties and same-user
  pytest; pytest writes local XML/JSON read by the observer. Existing boundaries widened: none.
- Actors/trust: repository author and pinned Actions are trusted; test output is local evidence,
  not commands. No fork-triggered workflow and contents:read only; checkout credentials disabled.
- Controls: fixed group/modes and bounded slots; no shell command interpolation; service bounds
  its cgroup; commit matches GITHUB_SHA; hashes bind identities and coverage; report omits raw
  XML hostname fields, private paths and raw pytest logs. Upload only bounded evidence JSON.
- Out of scope: compromised repository author/dependency/runner images are platform trust;
  raw test artifact formats are temporary diagnostics, not a supported public schema.
