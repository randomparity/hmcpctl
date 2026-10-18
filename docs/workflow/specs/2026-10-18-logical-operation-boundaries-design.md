# Logical operation dependency ownership

## Problem
The durable store imports authorization.connection_scope, transitively requiring
optional FastMCP. Its record models live in the LPAR sibling despite serving the
logical engine, persistence and status API. Both moves were approved by the operator.

## Scope
Keep shared connection-token normalization in config.py as
resolve_connection_profile(token: object) -> str | None, returning a profile key,
None for default/environment, and UNRESOLVED_CONNECTION (empty string) for unknown
or invalid tokens. Preserve existing rule ordering, nickname/profile precedence,
case-insensitive HMC_HOST behavior, and ConfigError on unreadable configuration.
selected_connection remains the authorization adapter: it delegates normalization
and translates ConfigError to the existing ConnectionScopeError template.
The store uses config directly and reports unreadable selection as a sanitized
ValueError with the configuration exception chained, without exposing its path.

Move EffectStatus, OperationState, OperationOutcome, EffectRecord, OperationEvent,
OperationRecord and OperationPage unchanged to operations/logical/models.py.
Leave WorkflowStep in operations/lpar/workflow_contract.py. Migrate imports in
engine, store, LPAR power, server adapters and tests. Add no compatibility exports.
Keep record fields, metadata, serialization, database schema and MCP schemas intact.
The stable six-name facade remains unchanged. No new dependencies or live writes.

## Success
Store and engine import with fastmcp, mcp, typer and rich blocked in a subprocess.
Connection authorization tests preserve their expected selections and sanitized errors.
Logical consumers import models from their owner, and persisted records still roundtrip.

## Failure model
- Actors/deployments: base-library clients, MCP app, CLI, local SQLite persistence;
  Python 3.11–3.14; local arm64 proof is separate from hosted amd64 CI.
- Invariants/assets: connection scope cannot widen; records and permission enforcement
  retain semantics; persisted data must remain readable.
- Accepted failures: old pre-release model imports break under explicitly approved
  replacement; no compatibility reexport is retained.
- Guardrails: blocked-import subprocess red/green, existing connection/store/engine and
  power tests, record schema comparison, typecheck, tool-docs-check and smoke.

## Validation
Focused-test: test_connection_label_sanitizes_unreadable_configuration asserts the
store's ValueError excludes the config path and retains ConfigError as its cause.
tests/unit/test_operations_imports.py imports engine/store with optional
app packages blocked; currently fails on fastmcp. Existing config and connection-scope
suites prove resolution and error translation. Existing logical store/engine tests prove
roundtrips and record behavior; compare dataclass field metadata before/after relocation.
Run focused pytest --no-cov, just typecheck, just tool-docs-check and just smoke.
