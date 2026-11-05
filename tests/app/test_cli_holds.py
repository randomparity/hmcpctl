"""CLI side of ADR 0193 holds: mirrored tools, --hold-id, operator commands (#1851)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import typer
from conftest import mock_hmc, mock_uuid_resolution  # noqa: F401 - mock_hmc is a fixture
from typer import _click
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.cli_commands.tool_mirrors import CLI_TOOL_MIRRORS
from hmcpctl.operations.logical import holds
from hmcpctl.server_tools.catalog import TOOL_SECURITY
from hmcpctl.tool_registry import hold_guarded

RUNNER = CliRunner()
SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"


def _leaves() -> dict[str, _click.Context]:
    """Every registered command, by path below the root, with its context chain."""
    root = typer.main.get_command(app)
    found: dict[str, _click.Context] = {}

    def walk(command: Any, ctx: _click.Context, path: list[str]) -> None:
        if hasattr(command, "commands"):
            for name, sub in command.commands.items():
                walk(
                    sub, _click.Context(sub, parent=ctx, info_name=name), [*path, name]
                )
        else:
            found[" ".join(path)] = ctx

    walk(root, _click.Context(root, info_name="hmcpctl"), [])
    return found


def test_every_command_names_its_mirrored_tool():
    leaves = _leaves()
    assert set(leaves) == set(CLI_TOOL_MIRRORS)
    unknown = {tool for tool in CLI_TOOL_MIRRORS.values() if tool} - set(TOOL_SECURITY)
    assert unknown == set()


def test_hold_id_is_exactly_on_commands_mirroring_hold_guarded_tools():
    for path, ctx in _leaves().items():
        tool = CLI_TOOL_MIRRORS[path]
        names = {param.name for param in ctx.command.get_params(ctx)}
        expected = tool is not None and hold_guarded(tool, TOOL_SECURITY[tool])
        assert ("hold_id" in names) == expected, path


def _place() -> holds.Hold:
    return holds.place_hold(
        SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c"
    )


def test_holds_list_and_release_stay_local_and_hide_the_hold_id():
    placed = _place()
    listed = RUNNER.invoke(app, ["holds", "list"])
    assert listed.exit_code == 0, listed.output
    [row] = json.loads(listed.stdout)["holds"]
    assert row["partition_uuid"] == LPAR and "hold_id" not in row
    assert placed.hold_id not in listed.stdout

    released = RUNNER.invoke(app, ["holds", "release", LPAR.upper()])
    assert released.exit_code == 0, released.output
    assert json.loads(released.stdout)["released"][0]["consumer_label"] == "kdive"
    assert holds.list_holds() == ()
    again = RUNNER.invoke(app, ["holds", "release", LPAR])
    assert json.loads(again.stdout) == {"released": []}


def test_holds_release_refuses_a_non_uuid():
    result = RUNNER.invoke(app, ["holds", "release", "lpar-a"])
    assert result.exit_code == 1
    assert "not a partition UUID" in result.output


@pytest.fixture
def hmc(monkeypatch, mock_hmc):  # noqa: F811 - the conftest fixture
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    mock_uuid_resolution(mock_hmc, SYS, "sys-R1", LPAR, "lpar-a")
    mock_hmc.route(method__in=["POST", "PUT", "DELETE"]).mock(
        return_value=httpx.Response(500, text="write reached")
    )
    return mock_hmc


def _power_on(*extra: str) -> Any:
    return RUNNER.invoke(
        app, ["lpars", "power-on", LPAR, "--system", SYS, "--yes", *extra]
    )


def test_held_partition_refuses_a_cli_mutation_without_its_hold_id(hmc):
    _place()
    result = _power_on()
    assert result.exit_code == 1
    assert "partition_held" in result.output


def test_matching_hold_id_lets_the_cli_mutation_past_the_hold(hmc):
    placed = _place()
    state = hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR}/quick/PartitionState").mock(
        return_value=httpx.Response(503, text="state read")
    )
    result = _power_on("--hold-id", placed.hold_id)
    assert "partition_held" not in result.output
    assert state.called, result.output


def test_cli_reads_of_a_held_partition_pass(hmc):
    _place()
    result = RUNNER.invoke(app, ["lpars", "show", LPAR, "--system", SYS])
    assert "partition_held" not in result.output


@pytest.mark.parametrize(
    "command,extra",
    [
        ("power-on", ["--yes"]),
        ("power-off", ["--yes"]),
        ("profile-activate", ["profile-a", "--action", "validate"]),
    ],
)
def test_system_cli_mirrors_refuse_held_system(hmc, command, extra):
    from conftest import assert_no_mutating_requests

    from hmcpctl.call_context import current_call

    _place()
    result = RUNNER.invoke(app, ["systems", command, "sys-R1", *extra])
    assert result.exit_code == 1, result.output
    assert "system_held" in result.output
    assert current_call() is None
    assert_no_mutating_requests(hmc)
