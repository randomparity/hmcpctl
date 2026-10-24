import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_connection_authorization import lab_profile  # noqa: F401 — installs the named test profile
from test_target_authorization import _authorize
from typer.testing import CliRunner

from hmcpctl.authorization.target_scope import TargetScopeError
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import memory
from hmcpctl.documents.lpar_memory import LparMemoryMove
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle

TOOL = "hmc_move_lpar_memory"


@pytest.mark.parametrize(
    "outcome,code",
    [("applied", 0), ("noop", 0), ("partial", 1), ("rejected", 1), ("unverified", 1)],
)
def test_cli_move(monkeypatch, outcome, code):
    hmc = MagicMock()
    operation = AsyncMock(
        return_value={"outcome": outcome, "workflow_completed": code == 0}
    )
    monkeypatch.setattr(memory, "move_lpar_memory", operation)
    monkeypatch.setattr(memory, "with_client", lambda fn: asyncio.run(fn(hmc)))
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory-move",
            "source-R1",
            "target-R2",
            "--system",
            "sys-R1",
            "--quantity",
            "256",
            "--yes",
        ],
    )
    assert result.exit_code == code, result.output
    assert json.loads(result.output) == {
        "outcome": outcome,
        "workflow_completed": code == 0,
    }
    operation.assert_awaited_once_with(
        hmc,
        "source-R1",
        "target-R2",
        LparMemoryMove(quantity_mib=256),
        system_name_or_uuid="sys-R1",
        ownership_override=False,
    )


def test_cli_cancel_and_invalid_quantity(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(memory, "with_client", client)
    args = ["lpars", "memory-move", "source-R1", "target-R2", "--system", "sys-R1"]
    cancelled = CliRunner().invoke(app, [*args, "--quantity", "256"], input="n\n")
    assert "source-R1" in cancelled.output and "target-R2" in cancelled.output
    assert CliRunner().invoke(app, [*args, "--quantity", "-1", "--yes"]).exit_code != 0
    client.assert_not_called()


@pytest.mark.parametrize("profile,override", [(None, False), ("lab", True)])
def test_mcp_move(monkeypatch, profile, override):
    hmc = MagicMock()
    operation = AsyncMock(return_value={"workflow_completed": True})
    monkeypatch.setattr(lifecycle, "move_lpar_memory", operation)
    client = MagicMock(side_effect=lambda fn, **kw: asyncio.run(fn(hmc)))
    monkeypatch.setattr(lifecycle, "with_client", client)
    move = LparMemoryMove(quantity_mib=256, force=override)
    assert lifecycle.hmc_move_lpar_memory(
        "source-R1",
        "target-R2",
        "sys-R1",
        move,
        profile=profile,
        ownership_override=override,
    )["workflow_completed"]
    assert client.call_args.kwargs == {"profile": profile}
    operation.assert_awaited_once_with(
        hmc,
        "source-R1",
        "target-R2",
        move,
        system_name_or_uuid="sys-R1",
        ownership_override=override,
    )


@pytest.mark.parametrize("allowed", [["source-R1"], ["target-R2"], ["foreign"]])
def test_mcp_requires_both_targets(allowed):
    args = {
        "lpar_name_or_uuid": "source-R1",
        "target_lpar_name_or_uuid": "target-R2",
        "system_name_or_uuid": "sys-R1",
        "profile": "lab",
        "move": LparMemoryMove(quantity_mib=256),
        "ownership_override": False,
    }
    grant = {
        "tools": [TOOL],
        "connections": ["lab"],
        "targets": {"managed_system": ["sys-R1"], "lpar": allowed},
    }
    with pytest.raises(TargetScopeError):
        _authorize([grant], TOOL, args)
    grant["targets"]["lpar"] = ["source-R1", "target-R2"]
    _authorize([grant], TOOL, args)


def test_split_grants_cannot_combine_targets():
    args = {
        "lpar_name_or_uuid": "source-R1",
        "target_lpar_name_or_uuid": "target-R2",
        "system_name_or_uuid": "sys-R1",
        "profile": "lab",
        "move": LparMemoryMove(quantity_mib=256),
        "ownership_override": False,
    }
    grants = [
        {
            "tools": [TOOL],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-R1"], "lpar": [name]},
        }
        for name in ("source-R1", "target-R2")
    ]
    with pytest.raises(TargetScopeError):
        _authorize(grants, TOOL, args)


def test_move_schema():
    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy

    application = create_mcp(
        compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    )
    tool = next(t for t in asyncio.run(application.list_tools()) if t.name == TOOL)
    assert {
        "lpar_name_or_uuid",
        "target_lpar_name_or_uuid",
        "system_name_or_uuid",
        "move",
    } <= set(tool.parameters["required"])
    assert tool.parameters["properties"]["move"]["additionalProperties"] is False
    security = TOOL_SECURITY[TOOL]
    assert {(t.kind, t.argument) for t in security.targets} == {
        ("lpar", "lpar_name_or_uuid"),
        ("lpar", "target_lpar_name_or_uuid"),
        ("managed_system", "system_name_or_uuid"),
    }
    assert security.exhaustive_targets and security.operation == "lpar.move_memory"


@pytest.mark.parametrize("selector", [0, 1, 2])
def test_mcp_move_rejects_blank_selector(monkeypatch, selector):
    client = MagicMock()
    monkeypatch.setattr(lifecycle, "with_client", client)
    selectors = ["source-R1", "target-R2", "sys-R1"]
    selectors[selector] = " "
    with pytest.raises(
        ValueError, match="requires source, destination and managed system"
    ):
        lifecycle.hmc_move_lpar_memory(*selectors, LparMemoryMove(quantity_mib=256))
    client.assert_not_called()


@pytest.mark.parametrize("selector", [0, 1, 2])
def test_cli_move_rejects_blank_selector(monkeypatch, selector):
    client = MagicMock()
    monkeypatch.setattr(memory, "with_client", client)
    selectors = ["source-R1", "target-R2", "sys-R1"]
    selectors[selector] = " "
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory-move",
            selectors[0],
            selectors[1],
            "--system",
            selectors[2],
            "--quantity",
            "256",
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "must be nonblank" in result.output
    client.assert_not_called()


def test_cli_move_accepts_confirmation_and_explicit_intent(monkeypatch):
    hmc = MagicMock()
    operation = AsyncMock(
        return_value={"outcome": "applied", "workflow_completed": True}
    )
    monkeypatch.setattr(memory, "move_lpar_memory", operation)
    monkeypatch.setattr(memory, "with_client", lambda fn: asyncio.run(fn(hmc)))
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "memory-move",
            "source-R1",
            "target-R2",
            "--system",
            "sys-R1",
            "--quantity",
            "256",
            "--force",
            "--ownership-override",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "source-R1" in result.output and "target-R2" in result.output
    assert '"workflow_completed": true' in result.output
    operation.assert_awaited_once_with(
        hmc,
        "source-R1",
        "target-R2",
        LparMemoryMove(quantity_mib=256, force=True),
        system_name_or_uuid="sys-R1",
        ownership_override=True,
    )


@pytest.mark.parametrize(
    "width,rich_panels", [(40, False), (61, False), (62, True), (80, True)]
)
def test_memory_move_discovery_is_bounded_in_real_cli(width, rich_panels, tmp_path):
    import os
    import subprocess
    import sys

    from rich.text import Text

    result = subprocess.run(
        [sys.executable, "-c", "from hmcpctl.cli import app; app()", "lpars", "--help"],
        cwd=tmp_path,
        env={**os.environ, "COLUMNS": str(width)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    help_text = Text.from_ansi(result.stdout).plain
    assert len(help_text) < 20000
    assert "memory-move" in help_text and "security-policy" in help_text
    assert ("╭" in help_text) is rich_panels


def test_memory_move_confirmation_encodes_terminal_controls(monkeypatch):
    import io
    import sys

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", Terminal())
    prompts = []
    monkeypatch.setattr(
        memory.typer, "confirm", lambda prompt: prompts.append(prompt) or False
    )
    client = MagicMock()
    monkeypatch.setattr(memory, "with_client", client)
    memory.lpars_memory_move(
        "source\x1b[31m", "target\nR2", "sys-R1", 256, False, False, False
    )
    assert "Move 256 MiB" in prompts[0]
    assert "source\\x1b[31m" in prompts[0] and "target\\nR2" in prompts[0]
    assert "\x1b" not in prompts[0] and "\n" not in prompts[0]
    client.assert_not_called()
