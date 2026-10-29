# 0308 — Definition-owned tool schema templates

## Status

Proposed (2026-10-29), for PR #1823. Implementation awaits design adoption.

## Context

`ToolDefinition` already owns a handler and frozen `ToolSecurity`; application
composition filters definitions and creates fresh dispatch authorization wrappers.
Each wrapper also causes FastMCP to regenerate schemas. A private, covered
Python 3.12 prototype at `43a6606f` reduced warm constructor medians from 425 ms
to 57 ms while preserving 200 advertised tool records. This is constructor
evidence, not a whole-suite multiplier or a diagnosis of the earlier timeout.

The operator approved preparing a bounded schema-reuse design in this PR,
preserving fresh authorization wrappers, schema equality and cache isolation.

## Decision

Give each collected `ToolDefinition` one lazy private `FunctionTool` template.
Prepare it from its original handler and derived annotations, without an
application, policy, session or dispatch authorizer. After `permits(name)` passes,
deep-copy that template, replace its callable with a fresh `authorized(...)`
wrapper, and register the copy through the existing `FastMCP.add_tool` API.
The template itself is never registered or returned to a client.

The definition is the lifetime and invalidation boundary. A new collection or
process constructs new definitions and templates, including when the handler
name is reused. Published deployments use their source-defined catalog for the
process lifetime; live edits to decorated functions/models are not a supported
cache-refresh mechanism. No hot-reload or invalidation API is introduced.

Use `functools.cached_property`. Failed preparation is not cached; concurrent
cold misses may prepare equivalent transient values, with one value retained
per definition. Each binding gets independent mutable schemas and annotations.

## Consequences

- The retained cache is bounded by the number of live collected definitions,
  rather than request count or policy count; discarded definitions are collectable.
- Schema preparation remains lazy after the ceiling check. Dispatch still binds
  arguments and authorizes against the current application's callback.
- Composed special tools keep their existing registration paths. The six-name
  stable facade, MCP schemas, dependencies and CI limits do not change.
- Fixed-definition lifetime needs explicit adoption before implementation;
  signature/model hot-patching would require a separate lifecycle design.

## Considered & rejected

- **Global cache keyed by name or callable.** judgment: definition ownership
  avoids collisions between collections and couples lifetime to an existing owner.
- **Share applications or bound tools.** judgment: these carry policy or mutable
  state; metadata reuse can preserve the existing independent application model.
- **Split files first or retain the current factory.** verified: diagnostic run
  [40952334953](https://github.com/randomparity/hmcpctl/actions/runs/40952334953)
  recorded balanced two-worker test-phase totals (1,084/1,089 seconds on 3.12).
  Retaining repeated construction leaves the measured constructor cost in place;
  file organization can be addressed independently.
