# ADR 0356: Processor operation ownership

## Status
Accepted

## Context
Current processor changes and two-endpoint moves share private snapshot policy;
memory already presents current and move owners together in one resource package.

## Decision
Group processor workflows under operations/lpar/processors/current.py and move.py.
Keep a minimal package initializer, direct caller imports and no compatibility
exports. Native grammar stays in SSH; observation and ownership keep their owners.
Update ADR0092's maintained ownership inventory to the current guarded symbol.

## Consequences
Internal module consumers migrate together. Operation behavior and public
CLI/MCP contracts stay unchanged; historical implementation specs describe history.

## Considered & rejected
- Keep two flat owners. judgment: resource navigation remains inconsistent.
- Re-export old paths. judgment: adds an unnecessary internal compatibility path.
- Merge current and move bodies. judgment: obscures their distinct single- and
  two-endpoint orchestration responsibilities.
