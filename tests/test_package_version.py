from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
COMMAND_TIMEOUT_SECONDS = 180
# ADR 0250: a tree without Git metadata takes its version from this variable.
PRETEND_ENV = "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL"
PACKAGE_VERSION = "0.0.1.dev42"
# Every prefix setuptools-scm and vcs-versioning read overrides from; an inherited one
# would decide the version instead of the case under test.
VERSION_OVERRIDE_PREFIXES = ("SETUPTOOLS_SCM_", "VCS_VERSIONING_")


def environment_without_version_overrides() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(VERSION_OVERRIDE_PREFIXES)
    }


def run(
    *command: str,
    cwd: Path | None = None,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **environment_without_version_overrides(),
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
        PRETEND_ENV: PACKAGE_VERSION,
        **(extra_env or {}),
    }
    if not environment[PRETEND_ENV]:
        del environment[PRETEND_ENV]
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=True,
        env=environment,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def copy_tracked_project(destination: Path) -> None:
    tracked = run("git", "ls-files", "-z", cwd=PROJECT_ROOT).stdout.split("\0")
    for relative in filter(None, tracked):
        source = PROJECT_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def wheel_metadata(wheel: Path) -> tuple[str, set[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_name))
    return str(metadata["Version"]), names


@dataclass(frozen=True)
class BuiltProject:
    project: Path
    wheel: Path
    sdist: Path
    version: str


@pytest.fixture(scope="module")
def built_project(tmp_path_factory: pytest.TempPathFactory) -> BuiltProject:
    workspace = tmp_path_factory.mktemp("package-version")
    project = workspace / "project"
    project.mkdir()
    copy_tracked_project(project)
    # ADR 0250: without a repository the version comes from the pretend variable. The
    # absence of a repository here is the property under test, not an accident of how
    # the fixture is assembled -- adding `git init` would retire that coverage.
    assert not (project / ".git").exists()
    artifacts = workspace / "artifacts"
    run(
        "uv",
        "build",
        "--wheel",
        "--sdist",
        "--out-dir",
        str(artifacts),
        str(project),
    )
    return BuiltProject(
        project=project,
        wheel=next(artifacts.glob("*.whl")),
        sdist=next(artifacts.glob("*.tar.gz")),
        version=PACKAGE_VERSION,
    )


def test_wheel_metadata_and_package_contents(built_project: BuiltProject) -> None:
    version, names = wheel_metadata(built_project.wheel)
    source_modules = {
        path.relative_to(built_project.project / "src").as_posix()
        for path in (built_project.project / "src" / "hmcpctl").rglob("*.py")
    }

    assert version == built_project.version
    assert source_modules <= names
    assert "hmcpctl/__init__.py" in names
    assert "hmcpctl/server.py" in names


def test_installed_wheel_runtime_version_matches_metadata(
    built_project: BuiltProject, tmp_path: Path
) -> None:
    target = tmp_path / "site-packages"
    run(
        "uv",
        "pip",
        "install",
        "--target",
        str(target),
        "--no-deps",
        str(built_project.wheel),
    )
    result = run(
        sys.executable,
        "-c",
        "import hmcpctl; print(hmcpctl.__version__)",
        cwd=tmp_path,
        extra_env={"PYTHONPATH": str(target)},
    )

    assert result.stdout.strip() == built_project.version


def test_sdist_embeds_version_and_rebuilds(
    built_project: BuiltProject, tmp_path: Path
) -> None:
    with tarfile.open(built_project.sdist) as archive:
        names = archive.getnames()
    assert any(name.endswith("/PKG-INFO") for name in names)

    rebuilt = tmp_path / "rebuilt"
    run(
        "uv",
        "build",
        "--wheel",
        "--out-dir",
        str(rebuilt),
        str(built_project.sdist),
    )
    version, _ = wheel_metadata(next(rebuilt.glob("*.whl")))

    assert version == built_project.version


def test_editable_install_uses_computed_metadata(
    built_project: BuiltProject, tmp_path: Path
) -> None:
    environment = tmp_path / "editable-venv"
    run("uv", "venv", "--python", sys.executable, str(environment))
    interpreter = environment / "bin" / "python"
    run(
        "uv",
        "pip",
        "install",
        "--python",
        str(interpreter),
        "--no-deps",
        "--editable",
        str(built_project.project),
    )
    result = run(
        str(interpreter),
        "-c",
        "import hmcpctl; print(hmcpctl.__version__)",
        cwd=tmp_path,
    )

    assert result.stdout.strip() == built_project.version


def test_gitless_build_without_pretend_version_fails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    copy_tracked_project(project)

    result = run(
        "uv",
        "build",
        "--wheel",
        "--out-dir",
        str(tmp_path / "artifacts"),
        str(project),
        check=False,
        extra_env={PRETEND_ENV: ""},
    )

    assert result.returncode != 0
    assert PRETEND_ENV in result.stderr


def git(*args: str, cwd: Path) -> str:
    return run("git", *args, cwd=cwd).stdout.strip()


def first_commit_after_release(clone: Path) -> str:
    """The first commit on the ancestry path whose only unreleased commit is itself."""
    candidates = git(
        "rev-list", "--reverse", "--ancestry-path", "v0.1.0..HEAD", cwd=clone
    ).split()
    return next(
        commit
        for commit in candidates
        if git("rev-list", "--count", f"v0.1.0..{commit}", cwd=clone) == "1"
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("tag", r"0\.1\.0"),
        ("one-past", r"0\.1\.1\.dev1\+g(?P<sha>[0-9a-f]+)"),
        ("dirty", r"0\.1\.1\.dev0\+g[0-9a-f]+\.d\d{8}"),
        ("no-tags", r"0\.1\.dev\d+\+g[0-9a-f]+"),
    ],
)
def test_version_derives_from_release_tags(
    case: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hatch_vcs.version_source import VCSVersionSource

    for key in os.environ:
        if key.startswith(VERSION_OVERRIDE_PREFIXES):
            monkeypatch.delenv(key)
    # Clone and check out only; no commit, tag or merge, so no hook can run (#237).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    tag = run(
        "git", "rev-parse", "--verify", "v0.1.0^{commit}", cwd=PROJECT_ROOT, check=False
    )
    assert tag.returncode == 0, "tag v0.1.0 missing; fetch tags (git fetch --tags)"
    clone = tmp_path / "clone"
    no_tags = ("--no-tags",) if case == "no-tags" else ()
    git(
        "clone",
        "--quiet",
        "--no-checkout",
        *no_tags,
        str(PROJECT_ROOT),
        str(clone),
        cwd=tmp_path,
    )
    if case == "one-past":
        target = first_commit_after_release(clone)
    elif case == "no-tags":
        target = "HEAD"
    else:
        target = "v0.1.0"
    git("checkout", "--quiet", "--detach", target, cwd=clone)
    if case == "dirty":
        with (clone / "README.md").open("a", encoding="utf-8") as readme:
            readme.write("\nlocal edit\n")
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)["tool"]["hatch"]["version"]

    version = VCSVersionSource(str(clone), config).get_version_data()["version"]

    match = re.fullmatch(expected, version)
    assert match, f"{case}: {version!r} does not match {expected!r}"
    if case == "one-past":
        assert target.startswith(match["sha"])
