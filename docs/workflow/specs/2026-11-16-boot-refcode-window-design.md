# Adaptive reference-code reads for provision boot waits

## Problem and authority

Issue #1911 requires detecting and widening a missing-baseline polling window,
with a regression exceeding 20 arrivals. WORK:SCOPE token q1911-b3ca2368 freezes
empty exclusions and the existing private boot-wait/caller/test surface. Root's
implementation decision selects bounded 20-to-100 widening and an explicit
`needs_attention` result when full history cannot establish coverage.

## Architecture and behavior

Keep policy in `provisioning/boot.py`; its sole production caller in `operation.py`
passes a count-taking read callable through to the existing SSH adapter. No public
interface, persisted identity, state, or compatibility path is added. The baseline,
HMC row ordering/timestamp filter, native boot-code set, and power-on stay unchanged.

Read 20 rows per poll. A saturated window (at least the requested count) lacking
the baseline identity triggers a fresh read of `MAX_REFCODE_COUNT` (100). A null
baseline is also absent, so saturated initial history receives the same treatment.
Search the resulting newer rows for boot evidence first. If none qualifies and the
maximum window is saturated without the baseline, end with an explicit incomplete
history reason, the observed rows, and existing `needs_attention` semantics.
Otherwise use the existing polling deadline and no-boot timeout. The additional read
is part of one poll, including a zero-second wait's mandatory poll.

A shorter successful result represents the history the HMC currently makes available:
`lsrefcode -n` returns the available rows when fewer exist. The strict parser retains
nonblank rows or rejects malformed records; the adapter enriches without filtering.
This proves no completeness beyond the HMC's available history. Neither the reference
nor captured evidence establishes a worst-case emission rate or retention guarantee.

## Success and validation

- A code beyond row20 but within the widened100 reaches `boot_started`, including
  a same-second identity comparison and a null baseline.
- A full100 without baseline or boot evidence reports incomplete history; evidence
  in that same full100 takes precedence over the diagnostic.
- A baseline at row20 prevents widening; a short/empty history remains bounded and
  follows existing timeout semantics. Older boot codes remain excluded.
- Normal and widened read failures preserve the existing bounded read-error result.
- Focused boot/SSH/application tests, mutation proof, `just verify`, and pinned prek
  checks pass. No live HMC is required or authorized.

## Failure model

- **Actors and deployments:** authenticated provision operators through MCP/app paths;
  offline CI fixtures; existing HMC SSH refcode reads under the operation's writer window.
- **Invariants and assets at stake:** no false boot claim from stale evidence; no silent
  no-boot conclusion after a demonstrably saturated missing-baseline maximum window;
  preserve recorded power-on and baseline identities, existing shared100-row bound.
- **Accepted failure classes:** unavailable/rotated HMC history may prevent reconstructing
  past boot evidence; explicitly report the detectable saturated gap rather than promise
  recovery. An absent baseline in a short/empty read cannot prove HMC retention; bounded
  existing no-boot semantics remain, because no omitted available row is demonstrated.
  Reads may outlast the call deadline, as in ADR0326; the additional read is bounded100.
- **Covered elsewhere:** SSH selector validation/strict parsing and100-row bound belong
  to `ssh/refcodes.py`; authorization and power-on replay belong to existing provision
  effects and ADR0326. Native boot-code evidence remains ADR0328's responsibility.

## Decisions and constraints

[ADR0388](../../adr/0388-adaptive-boot-refcode-window.md) records the read policy.
Python >=3.11; existing locked dependencies; no architecture target declared.
No new dependency, live call, external state, or configuration is needed.
