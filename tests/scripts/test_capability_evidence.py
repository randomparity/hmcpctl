"""Consumers import shared evidence without loading the inventory command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "consumer",
    [
        "live_test.evidence",
        "live_test.runner",
        "promote_live_observations",
        "run_release_window",
    ],
)
def test_consumer_import_does_not_load_inventory_command(consumer: str) -> None:
    program = """
import importlib
import importlib.abc
import sys

class RejectInventoryCommand(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'check_capability_inventory':
            raise ImportError('shared evidence must not load the inventory command')

sys.meta_path.insert(0, RejectInventoryCommand())
sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT / "scripts"), consumer],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
