# ADR 0345: Live failure session redaction

## Status
Proposed for #1896.

## Context
Raw failure bodies can bypass upstream exception sanitization; #1896 requires
an independent runner output boundary.

## Decision
Extend existing failure redaction with lexical XML-region and header removal,
before generic path handling. Respect CDATA, comments, nesting and self-closing
tags; unclosed sensitive regions consume through EOF. Collect identifiable named
values once per failure object, then replace their exact echoes across that object's
nested values before generic redaction. Keep callers/schema intact; no cross-row registry.

## Consequences
Diagnostic text inside a removed region is lost conservatively; siblings retain
existing behavior. No dependency or public interface is added.

## Considered & rejected
- **Upstream only.** verified: #1896 explicitly requires an independent boundary;
  `RunState.record` accepts direct FAIL data including raw-write error bodies.
- **Drop whole bodies.** judgment: unnecessary loss of useful diagnostic context.
- **XML document parser.** judgment: arbitrary diagnostic fragments and truncated
  bodies would still require another fallback; a lexical pass stays at one owner.
