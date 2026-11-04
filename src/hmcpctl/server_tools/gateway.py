"""Tool search and invocation over the served registry (ADR 0189 Decision 3).

``hmc_search_tools`` and ``hmc_invoke_tool`` read the application they are
registered on, so they see every tool the access policy's ceiling admitted and
the capabilities this server enabled — a superset of ``tools/list``, which carries
only the primary catalog (ADR 0330). Neither keeps a catalog of its own.

Invocation runs the named tool's own registered ``Tool``: FastMCP validates the
arguments against that tool's schema, then calls the ``authorized()`` wrapper,
which applies the connection and target scope and writes the ADR 0040 record
under the invoked tool's name before the handler runs. Guessing a name therefore
reaches nothing a direct ``tools/call`` would not.

This module must not import ``server``: ``server`` imports it, and the tool index
arrives as a parameter for that reason, as it does for ``permissions``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from ..operation_maturity import operation_maturity_meta
from ..tool_registry import (
    Authorize,
    ToolSecurity,
    annotations_for,
    authorized,
    validate_security,
)

SEARCH_TOOL_NAME = "hmc_search_tools"
INVOKE_TOOL_NAME = "hmc_invoke_tool"
GATEWAY_TOOL_NAMES = frozenset({SEARCH_TOOL_NAME, INVOKE_TOOL_NAME})

MAX_QUERY_CHARACTERS = 200
MAX_SEARCH_RESULTS = 20
MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_SUMMARY_CHARACTERS = 200

SEARCH_SECURITY = ToolSecurity(
    effect="read",
    operation="tools.search",
    target_kind="none",
    connection_argument=None,
)
# `destructive` because it reaches every registered tool of that class, so a
# client's approval prompt is never milder than the tool it ends up running.
INVOKE_SECURITY = ToolSecurity(
    effect="destructive",
    operation="tools.invoke",
    target_kind="none",
    connection_argument=None,
)

# One message for an unknown, policy-withheld and disabled name, so the reply
# cannot be used to probe what the policy hides.
_UNAVAILABLE = (
    "tool_unavailable: no tool by that name is available on this server; "
    f"call {SEARCH_TOOL_NAME} to find one"
)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class ToolEntry:
    """One tool the server exposes, as search reports it."""

    name: str
    summary: str
    effect: str
    maturity: dict[str, str]
    input_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolSearchResult:
    """At most ``limit`` matching tools; ``truncated`` when more matched."""

    tools: list[ToolEntry]
    limit: int
    truncated: bool


@dataclass(frozen=True)
class ToolInvocation:
    """The invoked tool's name and its own result."""

    name: str
    result: Any


def _summary(tool: Tool) -> str:
    first = (tool.description or "").strip().split("\n", 1)[0].strip()
    return first[:_MAX_SUMMARY_CHARACTERS]


def _entry(
    tool: Tool, security: ToolSecurity, *, with_schema: bool = False
) -> ToolEntry:
    return ToolEntry(
        name=tool.name,
        summary=_summary(tool),
        effect=security.effect,
        maturity=operation_maturity_meta(security.operation),
        input_schema=dict(tool.parameters) if with_schema else None,
    )


def _score(words: frozenset[str], tool: Tool) -> int:
    """Name words count double: they are the tool's most specific vocabulary."""
    name_words = set(_WORD.findall(tool.name.lower()))
    description_words = set(_WORD.findall((tool.description or "").lower()))
    return sum(2 * (word in name_words) + (word in description_words) for word in words)


def _query_words(query: str) -> frozenset[str]:
    if len(query) > MAX_QUERY_CHARACTERS:
        raise ValueError(
            f"invalid_query: query is limited to {MAX_QUERY_CHARACTERS} characters"
        )
    words = frozenset(_WORD.findall(query.lower())) - {"hmc"}
    if not words:
        raise ValueError(
            "invalid_query: query needs at least one letter or digit word besides 'hmc'"
        )
    return words


def _check_search_arguments(query: str | None, name: str | None, limit: int) -> None:
    if (query is None) == (name is None):
        raise ValueError("invalid_arguments: pass exactly one of query or name")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"invalid_limit: limit must be 1 to {MAX_SEARCH_RESULTS}")


def _argument_size(arguments: Mapping[str, Any]) -> int:
    return len(
        json.dumps(arguments, separators=(",", ":"), ensure_ascii=False).encode()
    )


def _unwrapped(result: ToolResult) -> Any:
    """The invoked tool's result as a direct ``tools/call`` client would read it."""
    if result.structured_content is not None:
        if (result.meta or {}).get("fastmcp", {}).get("wrap_result"):
            return result.structured_content["result"]
        return result.structured_content
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if not texts:
        return None
    return texts[0] if len(texts) == 1 else texts


def gateway_handlers(
    mcp: FastMCP, tool_security: Mapping[str, ToolSecurity]
) -> dict[str, tuple[Callable[..., Any], ToolSecurity]]:
    """The unwrapped search and invoke handlers bound to *mcp*, with their records.

    A factory because each handler reads the application it serves; the capability
    inventory calls it to read their signatures without composing a server.
    """

    async def hmc_search_tools(
        query: str | None = None,
        name: str | None = None,
        limit: int = 10,
    ) -> ToolSearchResult:
        """Find tools this server exposes by intent, or describe one tool by exact name.

        Searches every tool this server's access policy and enabled capabilities
        expose, including the specialists ``tools/list`` does not carry. A ``query``
        matches words in tool names and descriptions and returns the best matches,
        name first, without input schemas. An exact ``name`` returns that one tool with its full input schema, or
        no tool when this server does not expose it. Each entry carries the tool's
        one-line summary, effect class and operation maturity. Call a found tool
        directly, or through ``hmc_invoke_tool``.

        Args:
            query: Words describing what you want to do, up to 200 characters. Pass
                this or ``name``, not both.
            name: An exact tool name whose full input schema to return.
            limit: Most tools to return, 1 to 20.
        """
        _check_search_arguments(query, name, limit)
        if name is not None:
            tool = await mcp.get_tool(name)
            security = tool_security.get(name)
            found = tool is not None and security is not None
            tools = [_entry(tool, security, with_schema=True)] if found else []
            return ToolSearchResult(tools=tools, limit=limit, truncated=False)
        words = _query_words(query or "")
        exposed = await mcp.list_tools(run_middleware=False)
        scores = {
            tool.name: _score(words, tool)
            for tool in exposed
            if tool.name in tool_security
        }
        ranked = sorted(
            (tool for tool in exposed if scores.get(tool.name, 0) > 0),
            key=lambda tool: (-scores[tool.name], tool.name),
        )
        return ToolSearchResult(
            tools=[_entry(tool, tool_security[tool.name]) for tool in ranked[:limit]],
            limit=limit,
            truncated=len(ranked) > limit,
        )

    async def hmc_invoke_tool(
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolInvocation:
        """Call one tool this server exposes by name, exactly as a direct call would run it.

        The named tool's own argument validation, access-policy connection and target
        checks, ownership guards and authorization audit record all apply, so this
        reaches nothing a direct call to that tool could not. A name this server does
        not expose, whether unknown, withheld by policy or disabled, is refused with
        one message. ``hmc_search_tools``, ``hmc_invoke_tool`` and the arbitrary-command
        tool cannot be invoked this way. Returns ``{name, result}``, where ``result`` is
        the invoked tool's own result.

        Args:
            name: The exact tool name, as ``hmc_search_tools`` reports it.
            arguments: The tool's arguments as one JSON object, at most 64 KiB encoded.
        """
        arguments = {} if arguments is None else arguments
        if _argument_size(arguments) > MAX_ARGUMENT_BYTES:
            raise ValueError(
                f"arguments_too_large: arguments are limited to {MAX_ARGUMENT_BYTES} "
                "bytes of JSON"
            )
        if name in GATEWAY_TOOL_NAMES:
            raise ValueError(
                "gateway_recursion: the search and invoke tools cannot be invoked "
                f"through {INVOKE_TOOL_NAME}"
            )
        tool = await mcp.get_tool(name)
        security = tool_security.get(name)
        if tool is None or security is None:
            raise ValueError(_UNAVAILABLE)
        if security.effect == "arbitrary-command":
            raise ValueError(
                "arbitrary_command_refused: arbitrary-command tools cannot be invoked "
                f"through {INVOKE_TOOL_NAME}; call them directly"
            )
        return ToolInvocation(name=name, result=_unwrapped(await tool.run(arguments)))

    return {
        SEARCH_TOOL_NAME: (hmc_search_tools, SEARCH_SECURITY),
        INVOKE_TOOL_NAME: (hmc_invoke_tool, INVOKE_SECURITY),
    }


def register_gateway_tools(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register search and invoke on *mcp*, each only where the ceiling admits it."""
    for name, (handler, security) in gateway_handlers(mcp, tool_security).items():
        if not permits(name):
            continue
        validate_security(security, handler)
        mcp.tool(
            authorized(name, security, handler, authorize),
            annotations=annotations_for(security.effect),
        )
