"""Import-order regressions for operation-module dependency boundaries."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "modules",
    [
        ("hmcpctl.operations.lpar", "hmcpctl.operations.virtualization.vnic"),
        ("hmcpctl.operations.virtualization.vnic", "hmcpctl.operations.lpar"),
        ("hmcpctl.ssh.lpar", "hmcpctl.ssh.profiles"),
        ("hmcpctl.ssh.profiles", "hmcpctl.ssh.lpar"),
    ],
)
def test_sibling_modules_import_in_either_order(
    modules: tuple[str, str],
) -> None:
    imports = "; ".join(f"import {module}" for module in modules)

    completed = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "module", ["hmcpctl.operations.logical.store", "hmcpctl.operations.logical.engine"]
)
def test_logical_operations_import_without_application_dependencies(module):
    script = f"""
import importlib
import importlib.abc
import sys

class NoApplicationPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'fastmcp', 'mcp', 'typer', 'rich'}}:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, NoApplicationPackages())
importlib.import_module({module!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
