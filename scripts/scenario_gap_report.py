"""Report the gap between the capability ledger and the live scenario registry.

Usage:
    python scripts/scenario_gap_report.py                     # report; exits 0
    python scripts/scenario_gap_report.py --fail-on-dispatch  # exit 1 on a dispatch finding

Joins ``docs/capabilities/operations.json`` and ``rows.json`` to the scenario
functions the runner's ``SUBTASKS`` registry reaches, names the dispatches of
functions it no longer reaches, and checks each registered dispatch against the
input schema the composed MCP application serves. Offline
and read-only: no HMC credential is needed and nothing is written.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import live_test_runner

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAPABILITIES = _REPO_ROOT / "docs" / "capabilities"
_PACKAGE = "live_test"
_SCENARIO_PACKAGE = _REPO_ROOT / "scripts" / _PACKAGE
# Keyword arguments `RunState.call` consumes itself; they never reach the tool.
_RUNNER_KEYWORDS = frozenset({"expected", "reuse_gaps"})
_DISPATCH_PREFIXES = ("unregistered:", "dispatch-mismatch:", "unreadable:")


@dataclass(frozen=True)
class Dispatch:
    """One ``call(client, "<tool>", ...)`` site and the top-level function holding it."""

    site: str
    function: str
    tool: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class DispatchSite:
    """One ``.call(...)`` site as written, readable or not.

    ``arguments`` pairs each tool keyword with the expression supplying it, so a
    consumer can check the type a site passes; a ``**`` splat has ``None`` for its
    name. ``unreadable`` says why the site cannot be read statically, else None.
    """

    lineno: int
    function: str | None
    tool: str | None
    arguments: tuple[tuple[str | None, ast.expr], ...]
    unreadable: str | None


@dataclass(frozen=True)
class Verified:
    """One ``record_verified(..., operation="<id>")`` site."""

    site: str
    function: str
    operation: str


@dataclass(frozen=True)
class Scan:
    """One scenario-package module: what it dispatches, names, and references.

    ``references`` maps each top-level function or class to the ``(module, name)``
    pairs its body names, which is what decides whether a registered scenario
    reaches it.
    """

    module: str
    dispatches: tuple[Dispatch, ...]
    verified: tuple[Verified, ...]
    unreadable: tuple[str, ...]
    references: Mapping[str, frozenset[tuple[str, str]]]


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _references(
    definition: ast.AST,
    module: str,
    local: set[str],
    imported: Mapping[str, tuple[str, str]],
    packages: Mapping[str, str],
) -> frozenset[tuple[str, str]]:
    """The package-level names one definition's body mentions, resolved to a module."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(definition):
        if isinstance(node, ast.Name) and node.id in local:
            found.add((module, node.id))
        elif isinstance(node, ast.Name) and node.id in imported:
            found.add(imported[node.id])
        elif isinstance(node, ast.Attribute):
            holder = node.value
            if isinstance(holder, ast.Name) and holder.id in packages:
                found.add((packages[holder.id], node.attr))
            elif (  # `import live_test.m` then `live_test.m.f`
                isinstance(holder, ast.Attribute)
                and isinstance(holder.value, ast.Name)
                and holder.value.id == _PACKAGE
            ):
                found.add((holder.attr, node.attr))
    return frozenset(found)


def _import_origin(node: ast.ImportFrom) -> str | None:
    """The package module a `from` import reads, `""` for the package, else None."""
    origin = node.module or ""
    if node.level == 1:
        return origin
    if node.level == 0 and origin == _PACKAGE:
        return ""
    if node.level == 0 and origin.startswith(f"{_PACKAGE}."):
        return origin.removeprefix(f"{_PACKAGE}.")
    return None


def _package_imports(
    tree: ast.Module,
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Names bound to package functions, and names bound to package modules.

    Imports anywhere in the module count, relative or absolute through `live_test`:
    a function-local import still names a package function.
    """
    imported: dict[str, tuple[str, str]] = {}
    packages: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.startswith(f"{_PACKAGE}."):
                    packages[alias.asname] = alias.name.removeprefix(f"{_PACKAGE}.")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        origin = _import_origin(node)
        if origin is None:
            continue
        for alias in node.names:
            if origin:
                imported[alias.asname or alias.name] = (origin, alias.name)
            else:
                packages[alias.asname or alias.name] = alias.name
    return imported, packages


def _attribute_calls(tree: ast.Module) -> list[tuple[str | None, ast.Call]]:
    """Every ``x.attr(...)`` call in the module in source order, with the top-level
    definition holding it (None outside one)."""
    return sorted(
        (
            (statement.name if isinstance(statement, _DEFINITIONS) else None, node)
            for statement in tree.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ),
        key=lambda pair: (pair[1].lineno, pair[1].col_offset),
    )


def _dispatch_site(function: str | None, node: ast.Call) -> DispatchSite | None:
    """The dispatch ``node`` makes, or None when it is not a ``.call`` dispatch."""
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "call"):
        return None
    tool = _literal(node.args[1] if len(node.args) > 1 else None)
    arguments = tuple(
        (kw.arg, kw.value) for kw in node.keywords if kw.arg not in _RUNNER_KEYWORDS
    )
    unreadable = None
    if function is None:
        # A def nested in a top-level `if` or `try` has no registered name to
        # reach it by; saying so beats dropping its dispatch without a line.
        unreadable = "call outside a top-level function"
    elif tool is None or any(name is None for name, _ in arguments):
        unreadable = "dispatch with a non-literal tool or a ** splat"
    elif len(node.args) != 2 or any(isinstance(arg, ast.Starred) for arg in node.args):
        # RunState.call takes only (client, tool) positionally; the rest is keyword-only.
        unreadable = "dispatch with a * splat or extra positional arguments"
    return DispatchSite(node.lineno, function, tool, arguments, unreadable)


def dispatch_sites(tree: ast.Module) -> list[DispatchSite]:
    """Every ``.call`` dispatch in the module, in source order, readable or not."""
    return [
        site
        for function, node in _attribute_calls(tree)
        if (site := _dispatch_site(function, node)) is not None
    ]


def scan_source(source: str, label: str, module: str) -> Scan:
    """Read every dispatch, ``record_verified`` operation and reference in a module."""
    tree = ast.parse(source, filename=label)
    imported, packages = _package_imports(tree)
    definitions = [node for node in tree.body if isinstance(node, _DEFINITIONS)]
    # A module-level `alias = _impl` is a name a scenario can reach `_impl` through.
    aliases = {
        target.id: statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    local = {definition.name for definition in definitions} | aliases.keys()
    dispatches: list[Dispatch] = []
    verified: list[Verified] = []
    unreadable: list[str] = []
    for function, node in _attribute_calls(tree):
        site = f"{label}:{node.lineno}"
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        dispatch = _dispatch_site(function, node)
        if dispatch is not None:
            if dispatch.unreadable is not None:
                unreadable.append(f"{site} {dispatch.unreadable}")
            else:
                names = tuple(name for name, _ in dispatch.arguments if name)
                assert function is not None and dispatch.tool is not None
                dispatches.append(Dispatch(site, function, dispatch.tool, names))
            continue
        if function is None:
            if attribute == "record_verified":
                unreadable.append(f"{site} {attribute} outside a top-level function")
            continue
        if attribute == "record_verified":
            operation = next(
                (kw.value for kw in node.keywords if kw.arg == "operation"), None
            )
            name = _literal(operation)
            if name is None:
                unreadable.append(f"{site} record_verified without a literal operation")
                continue
            verified.append(Verified(site, function, name))
    references = {
        name: _references(node, module, local, imported, packages)
        for name, node in [
            *((definition.name, definition) for definition in definitions),
            *aliases.items(),
        ]
    }
    return Scan(
        module, tuple(dispatches), tuple(verified), tuple(unreadable), references
    )


def registered_definitions(
    scans: Iterable[Scan], roots: Iterable[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Every definition a registered scenario reaches through the names it mentions."""
    graph = {
        (scan.module, name): references
        for scan in scans
        for name, references in scan.references.items()
    }
    reached: set[tuple[str, str]] = set()
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node not in reached:
            reached.add(node)
            pending.extend(graph.get(node, ()))
    return reached


def build_report(
    scans: Sequence[Scan],
    roots: Iterable[tuple[str, str]],
    operations: Sequence[Mapping[str, Any]],
    row_ids: Iterable[str],
    schemas: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Every finding as one prefixed line, followed by one ``summary:`` line.

    ``roots`` are the ``(module, function)`` pairs the ``SUBTASKS`` registry names.
    """
    by_tool = {entry["tool"]: entry["operation"] for entry in operations}
    known = {entry["operation"] for entry in operations}
    registered = registered_definitions(scans, roots)
    exercised: set[str] = set()
    departed: list[str] = []
    unregistered: list[str] = []
    mismatches: list[str] = []
    unreadable: list[str] = []
    for scan in scans:
        unreadable += [f"unreadable: {item}" for item in scan.unreadable]
        for dispatch in scan.dispatches:
            if (scan.module, dispatch.function) not in registered:
                departed.append(
                    f"departed: {dispatch.site} {dispatch.function} "
                    f"dispatches {dispatch.tool}"
                )
                continue
            if dispatch.tool not in schemas or dispatch.tool not in by_tool:
                unregistered.append(
                    f"unregistered: {dispatch.site} tool {dispatch.tool}"
                )
                continue
            exercised.add(by_tool[dispatch.tool])
            mismatches += [
                f"dispatch-mismatch: {dispatch.site} {problem}"
                for problem in live_test_runner._dispatch_problems(
                    dispatch.tool, dispatch.arguments, schemas
                )
            ]
        for record in scan.verified:
            if (scan.module, record.function) not in registered:
                departed.append(
                    f"departed: {record.site} {record.function} "
                    f"records {record.operation}"
                )
            elif record.operation in known:
                exercised.add(record.operation)
            else:
                unregistered.append(
                    f"unregistered: {record.site} operation {record.operation}"
                )
    covered_rows = {
        row
        for entry in operations
        if entry["operation"] in exercised
        for row in entry["row_ids"]
    }
    all_rows = sorted(set(row_ids))
    uncovered_operations = sorted(known - exercised)
    uncovered_rows = [row for row in all_rows if row not in covered_rows]
    summary = (
        f"summary: {sum(len(scan.dispatches) for scan in scans)} dispatches; "
        f"operations {len(known) - len(uncovered_operations)}/{len(known)} "
        f"exercised; rows {len(all_rows) - len(uncovered_rows)}/{len(all_rows)} "
        f"exercised; {len(departed)} departed; {len(unregistered)} unregistered; "
        f"{len(mismatches)} dispatch mismatches; {len(unreadable)} unreadable"
    )
    return [
        *(f"uncovered-operation: {name}" for name in uncovered_operations),
        *(f"uncovered-row: {row}" for row in uncovered_rows),
        *departed,
        *unregistered,
        *mismatches,
        *unreadable,
        summary,
    ]


def exit_status(lines: Iterable[str], *, fail_on_dispatch: bool) -> int:
    """1 only when asked to fail and a dispatch-level finding exists."""
    if fail_on_dispatch and any(line.startswith(_DISPATCH_PREFIXES) for line in lines):
        return 1
    return 0


async def served_schemas() -> dict[str, dict[str, Any]]:
    """The input schema each tool serves, through the live runner's own composition."""
    async with live_test_runner.served_client() as client:
        return await live_test_runner.served_schemas(client)


def scenario_scans() -> list[Scan]:
    """Scan every module of the scenario package, registered or not."""
    return [
        scan_source(
            path.read_text(encoding="utf-8"),
            str(path.relative_to(_REPO_ROOT)),
            path.stem,
        )
        for path in sorted(_SCENARIO_PACKAGE.glob("*.py"))
    ]


def scenario_roots() -> set[tuple[str, str]]:
    """The ``(module, function)`` pair of every ``SUBTASKS`` entry."""
    return {
        (task.__module__.rsplit(".", 1)[-1], task.__name__)
        for task in live_test_runner.SUBTASKS.values()
    }


def _load(name: str, key: str) -> list[Any]:
    document = json.loads((_CAPABILITIES / name).read_text(encoding="utf-8"))
    return document[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fail-on-dispatch",
        action="store_true",
        help="exit 1 when a dispatch is unregistered, mismatched, or unreadable",
    )
    arguments = parser.parse_args(argv)
    lines = build_report(
        scenario_scans(),
        scenario_roots(),
        _load("operations.json", "operations"),
        (row["id"] for row in _load("rows.json", "rows")),
        asyncio.run(served_schemas()),
    )
    print("\n".join(lines))
    return exit_status(lines, fail_on_dispatch=arguments.fail_on_dispatch)


if __name__ == "__main__":
    sys.exit(main())
