"""Exercise a built wheel from fresh library-only and app environments."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

LIBRARY_PROBE = r"""
from importlib.util import find_spec
from pathlib import Path
import sys

import hmcpctl
from hmcpctl import api

expected = {
    "ConfigError",
    "HMCClient",
    "HMCConfig",
    "HMCError",
    "HMCTransportError",
    "TLSVerificationDisabledWarning",
}
assert set(api.__all__) == expected, api.__all__
assert find_spec("hmc_mcp") is None
for package in ("fastmcp", "mcp", "rich", "typer"):
    assert find_spec(package) is None, package
package_path = Path(hmcpctl.__file__).resolve()
environment = Path(sys.prefix).resolve()
assert package_path.is_relative_to(environment), (package_path, environment)
assert (package_path.parent / "py.typed").is_file(), package_path
"""


def select_wheel(artifact_dir: Path) -> Path:
    """Return the sole wheel in *artifact_dir* or reject ambiguous input."""
    wheels = sorted(artifact_dir.glob("*.whl"))
    if len(wheels) != 1 or not wheels[0].is_file():
        raise ValueError(
            f"expected exactly one wheel in {artifact_dir}, found {len(wheels)}"
        )
    return wheels[0].resolve()


def environment_python(environment: Path, platform: str = sys.platform) -> Path:
    """Return the environment interpreter path for *platform*."""
    if platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def environment_command(
    environment: Path, command: str, platform: str = sys.platform
) -> Path:
    """Return an installed console-script path for *platform*."""
    if platform == "win32":
        return environment / "Scripts" / f"{command}.exe"
    return environment / "bin" / command


def _run(*arguments: str | Path, cwd: Path, quiet: bool = False) -> None:
    subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def _create_environment(path: Path, wheel: Path, *, app: bool) -> Path:
    _run("uv", "venv", "--python", sys.executable, path, cwd=path.parent)
    python = environment_python(path)
    requirement = f"{wheel}[app]" if app else str(wheel)
    _run("uv", "pip", "install", "--python", python, requirement, cwd=path.parent)
    return python


def smoke(artifact_dir: Path) -> None:
    """Install and exercise the sole wheel without importing checkout source."""
    wheel = select_wheel(artifact_dir)
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hmcpctl-wheel-smoke-") as temporary:
        root = Path(temporary)
        library_environment = root / "library"
        library_python = _create_environment(library_environment, wheel, app=False)
        # Isolated mode keeps packaging assertions active under PYTHONOPTIMIZE.
        _run(library_python, "-I", "-c", LIBRARY_PROBE, cwd=root)

        app_environment = root / "app"
        app_python = _create_environment(app_environment, wheel, app=True)
        command = environment_command(app_environment, "hmcpctl")
        old_command = environment_command(app_environment, "hmc-mcp")
        if old_command.exists():
            raise RuntimeError(f"obsolete console command was installed: {old_command}")
        _run(command, "--help", cwd=root, quiet=True)
        _run(command, "capabilities", "--json", cwd=root, quiet=True)
        _run(app_python, project_root / "scripts" / "smoke_cli_groups.py", cwd=root)
        _run(app_python, project_root / "scripts" / "smoke_mcp.py", cwd=root)


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 1:
        print("usage: smoke_release_wheel.py ARTIFACT_DIR", file=sys.stderr)
        return 2
    try:
        smoke(Path(args[0]))
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"wheel smoke failed: {error}", file=sys.stderr)
        return 1
    print("wheel smoke: library and app environments passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
