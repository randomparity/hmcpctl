"""Capability owners load independently of partition creation."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("owner", ["configuration", "inventory", "activation"])
def test_capability_import_does_not_load_creation(owner):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from importlib.abc import MetaPathFinder\n"
                "class BlockCreation(MetaPathFinder):\n"
                " def find_spec(self, fullname, path=None, target=None):\n"
                "  if fullname == 'hmcpctl.operations.lpar.core':\n"
                "   raise ImportError('creation dependency forbidden')\n"
                "sys.meta_path.insert(0, BlockCreation())\n"
                f"import hmcpctl.operations.lpar.{owner}\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
