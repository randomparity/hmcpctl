import ast
import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
EXACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._,-]+\])?=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
# ADR 0068: the library runtime dependencies declare exactly one compatible
# range -- a floor at the current locked release and one upper bound. Anything
# else (extras, markers, multiple specifiers) must go through a reviewed
# widening of this pattern, not slip past the guard.
RANGED_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r">=(?P<floor>[A-Za-z0-9][A-Za-z0-9.!+_-]*),(?P<cap><[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
# The exhaustive library install-path set (ADR 0068). Adding a runtime
# dependency means adding it here consciously, with its policy-checked range.
LIBRARY_DEPENDENCIES = frozenset(
    {
        "asyncssh",
        "defusedxml",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "truststore",
        "typing-extensions",
    }
)
DEPENDABOT_CONFIG = """\
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 7
    groups:
      all-dependencies:
        applies-to: "version-updates"
        patterns:
          - "*"
    # These pins move together, which Dependabot cannot do; bump them by hand
    # with the coupled-bump procedure in CONTRIBUTING.md.
    ignore:
      - dependency-name: "fastmcp-slim"
      - dependency-name: "mcp"
      - dependency-name: "mcp-types"

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 7
    groups:
      github-actions-dependencies:
        patterns:
          - "*"
"""


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def _library_requirements() -> list[str]:
    """The library install path: [project.dependencies] (ADR 0068)."""
    project = _pyproject()
    return list(project["project"]["dependencies"])


def _exact_requirements() -> list[str]:
    """Everything ADR 0001 still governs: app extra, dev group, build backend."""
    project = _pyproject()
    return [
        *project["project"]["optional-dependencies"]["app"],
        *project["dependency-groups"]["dev"],
        *project["build-system"]["requires"],
    ]


def _locked_versions() -> dict[str, str]:
    with (ROOT / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)
    return {
        package["name"].lower().replace("_", "-"): package["version"]
        for package in lock["package"]
        if "version" in package
    }


def _release(version: str) -> tuple[int, ...]:
    """Numeric release tuple of the plain X.Y.Z shapes this lock pins."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    assert match, f"unsupported version shape for range comparison: {version}"
    return tuple(int(part) for part in match[0].split("."))


@pytest.mark.parametrize("requirement", _exact_requirements())
def test_application_surface_dependencies_are_exactly_pinned(
    requirement: str,
) -> None:
    assert EXACT_REQUIREMENT.fullmatch(requirement), (
        f"application-surface dependency must stay exactly pinned: {requirement}"
    )


@pytest.mark.parametrize("requirement", _library_requirements())
def test_library_runtime_dependencies_declare_compatible_ranges(
    requirement: str,
) -> None:
    match = RANGED_REQUIREMENT.fullmatch(requirement)
    assert match, (
        f"library runtime dependency must declare one >=floor,<cap range: {requirement}"
    )
    assert match["name"].lower().replace("_", "-") in LIBRARY_DEPENDENCIES


def test_library_dependency_set_is_exhaustive() -> None:
    names = {
        RANGED_REQUIREMENT.fullmatch(requirement)["name"].lower().replace("_", "-")
        for requirement in _library_requirements()
    }
    assert names == LIBRARY_DEPENDENCIES, (
        "a runtime dependency joined or left the library set; update "
        "LIBRARY_DEPENDENCIES and ADR 0068's policy notes together"
    )


def test_direct_third_party_imports_are_declared() -> None:
    """Every imported third-party distribution is a direct project dependency."""
    imported_modules: set[str] = set()
    for path in (ROOT / "src" / "hmcpctl").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.partition(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_modules.add(node.module.partition(".")[0])

    third_party = imported_modules - sys.stdlib_module_names - {"hmcpctl"}
    distribution_names = importlib.metadata.packages_distributions()
    imported_distributions = {
        distribution.lower().replace("_", "-")
        for module in third_party
        for distribution in distribution_names.get(module, ())
    }
    unresolved = third_party - distribution_names.keys()

    project = _pyproject()["project"]
    requirements = [
        *project["dependencies"],
        *(
            requirement
            for extra in project["optional-dependencies"].values()
            for requirement in extra
        ),
    ]
    declared = {
        re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)[0]
        .lower()
        .replace("_", "-")
        for requirement in requirements
    }

    assert unresolved == set(), (
        f"cannot map imported modules to distributions: {unresolved}"
    )
    assert imported_distributions <= declared, (
        "runtime modules import undeclared distributions: "
        f"{sorted(imported_distributions - declared)}"
    )


def test_exact_application_pins_match_the_lockfile() -> None:
    locked = _locked_versions()
    for requirement in _exact_requirements():
        match = EXACT_REQUIREMENT.fullmatch(requirement)
        assert match, f"cannot verify a non-exact requirement: {requirement}"
        name = match["name"].lower().replace("_", "-")
        assert locked.get(name) == match["version"], (
            f"{name} is pinned to {match['version']} but lock has {locked.get(name)}"
        )


@pytest.mark.parametrize("requirement", _library_requirements())
def test_locked_versions_satisfy_the_declared_ranges(requirement: str) -> None:
    """The committed resolution stays inside every relaxed range (ADR 0068)."""
    match = RANGED_REQUIREMENT.fullmatch(requirement)
    assert match, f"cannot verify a non-ranged requirement: {requirement}"
    name = match["name"].lower().replace("_", "-")
    locked = _locked_versions().get(name)
    assert locked is not None, f"{name} is declared but missing from uv.lock"
    floor = _release(match["floor"])
    cap = _release(match["cap"].removeprefix("<"))
    assert floor <= _release(locked) < cap, (
        f"lock has {name} {locked}, outside the declared range {requirement}"
    )


def test_dependabot_updates_dependencies_as_cooled_groups() -> None:
    config = ROOT / ".github" / "dependabot.yml"
    assert config.read_text() == DEPENDABOT_CONFIG
