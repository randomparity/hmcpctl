# Hosted coverage and worker cost measurements (#2000)

## Scope and success
[#2000](https://github.com/randomparity/hmcpctl/issues/2000) requests measurements only.
C1: compare application with/without coverage at two workers on each Python 3.11–3.13.
C2: compare covered two/three/four workers, with wall time and fresh cgroup memory.peak.
C3: report exact commit/lock/interpreters/packages/core, resources, hashes, order and limits.
Cycle 2 adds durable current-sample evidence before identity/coverage rejection: elapsed/resources,
expected/actual hashes, full coverage maps, JUnit, bounded logs and source/input fingerprints.
Authority: [complete scope](https://github.com/randomparity/hmcpctl/issues/2000#issuecomment-6946127768).
The first run at `11b84927b713d52206d324c7f679ca1bd60859c3` rejected Python 3.12 sample 8,
but its coverage map/hash/elapsed were not uploaded. That underlying drift remains unexplained.
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
The original three-version run is terminal; preserve its artifacts without retry or comparison.
Cycle 2 permits one fresh Python 3.12 diagnostic in the same eight-sample order, based on the
original measured workload/selector/runtime/test inputs. No selector-changing main integration.
Only deliberate `[diagnose-2000]` commit or explicit dispatch enables that diagnostic.
Limits: 720-second workload plus at most 15-second containment/sample, 60 minutes/job and
overall including push/queue, 60 aggregate runner-minutes; cumulative target jobs <=172.22.
Start one monotonic deadline before push; cancel before expiry with a 60-second reserve.
Fingerprint source/fixtures/selector before and after each sample; stop on changed inputs.
Retain metadata before rejecting a different build/package/lock/core fingerprint from original Py3.12.
Record actual hmcpctl distribution version separately; the new commit necessarily differs from
original `0.2.1.dev158+g11b84927b`. Proven equality covers workload bytes/args/lock and named tools only.
Persist current observation and raw owned outputs before the unchanged identity/coverage guards.
Collect available fields independently after service/parser failure; record field errors and rethrow.
Drain logs continuously, retaining first/last MiB with omitted-byte count; do not bound the child
by truncating its pipe. Artifacts allowlist only owned diagnostic files, including raw JUnit/maps.
Errors/mismatches stop the job and retain partial evidence without restart or expansion.
Even a passing diagnostic cannot clear original drift or authorize comparative publication;
return raw evidence for a further operating decision. C1/C2 remain owed, with no inferred ratios.

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
  A nonreproducing diagnostic does not identify the original cause; truncated logs lose their middle.
- Elsewhere: ADR0208 eligibility/lifetime; ADR0034/0309 coverage; #1998 grouping; normal CI.

## Threat model
- Added: trusted workflow→sudo/systemd→same-user pytest and local XML/JSON; widened: none.
- Trust: author/pinned Actions/runner; no fork trigger; contents:read and credential-free checkout.
- Controls: fixed modes/slots and argv, bounds, exact GITHUB_SHA, unchanged matched hashes,
  owned-output artifact allowlist and source/input fingerprints; no wholesale environment forwarding.
  Raw offline diagnostic artifacts can include runner XML fields/logs; human reports omit these.
- Out of scope: compromised author/dependencies/runner (platform trust); no public data schema.
