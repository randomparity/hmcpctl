"""Generate the declared live scenario definitions from the capability ledger.

Usage:
    python scripts/gen_scenario_definitions.py                 # write the registry
    python scripts/gen_scenario_definitions.py --output PATH   # write elsewhere

Derives one ADR 0247 definition for each ``docs/capabilities/operations.json``
operation that needs nothing but its targets (ADR 0249), writes the registry
``scripts/live_test/definitions.py`` loads, and prints every other operation as
needing a hand-written scenario. Offline: the served input schemas come from the
live runner's own composition, no HMC credential is needed, and nothing is run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from live_test import definitions, runner

from hmcpctl.server import TOOL_SECURITY
from hmcpctl.tool_registry import ToolSecurity

_OPERATIONS = (
    Path(__file__).resolve().parent.parent / "docs/capabilities/operations.json"
)
# A mutating definition may leave this unset: its default refuses a foreign-owned
# partition. Every other optional argument of a mutating tool changes what the call
# does, so a defaulted call may not act (ADR 0249).
_SAFE_DEFAULT = "ownership_override"


def derive(
    operation: str,
    tool: str,
    security: ToolSecurity | None,
    schema: Mapping[str, Any] | None,
) -> dict[str, Any] | str:
    """The definition of *operation*, or why it needs a hand-written scenario."""
    if security is None or schema is None:
        return f"tool {tool} is not registered"
    selectors = {s.argument: s.kind for s in security.targets if s.container is None}
    containers = {s.container for s in security.targets if s.container is not None}
    required = sorted(schema.get("required") or ())
    nested = sorted(containers.intersection(required))
    if nested:
        return f"required argument {nested[0]} is a nested selector's container"
    for name in required:
        if name not in selectors:
            return f"required argument {name} is not a target selector"
    read_only = security.effect == "read"
    if not read_only:
        allowed = selectors.keys() | {security.connection_argument, _SAFE_DEFAULT}
        others = sorted(set(schema.get("properties") or ()) - allowed)
        if others:
            return (
                f"mutating with optional {', '.join(others)} "
                "a definition would leave at its default"
            )
    return {
        "scenario": definitions.scenario_id(operation),
        "operation": operation,
        "class": "read-only" if read_only else "mutating",
        "arguments": {name: {"target": selectors[name]} for name in required},
        "expected": "success",
    }


def generate(
    operations: Sequence[Mapping[str, Any]],
    security: Mapping[str, ToolSecurity],
    schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """The registry document and one ``hand-written:`` line per underived operation."""
    derived: list[dict[str, Any]] = []
    hand_written: list[str] = []
    for entry in sorted(operations, key=lambda entry: entry["operation"]):
        operation, tool = entry["operation"], entry["tool"]
        result = derive(operation, tool, security.get(tool), schemas.get(tool))
        if isinstance(result, str):
            hand_written.append(f"hand-written: {operation}: {result}")
        else:
            derived.append(result)
    document = {"format_version": definitions.FORMAT_VERSION, "definitions": derived}
    definitions.load_definitions(document, security)
    return document, hand_written


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def ledger_operations() -> list[dict[str, Any]]:
    return json.loads(_OPERATIONS.read_text(encoding="utf-8"))["operations"]


def served_schemas() -> dict[str, dict[str, Any]]:
    """The input schema each tool serves, through the live runner's own composition."""

    async def read() -> dict[str, dict[str, Any]]:
        async with runner.served_client() as client:
            return await runner.served_schemas(client)

    return asyncio.run(read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=definitions.DEFINITIONS_PATH,
        help=f"registry to write (default: {definitions.DEFINITIONS_PATH})",
    )
    arguments = parser.parse_args(argv)
    document, hand_written = generate(
        ledger_operations(), TOOL_SECURITY, served_schemas()
    )
    arguments.output.write_text(render(document), encoding="utf-8")
    entries = document["definitions"]
    mutating = sum(entry["class"] == "mutating" for entry in entries)
    print("\n".join(hand_written))
    print(
        f"summary: {len(entries)} definitions ({len(entries) - mutating} read-only, "
        f"{mutating} mutating); {len(hand_written)} operations need a hand-written "
        "scenario"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
