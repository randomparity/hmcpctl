# Processor operation package

## Problem
Current and two-endpoint processor workflows share private snapshots but sit as
unrelated files in the mixed LPAR directory, unlike the existing memory package.

## Scope
Move operations/lpar/processors.py to processors/current.py and processor_move.py
to processors/move.py. Add a docstring-only processors/__init__.py with no exports.
Retain complete executable bodies; fix moved ownership relative imports and bind
move's existing processors alias directly to current. No forwarding module.
Migrate the35 existing direct import nodes in seven files: the moved move module,
server_tools/lpar/lifecycle.py, cli_commands/lpar/processors.py,
scripts/live_test/arms/processor_move.py, tests/lpar/test_processor_move.py,
tests/lpar/test_processor_quantities.py and tests/test_live_processor_move_arm.py.
Also migrate the embedded subprocess import in test_processor_quantities.py and
the two dynamic module parameter lists in tests/unit/test_operations_imports.py,
the current ADR0092 ownership inventory row and its two path fixtures, and the
current reconfiguration/core.py ownership-reference docstring. Historical October
specs remain historical; add [ADR 0356](../../adr/0356-processor-operation-ownership.md).
Existing public MCP/CLI signatures, values, exceptions, authorization, two-endpoint
read/mutation/reconciliation order and import dependency closures stay unchanged.
Do not change memory or SSH owners, quantities semantics, bounds or witnesses.

### Failure model
- Actors and deployments: package users and local operators through existing
  CLI/MCP operations; offline CI and live-arm importers.
- Invariants and assets: guarded native mutations and complete independent
  outcomes, and application-free operation imports.
- Accepted failure classes: old pre-release internal module paths are replaced,
  per repository replace-by-default policy; no new hardware evidence is claimed.
- Covered elsewhere: existing processor admission, ownership, TLS handling and
  bounded native SSH grammar remain their unchanged owners.

## Success
The two workflows share a processor package, and the listed direct/dynamic
consumers use its actual owners without compatibility aliases or public changes.
ADR0092's maintained inventory continues to resolve its current guarded symbol.

## Validation
- task-test-not-applicable: raw relocation introduces no new behavioral or
  structural contract; existing semantic and import-boundary tests are retained.
- Audit moved bodies and complete consumers by AST/raw source, normalizing only
  enumerated imports, dynamic module tokens and maintained path references.
- Run processor quantities/move, offline arm, presentation and import-boundary
  tests with --no-cov plus ADR0092 citation tests. Retain assertions; update actual
  patch targets if their owner changed. Compare actual207MCP models/metadata and
  fresh-process import closures, normalizing only the two owner moves and added
  docstring-only parent. Regenerate docs only through their configured recipe.
- Run relevant static/hooks, then final root full verification and whole-base
  review. No live hardware or cross-version CI claim follows from offline tests.
