# ADR 0345: Live failure session redaction

## Status
Proposed for #1896.

## Context
Raw failure bodies bypass upstream sanitization; #1896 requires an independent boundary.

## Decision
Extend existing failure redaction with lexical XML-region and header removal,
before generic path handling. Respect CDATA, comments, nesting and self-closing
tags; unclosed sensitive regions consume through EOF. Collect identifiable named
values once per failure object, then replace their exact echoes across that object's
nested values before generic redaction. Bound input, nesting, collection and pattern
work; exhaustion replaces complete failure data with a fixed marker, retaining the
outer row envelope. Keep callers/schema intact; no cross-row registry.

## Consequences
Diagnostic text inside a removed region is lost conservatively; siblings retain
existing behavior within those bounds.

## Considered & rejected
- **Upstream only.** verified: `RunState.record` also accepts raw-write error bodies.
- **Drop whole bodies.** judgment: unnecessary for bounded input; resource
  exhaustion requires conservative loss.
- **XML document parser.** judgment: arbitrary diagnostic fragments and truncated
  bodies would still require another fallback; a lexical pass stays at one owner.
