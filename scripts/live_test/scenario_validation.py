"""Validate declared scenario outcomes before hardware dispatch."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable, Mapping, Sequence
from types import ModuleType

from hmcpctl.server import TOOL_SECURITY
from live_test.observation import ExpectedOutcome


def validate_expected_dispatch(tool: str, expected: Sequence[ExpectedOutcome]) -> None:
    for outcome in expected:
        security = TOOL_SECURITY.get(tool)
        if security is None or outcome.operation != security.operation:
            raise ValueError(f"expected outcome operation does not match tool {tool}")


def _declared_names(
    node: ast.expr, declarations: Mapping[str, ExpectedOutcome]
) -> list[str]:
    if not isinstance(node, ast.List) or not all(
        isinstance(item, ast.Name) and item.id in declarations for item in node.elts
    ):
        raise ValueError(
            "expected outcomes must be a literal list of module declarations"
        )
    return [item.id for item in node.elts if isinstance(item, ast.Name)]


def _result_names(nodes: Sequence[ast.expr]) -> tuple[str, str]:
    if len(nodes) != 2 or not all(isinstance(node, ast.Name) for node in nodes):
        raise ValueError("declared results require a named status/data pair")
    names = tuple(node.id for node in nodes if isinstance(node, ast.Name))
    if names[0] == names[1]:
        raise ValueError("declared status and data names must differ")
    return names[0], names[1]


def _call_result_names(
    node: ast.Call, parents: Mapping[ast.AST, ast.AST]
) -> tuple[str, str]:
    awaiter = parents.get(node)
    assignment = parents.get(awaiter) if awaiter else None
    if (
        not isinstance(awaiter, ast.Await)
        or not isinstance(assignment, ast.Assign)
        or len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Tuple)
    ):
        raise ValueError("declared calls require an assigned awaited status/data pair")
    return _result_names(assignment.targets[0].elts)


def _validate_declared_function(
    function: ast.AsyncFunctionDef,
    declarations: Mapping[str, ExpectedOutcome],
) -> None:
    parents = {
        child: node
        for node in ast.walk(function)
        for child in ast.iter_child_nodes(node)
    }
    events = sorted(
        (
            node
            for node in ast.walk(function)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute))
            or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    pending: dict[tuple[str, str], list[str]] = {}
    for node in events:
        if isinstance(node, ast.Name):
            if any(node.id in binding for binding in pending):
                raise ValueError(
                    f"unrecorded declared result overwritten in {function.name}"
                )
            continue
        assert isinstance(node.func, ast.Attribute)
        if node.func.attr == "record_with_expected":
            if len(node.args) != 5:
                raise ValueError(
                    "expected-outcome recording must use five positional arguments"
                )
            names = _declared_names(node.args[4], declarations)
            if pending.pop(_result_names(node.args[2:4]), None) != names:
                raise ValueError(
                    f"expected-outcome result pairing mismatch in {function.name}"
                )
        if node.func.attr != "call":
            continue
        expected = next(
            (kw.value for kw in node.keywords if kw.arg == "expected"), None
        )
        if expected is None:
            continue
        binding = _call_result_names(node, parents)
        names = _declared_names(expected, declarations)
        tool = node.args[1] if len(node.args) > 1 else None
        if not isinstance(tool, ast.Constant) or not isinstance(tool.value, str):
            raise ValueError(  # noqa: TRY004 - invalid scenario source is a startup configuration error
                "declared dispatch requires a literal tool name"
            )
        validate_expected_dispatch(tool.value, [declarations[name] for name in names])
        pending[binding] = names
    if pending:
        raise ValueError(f"unrecorded declared results in {function.name}")


def validate_declared_outcomes(modules: Iterable[ModuleType | None]) -> None:
    """Validate every registered scenario before any client can reach hardware."""
    registered = {security.operation for security in TOOL_SECURITY.values()}
    for module in modules:
        if module is None:
            raise ValueError("cannot resolve a live scenario module")
        declarations = {
            name: value
            for name, value in vars(module).items()
            if isinstance(value, ExpectedOutcome)
        }
        if any(
            outcome.operation not in registered for outcome in declarations.values()
        ):
            raise ValueError(
                f"unregistered expected outcome operation in {module.__name__}"
            )
        for function in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(function, ast.AsyncFunctionDef):
                _validate_declared_function(function, declarations)
