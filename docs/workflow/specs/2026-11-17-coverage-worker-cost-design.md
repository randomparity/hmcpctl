# Hosted coverage and worker cost measurements (#2000)

## Scope and success
[#2000](https://github.com/randomparity/hmcpctl/issues/2000) requests measurements only.
C1: compare application with/without coverage at two workers on each Python 3.11–3.13.
C2: compare covered two/three/four workers, with wall time and fresh cgroup memory.peak.
C3: report exact commit/lock/interpreters/packages/core, resources, hashes, order and limits.
Cycle 2 adds durable current-sample evidence before identity/coverage rejection: elapsed/resources,
expected/actual hashes, full coverage maps, JUnit, bounded logs and source/input fingerprints.
Authority: [scope](https://github.com/randomparity/hmcpctl/issues/2000#issuecomment-6950377766).
The first run at `11b84927b713d52206d324c7f679ca1bd60859c3` rejected Python 3.12 sample 8,
but its coverage map/hash/elapsed were not uploaded. That drift remains unexplained under #2021.
Application is one representative multi-file shard; reuse its selector without ownership transition.

## Architecture and experiment
Same-user systemd services use explicit venv/system PATH, fresh memory accounting, 12 GiB
MemoryMax, 720-second runtime and control-group cleanup with five-second stop grace.
A 20-second service probe checks native amd64, four effective CPUs, 12 GiB host available
memory, cgroup accounting and this venv's console script; retain limits/charges and tracer core.
The original experiment assigned eight samples/version: 2 covered, 2 uncovered, 3 covered,
4 covered; then reverse. The later diagnostic ran this same order on Python 3.12 only.
Reuse loadfile, no worker restarts, branch data and production partial-group floor zero.
Require matching case/outcome hashes and per-file coverage hashes before comparisons.
Use fresh VMs, no setup-uv cache, one locked venv/version; report warming OS/interpreter caches.
The initial preflight-only push used five minutes/job, ten overall including queue,
and 15 runner-minutes.
Both design cycles, audits and the one additional diagnostic are complete; budgets are not reset.
The original three-version run remains FAILED; preserve its artifacts without retry or clearance.
The diagnostic preserved the original measured workload/selector/runtime/test inputs before any
selector-changing main integration. Later report/control/main integration is unmeasured.
The whole isolated job requires the existing deliberate `[diagnose-2000]` push marker or explicit
dispatch measure input before runner provisioning; ordinary unmarked pushes skip the entire job.
Limits: 720-second workload plus at most 15-second containment/sample, 60 minutes/job and
overall including push/queue, 60 aggregate runner-minutes;
cumulative target jobs <=172.22 runner-minutes.
Start one monotonic deadline before push; cancel before expiry with a 60-second reserve.
Fingerprint source/fixtures/selector before and after each sample; stop on changed inputs.
Retain metadata before rejecting a different build/package/lock/core fingerprint
from original Py3.12.
Record actual hmcpctl distribution version separately; the new commit necessarily differs from
original `0.2.1.dev158+g11b84927b`. Proven equality covers workload bytes/args/lock
and named tools only.
Persist current observation and raw owned outputs before the unchanged identity/coverage guards.
Collect available fields independently after service/parser failure; record field errors
and rethrow.
Drain logs continuously, retaining first/last MiB with omitted-byte count; do not bound the child
by truncating its pipe. Artifacts allowlist only owned diagnostic files, including raw JUnit/maps.
Errors/mismatches stop the job and retain partial evidence without restart or expansion.
The passing diagnostic did not clear the original drift. The later operator decision admits only
separate complete guard-passing within-version descriptive cohorts: original 3.11/3.13 after
all eight recorded admission/identity/coverage/resource results are verified, and diagnostic 3.12.
Exclude original 3.12's seven-row partial cohort from estimates; #2021 owns the unresolved drift.
Disclose missing old raw JUnit/maps, cache/order/one-shard/host/SCM/instrumentation/resource limits.
Never pool cohorts, infer cross-version or before/after effects, claim stable population
performance, or recommend cap/floor policy. No further experiment, preflight or version run
is authorized.

## Global Constraints
Project Python >=3.11; production CI amd64 ubuntu-24.04, Python 3.11–3.15.
Measurement cohorts cover native amd64 ubuntu-24.04, Python 3.11–3.13; the isolated workflow's
remaining matrix is Python 3.12 only, with no further run authorized by this report.
Use existing pinned actions/tools and locked app/dev dependencies; add no dependency.
Use `just setup`, then `uv run --no-sync`; do not access an HMC.
Keep production worker cap two, coverage floors and four-group membership unchanged.

## Failure model
- Actors/deployments: trusted author, fixed offline tests, hosted ubuntu-24.04 runners.
- Assets/invariants: truthful matched evidence, bounded cost/descendants, unchanged policy.
- Accepted: two ordered samples/mode have cache/order/runner noise and cover one shard;
  memory.peak includes charged cache, not summed RSS; failed samples require a new decision.
  A nonreproducing diagnostic does not identify the original cause; truncated logs
  lose their middle.
- Elsewhere: ADR0208 eligibility/lifetime; ADR0034/0309 coverage; #1998 grouping; normal CI.

## Threat model
- Added: trusted workflow→sudo/systemd→same-user pytest and local XML/JSON; widened: none.
- Trust: author/pinned Actions/runner; no fork trigger; contents:read and credential-free checkout.
- Controls: fixed modes/slots and argv, bounds, exact GITHUB_SHA, unchanged matched hashes,
  owned-output artifact allowlist and source/input fingerprints;
  job admission precedes VM allocation;
  no wholesale environment forwarding.
  Raw offline diagnostic artifacts can include runner XML fields/logs; human reports omit these.
- Out of scope: compromised author/dependencies/runner (platform trust); no public data schema.
