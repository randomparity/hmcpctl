"""Smoke-test the MCP server over stdio using the official client protocol.

Composes through the same generator an operator's migration uses, so this leg also
proves on every ``just verify`` and every CI run that the legacy-equivalent grant
compiles and composes. It does not serialize TOML, so a *rendering* defect is not
caught here — ``tests/unit/test_legacy_policy.py`` owns that round trip.
"""

import argparse
import asyncio

from fastmcp import Client

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp


async def _run_smoke(verbose: bool) -> None:
    # Composed here rather than at module scope: since ADR 0041 nothing in the package
    # holds an application at import, and this script is inside the guard that checks it.
    mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))
    async with Client(mcp) as client:
        tools = await client.list_tools()
        suffix = ":" if verbose else "."
        print(f"Connected. {len(tools)} tools exposed{suffix}")
        if verbose:
            for tool in tools:
                print(f"  - {tool.name}")


def main(args: list[str] | None = None) -> None:
    """Perform an MCP protocol handshake and report the exposed tools."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list the tools tools/list carries (the primary catalog) after the handshake",
    )
    parsed_args = parser.parse_args(args)
    asyncio.run(_run_smoke(parsed_args.verbose))


if __name__ == "__main__":
    main()
