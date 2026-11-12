"""Enforce the repository test layout: one conftest, one test module per script."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 # Call sites document execution authority.
import sys
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_CONFTEST = "tests/conftest.py"

# AGENTS.md documents two scripts that predate the tests/scripts/test_<name>.py
# convention. Each maps to the module that actually covers it, and that module
# must exist: an exception whose target was deleted would otherwise excuse the
# script from coverage entirely.
_TEST_MODULE_EXCEPTIONS = {
    "scripts/check_env_vars.py": "tests/test_env_var_guard.py",
    "scripts/live_test_runner.py": "tests/test_live_runner.py",
}


def _repository_files(repo_root: Path) -> tuple[str, ...]:
    """Return tracked and unignored untracked repository paths."""
    try:
        # Fixed Git inventory query; inherited repository routing is removed.
        result = subprocess.run(  # nosec B603, B607
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"cannot inspect repository paths in {repo_root}: {error}"
        ) from error
    return tuple(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)


def unexpected_conftests(repo_root: Path) -> tuple[str, ...]:
    """Return repository conftest paths that violate the canonical layout."""
    return tuple(
        sorted(
            path
            for path in _repository_files(repo_root)
            if PurePosixPath(path).name == "conftest.py" and path != _CANONICAL_CONFTEST
        )
    )


def _executable_scripts(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the top-level ``scripts/*.py`` entry points.

    Modules under ``scripts/live_test/`` are library code the runner imports,
    never invoked directly, and they are covered by behaviour-grouped modules
    rather than one apiece. The convention governs what an operator can run.
    """
    return tuple(
        sorted(
            path
            for path in paths
            # An anchored comparison, not PurePosixPath.match: that matches from
            # the right, so "scripts/*.py" would also admit tests/scripts/*.py.
            if PurePosixPath(path).parent == PurePosixPath("scripts")
            and path.endswith(".py")
            and PurePosixPath(path).name != "__init__.py"
        )
    )


def scripts_without_test_modules(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """Return ``(script, expected test module)`` pairs with no test module.

    A documented exception reports the module it names, so a deleted exception
    target surfaces as the violation it is rather than passing silently.
    """
    paths = _repository_files(repo_root)
    present = set(paths)
    missing = []
    for script in _executable_scripts(paths):
        expected = _TEST_MODULE_EXCEPTIONS.get(
            script, f"tests/scripts/test_{PurePosixPath(script).stem}.py"
        )
        if expected not in present:
            missing.append((script, expected))
    return tuple(missing)


def _report_conftests(unexpected: tuple[str, ...]) -> None:
    print("ERROR: repository test layout is invalid:", file=sys.stderr)
    for path in unexpected:
        print(f"     {path}", file=sys.stderr)
    print(
        "     tests/conftest.py is the only permitted conftest.py; "
        "another file can shadow its bare imports during pytest collection",
        file=sys.stderr,
    )


def _report_missing_modules(missing: tuple[tuple[str, str], ...]) -> None:
    print("ERROR: scripts without a test module:", file=sys.stderr)
    for script, expected in missing:
        print(f"     {script} -> {expected}", file=sys.stderr)
    print(
        "     AGENTS.md requires one test module per scripts/ entry point; "
        "add the module above or document a new exception",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    try:
        unexpected = unexpected_conftests(args.repo_root)
        missing = scripts_without_test_modules(args.repo_root)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if unexpected:
        _report_conftests(unexpected)
    if missing:
        _report_missing_modules(missing)
    if unexpected or missing:
        return 1

    print("OK: repository uses the canonical test layout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
