"""Declared live scenario definitions: the ADR 0247 format, loaded and validated.

Nothing here executes a definition. The registry is data a generator writes and the
scenario gap report reads; it coexists with the runner's hand-written ``SUBTASKS``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hmcpctl.server import TOOL_SECURITY
from hmcpctl.tool_registry import ToolSecurity

from .observation import OPERATION_ID, SCENARIO_ID

DEFINITIONS_PATH = Path(__file__).with_name("scenario_definitions.json")
FORMAT_VERSION = 1
#: The namespace marker every definition's ``st<N>`` carries; never a result row.
SCENARIO_NUMBER = 100
_FIELDS = ("scenario", "operation", "class", "arguments", "expected")
_CLASSES = {"read-only": True, "mutating": False}
_EXPECTED = frozenset({"success"})


class DefinitionError(ValueError):
    """The registry, or one definition in it, breaks the ADR 0247 format."""


@dataclass(frozen=True)
class Placeholder:
    """A target identity a run binds; never a committed lab identity."""

    kind: str


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario: str
    operation: str
    tool: str
    read_only: bool
    arguments: Mapping[str, Any]
    expected: str


def scenario_id(operation: str) -> str:
    """The one scenario ID a definition of *operation* carries."""
    slug = operation.replace(".", "-").replace("_", "-")
    return f"st{SCENARIO_NUMBER}-{slug}"


def _placeholder(value: Any) -> Placeholder | None:
    if isinstance(value, dict) and set(value) == {"target"}:
        return Placeholder(value["target"])
    return None


def _arguments(raw: Any, security: ToolSecurity) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise DefinitionError("arguments must be an object")
    selectors = {s.argument: s.kind for s in security.targets if s.container is None}
    forbidden = {s.container for s in security.targets if s.container is not None}
    if security.connection_argument is not None:
        forbidden.add(security.connection_argument)
    arguments: dict[str, Any] = {}
    for name, value in raw.items():
        if name in forbidden:
            raise DefinitionError(f"argument {name} may not be supplied")
        placeholder = _placeholder(value)
        kind = selectors.get(name)
        if kind is None and placeholder is not None:
            raise DefinitionError(
                f"argument {name} is not a target selector and takes no placeholder"
            )
        if kind is not None and placeholder != Placeholder(kind):
            raise DefinitionError(
                f'argument {name} must be the placeholder {{"target": "{kind}"}}'
            )
        arguments[name] = value if placeholder is None else placeholder
    return MappingProxyType(arguments)


def _definition(
    raw: Any, tools: Mapping[str, tuple[str, ToolSecurity]]
) -> ScenarioDefinition:
    if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
        raise DefinitionError(
            f"must be an object with exactly the fields {', '.join(_FIELDS)}"
        )
    operation = raw["operation"]
    if not isinstance(operation, str) or not OPERATION_ID.fullmatch(operation):
        raise DefinitionError("operation is not an operation ID")
    if operation not in tools:
        raise DefinitionError(f"operation {operation} is not a registered tool's")
    tool, security = tools[operation]
    expected_id = scenario_id(operation)
    if raw["scenario"] != expected_id or not SCENARIO_ID.fullmatch(expected_id):
        raise DefinitionError(f"scenario must be {expected_id}")
    declared = raw["class"]
    if not isinstance(declared, str) or declared not in _CLASSES:
        raise DefinitionError("class must be read-only or mutating")
    if _CLASSES[declared] != (security.effect == "read"):
        raise DefinitionError(
            f"class {declared} disagrees with {tool}'s effect {security.effect}"
        )
    if not isinstance(raw["expected"], str) or raw["expected"] not in _EXPECTED:
        raise DefinitionError("expected must be success")
    return ScenarioDefinition(
        expected_id,
        operation,
        tool,
        _CLASSES[declared],
        _arguments(raw["arguments"], security),
        raw["expected"],
    )


def load_definitions(
    document: Any, security: Mapping[str, ToolSecurity]
) -> tuple[ScenarioDefinition, ...]:
    """Validate a parsed registry against *security*; reject it whole on any fault."""
    if not isinstance(document, dict) or set(document) != {
        "format_version",
        "definitions",
    }:
        raise DefinitionError(
            "registry must be an object with exactly format_version and definitions"
        )
    version = document["format_version"]
    if type(version) is not int or version != FORMAT_VERSION:
        raise DefinitionError(f"unsupported format_version {version!r}")
    if not isinstance(document["definitions"], list):
        raise DefinitionError("definitions must be a list")
    tools = {entry.operation: (tool, entry) for tool, entry in security.items()}
    loaded: list[ScenarioDefinition] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["definitions"]):
        try:
            definition = _definition(raw, tools)
            if definition.scenario in seen:
                raise DefinitionError(f"duplicate scenario {definition.scenario}")
        except DefinitionError as error:
            raise DefinitionError(f"definition {index}: {error}") from None
        seen.add(definition.scenario)
        loaded.append(definition)
    return tuple(loaded)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise DefinitionError(f"duplicate key {key}")
        document[key] = value
    return document


def read_definitions(
    path: Path = DEFINITIONS_PATH,
    security: Mapping[str, ToolSecurity] = TOOL_SECURITY,
) -> tuple[ScenarioDefinition, ...]:
    """Read and validate the registry file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DefinitionError(f"cannot read the registry: {error.strerror}") from None
    except UnicodeDecodeError:
        raise DefinitionError("the registry is not UTF-8") from None
    try:
        document = json.loads(text, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as error:
        raise DefinitionError(
            f"invalid JSON: {error.msg} at line {error.lineno}"
        ) from None
    except RecursionError:
        raise DefinitionError("invalid JSON: nested too deeply") from None
    return load_definitions(document, security)
