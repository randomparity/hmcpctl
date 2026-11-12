# ADR 0358: Separate recovery entry responsibilities

## Status

Accepted, 2026-11-11, within the authorized behavior-preserving code-quality pilot.

## Context

The recovery entry script mixes generic witnessing, delegation to standalone
procedure owners, and formatting. The existing `live_test.recovery` package
already separates domain witnesses. The entry is 767 lines at `4e12a273` and
carries a structural finding; its recovery/preflight baseline passes 610 tests.

## Decision

Keep the parser, generic witness orchestration, read-only tool gate, and main
entry at `scripts/live_test_recovery.py`. Move the five existing standalone
procedure helpers into `live_test.recovery.procedures`, and report/partial helpers
into `live_test.recovery.reporting`. Consumers use the concrete owners directly.
Preserve behavior, limits, labels, exit codes, and arm-owned authority checks.

## Consequences

The entry composes smaller owners without compatibility forwarding helpers.
Tests patch the implementation owner when delegation constructs HMCConfig.
No new generic reader, recovery framework, or permission policy is introduced.
Compare actual structural findings before extending this pilot.

## Considered & rejected

- Keep the monolith. judgment: responsibility boundaries remain harder to locate.
- Merge the two raw readers. judgment: unnecessary policy consolidation risks
  conflating their existing diagnostics and admission details.
- Split generic witnessing as well. judgment: additional migration without a
  demonstrated need for this bounded pilot.
