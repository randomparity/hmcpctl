"""Lazy discovery of capability handlers and source observation routes."""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

from capability_evidence import schema as capability_schema
from capability_evidence.schema import InventoryError


@dataclass(frozen=True)
class RegistryTool:
    tool: str
    operation: str
    handler: str
    signature: str
    surfaces: tuple[str, ...]


def _bound_handlers() -> dict[str, Callable[..., object]]:
    """The handlers built only by a factory: gateway, inventory, plan, power, provision,
    reconfigure, decommission, inspect, handoff."""
    from fastmcp import FastMCP

    from hmcpctl.server_tools.catalog import TOOL_SECURITY
    from hmcpctl.server_tools.gateway import gateway_handlers
    from hmcpctl.server_tools.inventory.logical import (
        INVENTORY_TOOL_NAME,
        inventory_handler,
    )
    from hmcpctl.server_tools.lpar.decommission import (
        DECOMMISSION_TOOL_NAME,
        decommission_handler,
    )
    from hmcpctl.server_tools.lpar.handoff import HANDOFF_TOOL_NAME, handoff_handler
    from hmcpctl.server_tools.lpar.inspect import INSPECT_TOOL_NAME, inspect_handler
    from hmcpctl.server_tools.lpar.plan import PLAN_TOOL_NAME, plan_handler
    from hmcpctl.server_tools.lpar.power import POWER_TOOL_NAME, power_handler
    from hmcpctl.server_tools.lpar.provision import (
        PROVISION_TOOL_NAME,
        provision_handler,
    )
    from hmcpctl.server_tools.lpar.reconfigure import (
        RECONFIGURE_TOOL_NAME,
        reconfigure_handler,
    )

    bound = gateway_handlers(FastMCP(name="capability-inventory"), TOOL_SECURITY)
    handlers = {name: handler for name, (handler, _security) in bound.items()}
    handlers[INVENTORY_TOOL_NAME] = inventory_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[PLAN_TOOL_NAME] = plan_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[POWER_TOOL_NAME] = power_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[PROVISION_TOOL_NAME] = provision_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[RECONFIGURE_TOOL_NAME] = reconfigure_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[DECOMMISSION_TOOL_NAME] = decommission_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[INSPECT_TOOL_NAME] = inspect_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    handlers[HANDOFF_TOOL_NAME] = handoff_handler(
        TOOL_SECURITY, lambda _name: False, lambda *_args: None
    )
    return handlers


def discover_registry() -> tuple[RegistryTool, ...]:
    from hmcpctl.server_tools import command, permissions
    from hmcpctl.server_tools.catalog import TOOL_MODULES, TOOL_SECURITY

    modules = (*TOOL_MODULES, command, permissions)
    composed = _bound_handlers()
    result: list[RegistryTool] = []
    for tool, security in sorted(TOOL_SECURITY.items()):
        if tool == "hmc_effective_permissions":
            result.append(
                RegistryTool(
                    tool=tool,
                    operation=security.operation,
                    handler="hmcpctl.server_tools.permissions.<composed>",
                    signature="()",
                    surfaces=("mcp",),
                )
            )
            continue
        handlers = list(
            {
                id(handler): handler
                for module in modules
                if hasattr(module, tool)
                for handler in (getattr(module, tool),)
            }.values()
        ) + ([composed[tool]] if tool in composed else [])
        if len(handlers) != 1:
            raise InventoryError(
                f"registry tool {tool!r} resolves to {len(handlers)} handlers"
            )
        handler = inspect.unwrap(handlers[0])
        result.append(
            RegistryTool(
                tool=tool,
                operation=security.operation,
                handler=f"{handler.__module__}.{handler.__name__}",
                signature=str(inspect.signature(handler)),
                surfaces=("mcp",),
            )
        )
    return tuple(result)


def _module_observation_sites(module: ModuleType | None) -> set[tuple[str, int, str]]:
    if module is None:
        raise InventoryError("cannot resolve observation source module")
    tree = ast.parse(inspect.getsource(module))
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    sites: set[tuple[str, int, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record_verified"
        ):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        operation = keywords.get("operation")
        if not isinstance(operation, ast.Constant) or not isinstance(
            operation.value, str
        ):
            raise InventoryError("observation operation must be a literal")
        values = []
        for field in (node.args[0] if node.args else None, keywords.get("scenario")):
            if isinstance(field, ast.Constant):
                values.append(field.value)
            elif isinstance(field, ast.Name):
                values.append(constants.get(field.id))
            else:
                values.append(None)
        row, scenario = values
        if (
            type(row) is not int
            or row < 0
            or not capability_schema.matches(capability_schema.SCENARIO_ID, scenario)
        ):
            raise InventoryError(
                "observation row and scenario must resolve from source constants"
            )
        sites.add((operation.value, row, cast(str, scenario)))
    return sites


def discover_observation_sites() -> dict[tuple[str, int], str]:
    """Associate result-row identities with scenarios, independently of dispatch IDs."""
    from live_test.runner import _SCENARIO_MODULES

    sites: dict[tuple[str, int], str] = {}
    for module in _SCENARIO_MODULES:
        for operation, row, scenario in _module_observation_sites(module):
            key = (operation, row)
            if key in sites and sites[key] != scenario:
                raise InventoryError(
                    "observation result-row identity has ambiguous scenarios"
                )
            sites[key] = scenario
    return sites


def release_routes() -> set[tuple[str, str, str]]:
    from live_test.runner import RELEASE_SCENARIO_SUBTASKS, SUBTASK_GROUPS, SUBTASKS

    routes: set[tuple[str, str, str]] = set()
    seen: set[str] = set()
    for subtask in set(RELEASE_SCENARIO_SUBTASKS.values()):
        module = inspect.getmodule(SUBTASKS[subtask])
        for operation, _row, scenario in _module_observation_sites(module):
            if RELEASE_SCENARIO_SUBTASKS.get(scenario) != subtask:
                continue
            seen.add(scenario)
            for arm, tasks in SUBTASK_GROUPS.items():
                # The dedicated arm precedes bare-CEC in all; its row 29 makes
                # exercise_bare_cec skip to preserve the first fixture's recovery data.
                if arm == "all" and scenario == "st35-bare-cec":
                    continue
                if subtask in tasks:
                    routes.add((operation, arm, scenario))
    if seen != set(RELEASE_SCENARIO_SUBTASKS):
        raise InventoryError(
            "release scenario registration has no record_verified declaration"
        )
    return routes


def load_release_claims(
    path: Path, operations: Collection[str]
) -> list[dict[str, str]]:
    document = capability_schema.load_json(path)
    if path.suffix != ".json" or not re.fullmatch(
        r"v[A-Za-z0-9][A-Za-z0-9.+-]*", path.stem
    ):
        raise InventoryError(f"{path}: invalid release claims filename")
    if (
        set(document) != {"format_version", "operations"}
        or type(document.get("format_version")) is not int
        or document["format_version"] != 1
        or not isinstance(document.get("operations"), list)
    ):
        raise InventoryError(f"{path}: invalid release claims format")
    routes = release_routes()
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for row in cast(list[object], document["operations"]):
        if (
            not isinstance(row, dict)
            or set(row) != {"operation", "arm", "scenario"}
            or any(not isinstance(value, str) or not value for value in row.values())
        ):
            raise InventoryError(f"{path}: invalid release claim row")
        operation, arm, scenario = (
            row[key] for key in ("operation", "arm", "scenario")
        )
        if operation not in operations:
            raise InventoryError(f"{path}: unknown claimed operation {operation!r}")
        if operation in seen:
            raise InventoryError(f"{path}: duplicate claimed operation {operation!r}")
        if (operation, arm, scenario) not in routes:
            raise InventoryError(
                f"{path}: unknown release claim route {(operation, arm, scenario)!r}"
            )
        seen.add(operation)
        result.append(row)
    return result
