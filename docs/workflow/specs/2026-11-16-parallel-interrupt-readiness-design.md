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
- Covered elsewhere: worker limits #2000; CI group membership #1998; production diagnostic
  and cleanup contracts ADR 0130/0208. No HMC, dependencies, or warning-policy changes.

## Validation

One coupled implementation task covers readiness and real supervisor waits. The plan
inventories focused evidence. The existing Linux failure is the red cleanup observation;
negative marker tests and mutations prove readiness does not accept partial/duplicate
identity. Stress both delivery modes, including the causal contention schedule. Pure
prose needs no invented text assertions. No new production or untrusted-input boundary.
