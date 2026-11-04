"""Direct tests for the MCP stdio smoke script."""

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from hmcpctl.server import PRIMARY_TOOLS

ROOT = Path(__file__).parents[2]


def _main() -> Callable[[list[str] | None], None]:
    namespace = runpy.run_path(str(ROOT / "scripts" / "smoke_mcp.py"))
    return cast(Callable[[list[str] | None], None], namespace["main"])


def test_main_reports_only_the_tool_count_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _main()([])

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Connected. ")
    assert lines[0].endswith(" tools exposed.")
    assert "  - " not in output


def test_main_lists_the_default_listing_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke leg serves what a client sees: the primary catalog (ADR 0330)."""
    _main()(["--verbose"])

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Connected. {len(PRIMARY_TOOLS)} tools exposed:"
    assert {line.removeprefix("  - ") for line in lines[1:]} == PRIMARY_TOOLS
