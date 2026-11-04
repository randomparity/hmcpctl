"""The MCP ``tools/list`` projection: the listed catalog and its maturity and tier metadata."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

import mcp_types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

from hmcpctl.operation_maturity import operation_maturity_meta
from hmcpctl.tool_registry import ToolSecurity

MATURITY_META_KEY = "io.github.randomparity.hmcpctl/operation-maturity"
# ADR 0189 Decision 4: "primary" or "secondary". A primary-catalog listing (ADR 0330)
# carries only "primary"; a registered listing carries both.
TIER_META_KEY = "io.github.randomparity.hmcpctl/catalog-tier"


class OperationMaturityMiddleware(Middleware):
    """Shape what ``tools/list`` returns: which tools, and their maturity and tier metadata.

    With *list_only_primary*, a tool outside *primary_tools* is left out of the listing
    and nothing else: it stays registered, so ``tools/call`` by name and
    ``hmc_invoke_tool`` still reach it under its own guards (ADR 0189 D4, ADR 0330).
    """

    def __init__(
        self,
        tool_security: Mapping[str, ToolSecurity],
        *,
        primary_tools: Collection[str] = frozenset(),
        list_only_primary: bool = False,
    ) -> None:
        self._tool_security = tool_security
        self._primary_tools = primary_tools
        self._list_only_primary = list_only_primary

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        projected: list[Tool] = []
        for tool in tools:
            if self._list_only_primary and tool.name not in self._primary_tools:
                continue
            security = self._tool_security.get(tool.name)
            if security is None:
                projected.append(tool)
                continue
            metadata = dict(tool.meta or {})
            metadata[MATURITY_META_KEY] = operation_maturity_meta(security.operation)
            metadata[TIER_META_KEY] = (
                "primary" if tool.name in self._primary_tools else "secondary"
            )
            projected.append(tool.model_copy(update={"meta": metadata}))
        return projected
