# Tool schema reuse design

Proposed 2026-10-29 for PR #1823, `fix/checkpoint-json-serialization`.
Authority: the operator approved step 1, preparing the bounded design; production
implementation, publishing changes and merging are later steps.
Decision: [ADR 0308](../../adr/0308-definition-owned-tool-schema-templates.md).

## Problem

Fresh MCP applications regenerate input/output schemas for collected tools.
The current authorization wrapper must stay application-specific, but schema
metadata does not depend on that callback. A fixed-source covered 3.12 prototype
reduced warm construction from 425 to 57 ms. It is not shipping verification.
The latest logged full suites passed in 19m32s (3.12) and 19m26s (3.13), with
24,523 matching identities and balanced workers; the earlier timeout's exact
cause remains unestablished.

## Scope and ownership

- Extend `ToolDefinition` in `src/hmcpctl/tool_registry.py`, rather than add a
  process-global cache. Its frozen handler/security fields remain authoritative.
- `_schema_template` is a lazy private `FunctionTool`; `_bind(authorize)` returns
  a deep copy with a newly constructed `authorized` callable. Neither method is
  exported from the stable facade.
- `_ToolModuleBuilder.register_tools` keeps its signature and ceiling check,
  then calls `mcp.add_tool(definition._bind(authorize))` for permitted definitions.
- The static source-defined catalog is the named deployment. New definitions
  after a new collection/process get fresh templates. Old applications remain
  independent. There is no runtime handler/model hot-reload mechanism to support.
- Composed special tools, permission evaluation, client sessions, HMC operations,
  test layout, workers, timeouts and coverage retain their existing owners.
  The earlier checkpoint-encoding fix remains in this PR.
- There are no deferred or excluded criteria inside this design's scope.

## Global Constraints

Python >=3.11; CI Python 3.11–3.14 on ubuntu-24.04 amd64; local arm64 is separate
evidence. Keep fastmcp-slim[server]==4.0.10 and pydantic==2.14.1 as currently locked;
add no dependency or version floor. Preserve branch coverage and the exact 90.5%
gate, existing two-worker admission/ownership policy, 25-minute test deadline and
30-minute CI job deadline. Use sibling worktrees and `uv run --no-sync`.

## Construction and lifetime

1. Decoration still validates security and collects a frozen definition.
2. Registration evaluates `permits(name)` before preparing or copying a template.
3. A successful cold preparation calls the existing `Tool.from_function` with
   the original handler and `annotations_for(effect)`. It stores no policy state.
4. Binding deep-copies the `FunctionTool`, including nested schema/annotation
   containers, and replaces `fn` with a fresh `authorized` wrapper. Its authorizer
   remains the current caller's callback and is evaluated on each invocation.
5. Register only the bound copy. The original Python handler is unchanged.
6. `cached_property` retains at most one template per accessed live definition.
   Duplicate concurrent cold preparations are permitted; published copies remain
   separate. No single-flight, eager compilation or mutable global index is needed.
7. A preparation failure propagates and stores no failed value. A copy failure
   propagates before registering that definition, retaining its valid private template. Neither
   failure invokes a handler or installs a raw fallback.
8. Invalidation is replacement: a new definition/collection/process prepares new
   metadata. Post-decoration mutation of Python definitions is outside this
   static-catalog proposal and needs an explicit future lifecycle contract.

## Success

- S1: Sequential registrations of one definition prepare its template once after
  the first successful preparation; a distinct definition prepares independently.
- S2: Permitted and withheld tools, per-call connection/target authorization,
  synchronous/asynchronous execution and invalid-argument refusal retain their
  existing semantics in the collected registry and named authorization suites.
- S3: Advertised tool records equal an uncached reference at the same candidate,
  for legacy, narrow and empty policies, including the opt-in arbitrary-command
  tool. Mutable tool/schema instances do not cross application boundaries.
- S4: Failed preparation/copy cannot register a raw handler. Replaced/discarded
  definitions and concurrent cold bindings obey the lifetime rules above.
- S5: Record constructor and affected/full-suite timing on the actual candidate,
  preserve old test identities, and require normal final verification/hooks/CI.
  No constructor multiplier is promised for the whole suite or a cold process.

## Failure model

- **Actors and deployments:** trusted source modules define tools; local operators
  and MCP clients use independently composed static-catalog applications; offline
  tests/CI compose applications repeatedly, including concurrent cold preparation.
- **Invariants and assets:** capability ceiling, current dispatch authorizer,
  argument validation, immutable classification and advertised metadata; independent
  mutable application state; bounded retained template count.
- **Accepted classes:** duplicate equivalent cold preparation is bounded by callers
  already composing applications. Runtime function/model rewriting is outside the
  source-defined deployment, not a claimed refresh path. Memory exhaustion may
  fail construction; no fallback registration is permitted.
- **Covered elsewhere:** policy compilation/startup and connection/target decisions
  remain owned by ADRs 0035/0037/0038/0041 and their existing tests; native resource
  effects remain owned by existing operations. This change adds no hardware arm.

## Threat model

- **Boundaries:** existing source definition → FastMCP metadata; existing per-app
  policy → registered callable; existing client arguments → validation/authorization.
  New sharing is private schema metadata across applications, not a widened grant.
- **Actors:** MCP clients control tool names/arguments, not Python definitions or
  the private template. Deployment source and pinned dependencies are trusted.
- **Controls:** ceiling before cache access; deep-copy metadata; bind a fresh current
  authorizer; preserve SDK argument validation and the existing dispatch wrapper;
  propagate preparation/copy errors before registration without raw fallbacks.
- **Outside scope:** compromised interpreter/source or arbitrary same-process Python
  mutation can bypass existing policy code too; this proposal provides no sandbox.
  Existing native operation protections and public-log privacy rules remain in force.

## Validation and rollback

The local implementation plan inventories executable tests for S1–S4, complete
uncached/cached wire-record comparisons and existing authorization regressions.
Use controlled faults for recomputation, shared dictionaries and stale authorizers;
restore each fault before continuing. Run focused tests without package coverage,
then normal full verification, separate hooks and the four-leg CI matrix. Report
observed timing/resources/coverage and additions to the baseline test set.
No hardware access is required for metadata construction or local MCP protocol proof.
If generated reference checks fail, use their existing regenerators and inspect the
results; do not hand-edit schemas or historical live evidence to obtain equality.
Rollback is a separate revert of the schema-reuse implementation commit followed
by process restart. No persisted cache, migration or hardware cleanup exists.
