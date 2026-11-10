"""Contract tests for the repository test-layout guard."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

MODULE_PATH = Path(__file__).parents[2] / "scripts" / "check_test_layout.py"
MODULE_SPEC = importlib.util.spec_from_file_location("check_test_layout", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
check_test_layout = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(check_test_layout)


def _repository(tmp_path: Path, *files: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for relative_path in files:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test fixture\n", encoding="utf-8")
    return tmp_path


def test_accepts_only_canonical_conftest(tmp_path: Path, capsys) -> None:
    root = _repository(tmp_path, "tests/conftest.py")

    assert check_test_layout.main(["--repo-root", str(root)]) == 0
    assert "canonical test layout" in capsys.readouterr().out


def test_rejects_nested_conftest(tmp_path: Path, capsys) -> None:
    root = _repository(tmp_path, "tests/conftest.py", "tests/app/conftest.py")
    subprocess.run(["git", "add", "tests/app/conftest.py"], cwd=root, check=True)

    assert check_test_layout.main(["--repo-root", str(root)]) == 1
    error = capsys.readouterr().err
    assert "tests/app/conftest.py" in error
    assert "tests/conftest.py is the only permitted conftest.py" in error


def test_rejects_second_root_conftest(tmp_path: Path, capsys) -> None:
    root = _repository(tmp_path, "tests/conftest.py", "conftest.py")

    assert check_test_layout.main(["--repo-root", str(root)]) == 1
    error = capsys.readouterr().err
    assert "conftest.py" in error
    assert "tests/conftest.py is the only permitted conftest.py" in error


def test_accepts_script_with_its_test_module(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        "tests/conftest.py",
        "scripts/live_test_evidence.py",
        "tests/scripts/test_live_test_evidence.py",
    )

    assert check_test_layout.main(["--repo-root", str(root)]) == 0


def test_rejects_script_without_a_test_module(tmp_path: Path, capsys) -> None:
    root = _repository(tmp_path, "tests/conftest.py", "scripts/live_test_evidence.py")

    assert check_test_layout.main(["--repo-root", str(root)]) == 1
    error = capsys.readouterr().err
    assert (
        "scripts/live_test_evidence.py -> tests/scripts/test_live_test_evidence.py"
        in error
    )
    assert "one test module per scripts/ entry point" in error


def test_ignores_live_test_package_modules(tmp_path: Path) -> None:
    """Library modules under scripts/live_test/ are outside the convention."""
    root = _repository(
        tmp_path, "tests/conftest.py", "scripts/live_test/arms/vmedia.py"
    )

    assert check_test_layout.main(["--repo-root", str(root)]) == 0


def test_accepts_a_documented_exception(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        "tests/conftest.py",
        "scripts/live_test_runner.py",
        "tests/test_live_runner.py",
    )

    assert check_test_layout.main(["--repo-root", str(root)]) == 0


def test_rejects_an_exception_whose_module_is_gone(tmp_path: Path, capsys) -> None:
    """A deleted exception target is a violation, not a silent pass."""
    root = _repository(tmp_path, "tests/conftest.py", "scripts/live_test_runner.py")

    assert check_test_layout.main(["--repo-root", str(root)]) == 1
    assert (
        "scripts/live_test_runner.py -> tests/test_live_runner.py"
        in capsys.readouterr().err
    )


def test_reports_both_violation_kinds_together(tmp_path: Path, capsys) -> None:
    root = _repository(
        tmp_path, "tests/conftest.py", "tests/app/conftest.py", "scripts/orphan.py"
    )
    subprocess.run(["git", "add", "tests/app/conftest.py"], cwd=root, check=True)

    assert check_test_layout.main(["--repo-root", str(root)]) == 1
    error = capsys.readouterr().err
    assert "tests/app/conftest.py" in error
    assert "scripts/orphan.py -> tests/scripts/test_orphan.py" in error


def test_repository_satisfies_its_own_layout_rule() -> None:
    """The rule this change adds is green on the tree that adds it."""
    assert check_test_layout.scripts_without_test_modules(MODULE_PATH.parents[1]) == ()
