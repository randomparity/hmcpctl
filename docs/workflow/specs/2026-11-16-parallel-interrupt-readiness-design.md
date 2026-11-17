# Deterministic real parallel interrupt probes

## Problem

[#1909](https://github.com/randomparity/hmcpctl/issues/1909) reports exit 1 and a
surviving worker from a real interrupt probe. On base `425fb3cf`, Linux aarch64,
Python 3.13.11, one-CPU contention with 24 normal-priority busy processes and
pytest descendants at nice 19 reproduced `(1, [controller])` in 9.15 seconds.
Both worker markers existed; stderr named bounded cleanup failure. Restoring
only the production termination bound produced `(130, [])` but lost the diagnostic.
Restoring both production bounds produced `(130, [])` and `KeyboardInterrupt` in
20.82 seconds. Separately, one shared ready file proves only one worker is ready.

## Scope

The test probe remains the synchronization owner; `scripts/run_tests.py` remains
the supervisor/reap owner. No ownership transition or caller migration is needed.
`tests/scripts/test_run_tests.py` adds a bounded wait for identities `gw0` and `gw1`,
requires distinct PIDs, and waits for each exact PID readiness marker. Real fixture
workers atomically publish identity, install the default interrupt handler, then publish readiness.
Real-worker probes retain production interrupt and termination bounds; synthetic
controllers keep their existing short overrides. Survivor inventory stays immediately
after `r.main()`, before fixture containment. See [ADR 0385](../../adr/0385-real-worker-probe-synchronization.md).

## Success

- Both real workers install handlers before the probe delivers its single interrupt (C2).
- Both existing delivery modes retain status 130, no survivors, KeyboardInterrupt,
  no INTERNALERROR, restored handler and subreaper state (C2).
- Linux stress under load and mutation proof exercise readiness and reap; focused tests,
  `just verify`, pinned prek, and exact-head hosted CI pass (C1/C3).

## Failure model

- Actors and deployments: trusted pytest fixtures in Linux local/container/hosted CI;
  Python 3.11–3.15; hosted amd64 remains separate from local aarch64 evidence.
- Invariants and assets: one signal after two distinct ready workers; diagnostic retained;
  real supervisor reaps before inventory; containment cannot turn survivors into success.
- Accepted failure classes: missing readiness/early worker exit fails within the existing
  readiness ceiling; pathological scheduling beyond fixture collection ceiling fails.
- Covered elsewhere: hosted worker-count/tracer measurements #2000; CI groups #1998;
  production diagnostic/reap contracts ADR 0130/0208. Worker-cap changes are excluded;
  none is owed or deferred by #1909/#2000. A future cap decision belongs to the maintainer
  under a separate ADR. No HMC, dependencies, or warning-policy changes.

## Validation

The plan maps readiness and real supervisor waits to focused red/green and mutation
checks, plus both-mode loaded Linux stress. Pure prose needs no text assertions.

## Threat model

- Boundaries: no added/widened production boundary; generated PID identities are read
  from the isolated fixture directory and used in readiness paths (security review trigger).
- Actors: trusted local/CI fixture producer; no external input or credential use.
- Controls: atomic identity publication, integer parsing, distinct PIDs, bounded marker
  waits, and existing owned-process cleanup/containment.
- Out of scope: hostile modification of the fixture's private temporary tree; its
  producer is trusted under the existing test model. Production authorization is unchanged.
