# Hosted coverage and worker cost measurements (#2000)

## Scope and success
[#2000](https://github.com/randomparity/hmcpctl/issues/2000) requests measurements only.
C1: compare application with/without coverage at two workers on each Python 3.11–3.13.
C2: compare covered two/three/four workers, with wall time and fresh cgroup memory.peak.
C3: report exact commit/lock/interpreters/packages/core, resources, hashes, order and limits.
Application is one representative multi-file shard; reuse its selector without ownership transition.

## Architecture and experiment
Same-user systemd services use explicit venv/system PATH, fresh memory accounting, 12 GiB
MemoryMax, 720-second runtime and control-group cleanup with five-second stop grace.
A 20-second service probe checks native amd64, four effective CPUs, 12 GiB host available
memory, cgroup accounting and this venv's console script; retain limits/charges and tracer core.
Eight samples/version: 2 covered, 2 uncovered, 3 covered, 4 covered; then reverse.
Reuse loadfile, no worker restarts, branch data and production partial-group floor zero.
Require matching case/outcome hashes and per-file coverage hashes before comparisons.
Use fresh VMs, no setup-uv cache, one locked venv/version; report warming OS/interpreter caches.
Preflight-only first push: five minutes/job, ten overall including queue, 15 runner-minutes.
After design review/audit send actual prerequisites, candidate and plan to controller.
Only deliberate `[measure-2000]` commit or explicit dispatch enables the fixed experiment:
24 samples, 12 minutes/sample, 60/job, 180 runner-minutes, 60 overall including queue.
Start a monotonic deadline at dispatch; cancel on expiry. Errors/mismatches stop the job;
retain partial evidence without comparison, restart or expansion. Report samples and medians;
covered/no-cov delta is total coverage overhead, without isolated-tracer/significance claims.

## Global Constraints
Project Python >=3.11; production CI amd64 ubuntu-24.04, Python 3.11–3.15.
The diagnostic targets native amd64 ubuntu-24.04, Python 3.11–3.13 only.
Use existing pinned actions/tools and locked app/dev dependencies; add no dependency.
Use `just setup`, then `uv run --no-sync`; do not access an HMC.
Keep production worker cap two, coverage floors and four-group membership unchanged.

## Failure model
- Actors/deployments: trusted author, fixed offline tests, hosted ubuntu-24.04 runners.
- Assets/invariants: truthful matched evidence, bounded cost/descendants, unchanged policy.
- Accepted: two ordered samples/mode have cache/order/runner noise and cover one shard;
  memory.peak includes charged cache, not summed RSS; failed samples require a new decision.
- Elsewhere: ADR0208 eligibility/lifetime; ADR0034/0309 coverage; #1998 grouping; normal CI.

## Threat model
- Added: trusted workflow→sudo/systemd→same-user pytest and local XML/JSON; widened: none.
- Trust: author/pinned Actions/runner; no fork trigger; contents:read and credential-free checkout.
- Controls: fixed modes/slots and argv, bounds, exact GITHUB_SHA, matched hashes, JSON allowlist;
  no wholesale environment forwarding. Report omits raw XML hostnames/logs/private paths.
- Out of scope: compromised author/dependencies/runner (platform trust); no public data schema.
