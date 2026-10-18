# Consistent LPAR operation selector order

## Problem
LPAR reads and activation take the partition first, but shared power and migration
operations take a system first. Both selectors are strings, allowing accidental swaps.

## Scope
The operator approved target-first pre-release operation APIs on 2026-10-18.
Change power_lpar, validate_lpar_migration, migrate_lpar,
migrate_lpar_with_affinity_preflight, abort_lpar_migration and recover_lpar_migration.
Move system_name_or_uuid after the keyword-only marker, defaulting to None.
Keep the relative order of other parameters. Update repository direct and aliased
calls, callback invocations, mock expectations and examples that exercise these APIs.
The owning modules remain operations/lpar/core.py and migration.py. Remove the old
positional form without aliases; the six-name stable facade does not export these APIs.
Keep operation bodies, external CLI/MCP schemas and transport contracts unchanged.

## Success
The six named functions use hmc then lpar_name_or_uuid as positional selectors.
Their repository callers supply system_name_or_uuid by keyword when needed.
Scoped and unscoped power, ownership, migration validation, affinity preflight,
abort and recovery keep their existing execution and failure behavior.

## Failure model
- Actors and deployments: library callers, CLI and MCP adapters, offline tests on
  Python 3.11–3.14; local verification uses arm64 Python 3.11.
- Invariants and assets: correct target and system scope, authorization, no accidental
  power or migration submission; existing behavior tests protect these.
- Accepted failures: old positional calls outside this pre-release repository break;
  operator explicitly approved replacement, with no compatibility alias.
- Guardrails: AST inventory for named calls and aliases, focused power/migration and
  ownership tests, typecheck, lint, formatting, final verify and pinned hooks.

## Validation
Mode: focused-test. Contract: six function signatures bind target positionally and
system only by keyword, default None. Parameterized signature test rejects old
ordering before the change and passes after it.
Mode: focused-test. Contract: adapter arguments preserve target/scope, validation and
ownership. Run pytest tests/lpar tests/unit/test_destructive_scope.py --no-cov -q;
existing call/assertion failures expose incomplete migration. Run just typecheck,
just lint and just format-check; final branch verification remains required.
