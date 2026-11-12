# ADR 0370: Settle vNIC witness allocation counters before the baseline

## Status

Accepted (2026-11-12), under the operator-confirmed campaign dispatch for #1936.

## Context

The post-delete witness is a version-3 fixture `prepare` preflight followed by its
independent recovery (`docs/live-testing.md`, #1858 step 4). In one #1858 window on
HMC V10R3 M1060 the managed system's allocation counters still carried a
just-deleted fixture's 0.05 processor units and 512+32 MiB, and kept moving while
recovery re-read them, so recovery's single-shot equality returned MANUAL_RECOVERY.
A read-only witness minutes later returned READY and UNCHANGED. The #1857 window
saw no lag. That release is observed behaviour, not documented HMC behaviour.

## Decision

A fixture preflight whose packet action is `prepare` reads only the managed
system's memory and processor sections, at most 12 times, 10 seconds apart, before
its full snapshot. It journals every read. It stops at the first two consecutive
equal reads. Between reads only the six fixture allocation counters may change,
each only in its release direction. Their total movement stays within one fixed
fixture's allocation. Any other change, the wrong direction, or no settled pair by
the twelfth read fails the preflight as before. The full snapshot's sections must
equal the settled read. Recovery revalidates the journaled reads with the same
rules and keeps its single-shot equality against the settled observation. The
preflight time cap rises from 120 to 240 seconds to hold the wait. This amends
the preflight cap in the 2026-11-01 retained-fixture design; that design's
transient-lag refusal keeps governing the cleanup comparison.

Settling performs no write and no effect retry. Cleanup is unchanged: its
immediate post-delete equality still fails on any counter lag.

## Consequences

- Every prepare preflight costs at least one extra REST read and a 10-second wait,
  including one taken before a first preparation.
- The 12 x 10 s bound is a judgment, not a measurement. #1858's release outlasted a
  whole preflight and was gone minutes later. A lag longer than about 110 seconds
  still fails, and the operator then runs a later read-only witness, as #1858 did.
- A plateau of 10 seconds mid-release reads as settled. Against the pre-fixture
  digest the witness binds, it then fails as "supplied baseline differs", safely.
- The live record reports how many settling reads ran; two equal reads mean no
  lag was observed and the settling path was not exercised.
- A preflight journal written before this change has no settling record. Recovery
  refuses it, so such a preflight is re-run, not recovered.

## Considered & rejected

- **Classify converging counters as a named transient outcome.** judgment: each
  read stays single-shot, but recovery must then judge a direction from one
  observation against a baseline it did not settle, and the operator still owes a
  second witness every time.
- **Settle inside recovery instead of preflight.** judgment: the READY baseline
  would already carry the moving values, so a settled recovery could only report
  a difference, never UNCHANGED.
- **Stop only when the read reproduces the bound pre-fixture baseline.** judgment:
  the counters alone cannot be compared with a whole-snapshot digest without a
  full snapshot per read, which the REST cap cannot hold.
- **Do nothing; keep re-running the witness by hand.** judgment: the witness
  keeps demanding manual attention for a state that clears itself.
- **Relax the cleanup counter-lag rule.** verified: the operator excluded it for
  #1936 (the issue's frozen `WORK:SCOPE` exclusions, 2026-11-12); cleanup's own
  equality is the delete's proof.
