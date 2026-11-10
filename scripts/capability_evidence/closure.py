"""Import closures and source fingerprints for capability evidence."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Sequence
from pathlib import Path

PACKAGE = "hmcpctl"


def _readable(path: Path) -> bool:
    """Report whether a path is a regular file this walk may hash.

    A symlink is skipped rather than followed: one pointing outside the
    repository would make the fingerprint machine-dependent, so the runner's
    recorded value and CI's recomputation could never agree and the observation
    would read stale forever.
    """
    return path.is_file() and not path.is_symlink()


def _resolution_chain(package_root: Path, dotted: str) -> list[Path]:
    """Every file a dotted name resolves through, packages included.

    A dotted target resolves to ``<path>.py`` when that exists, else to
    ``<path>/__init__.py``; each ``__init__.py`` on the way is part of what the
    import executes and so belongs in the closure.
    """
    parts = dotted.split(".")
    found: list[Path] = []
    for index in range(1, len(parts) + 1):
        directory = package_root.joinpath(*parts[:index])
        if _readable(directory / "__init__.py"):
            found.append(directory / "__init__.py")
        if index == len(parts):
            module = directory.with_suffix(".py")
            if _readable(module):
                found.append(module)
    return found


def _package_of(package_root: Path, path: Path) -> tuple[str, ...]:
    """The dotted package a module file's relative imports resolve against.

    Both forms drop their last component: `jobs/core.py` and `jobs/__init__.py`
    each sit in the package `hmcpctl.jobs`, so `from .core import …` written in
    the latter resolves against `hmcpctl.jobs`, not `hmcpctl.jobs.__init__`.
    """
    return path.relative_to(package_root).with_suffix("").parts[:-1]


def _module_level_statements(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    """Statements the module executes at import time, one `If`/`Try` level in.

    A `TYPE_CHECKING` guard is a module-level `If`, so its body has to be read;
    a `FunctionDef`, `AsyncFunctionDef` or `ClassDef` body must not be. The
    distinction decides the design: `src/hmcpctl/__init__.py` imports `.cli`
    inside `main()` and sits on every resolution path, so an `ast.walk` here
    would pull 179 of 180 files into every closure and silently reinstate ADR
    0126's repository-wide fingerprint.
    """
    statements: list[ast.stmt] = []
    for statement in body:
        statements.append(statement)
        if isinstance(statement, ast.If):
            statements += _module_level_statements(statement.body)
            statements += _module_level_statements(statement.orelse)
        elif isinstance(statement, ast.Try):
            statements += _module_level_statements(statement.body)
            for handler in statement.handlers:
                statements += _module_level_statements(handler.body)
            statements += _module_level_statements(statement.orelse)
            statements += _module_level_statements(statement.finalbody)
    return statements


def _imported_names(path: Path, package: tuple[str, ...]) -> list[str]:
    """Every dotted name this module imports that could reach the package."""
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return []
    names: list[str] = []
    for statement in _module_level_statements(tree.body):
        if isinstance(statement, ast.Import):
            names += [
                alias.name
                for alias in statement.names
                if alias.name == PACKAGE or alias.name.startswith(f"{PACKAGE}.")
            ]
        elif isinstance(statement, ast.ImportFrom):
            names += _import_from_names(statement, package)
    return names


def _import_from_names(
    statement: ast.ImportFrom, package: tuple[str, ...]
) -> list[str]:
    """Resolve one `from ... import ...` to dotted names, absolute or relative."""
    if statement.level:
        upward = statement.level - 1
        if upward >= len(package):
            # Resolving this far up leaves the package: at `package` depth 1,
            # `from ..x import y` would resolve `x` against `src/` itself.
            return []
        base = package[: len(package) - upward]
    elif statement.module and (
        statement.module == PACKAGE or statement.module.startswith(f"{PACKAGE}.")
    ):
        base = ()
    else:
        return []
    target = (*base, *(statement.module.split(".") if statement.module else ()))
    if not target:
        return []
    dotted = ".".join(target)
    # An imported name may itself be a module in that package, or may be a class
    # or constant, in which case the name resolves to nothing and is skipped.
    return [dotted, *(f"{dotted}.{alias.name}" for alias in statement.names)]


def closure_paths(repo_root: Path, handler_module: str) -> list[Path]:
    """Every `src/hmcpctl/` file the handler module transitively imports."""
    package_root = repo_root / "src"
    found: set[Path] = set()
    pending = [handler_module]
    while pending:
        for path in _resolution_chain(package_root, pending.pop()):
            if path in found:
                continue
            found.add(path)
            pending += _imported_names(path, _package_of(package_root, path))
    return sorted(found)


def closure_fingerprint(repo_root: Path, handler_module: str) -> str:
    """Hash the operation's import closure, length-prefixed and path-ordered."""
    digest = hashlib.sha256()
    for path in closure_paths(repo_root, handler_module):
        relative = path.relative_to(repo_root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
