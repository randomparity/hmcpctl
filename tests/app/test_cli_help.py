"""Offline CLI discovery and error recovery contracts (#1702)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import asyncssh
import pytest
import typer
from click import unstyle
from typer.main import get_command
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.cli_commands import output, runtime
from hmcpctl.resource_identity import ResourceNotFoundError, resolve_system_uuid

runner = CliRunner()


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("help or parser rejection attempted an HMC call")

    monkeypatch.setattr(runtime, "client", forbidden)
    monkeypatch.setattr(asyncssh, "connect", forbidden)


@pytest.mark.parametrize("color", [False, True])
def test_task_help_leads_with_inventory_and_explains_root_options(offline, color):
    for group, panels in (
        ("lpars", ["Inventory", "Lifecycle", "Console", "Configuration"]),
        ("storage", ["Inventory", "Disks", "Optical media", "Mappings"]),
    ):
        result = runner.invoke(app, [group, "--help"], color=color)
        text = unstyle(result.stdout)
        assert result.exit_code == 0
        assert text.index("Inventory") < min(text.index(panel) for panel in panels[1:])
        if group == "lpars":
            assert text.index("Console") < text.index("Configuration")
        for panel in panels:
            assert panel in text
        assert "hmcpctl --profile NAME" in text
        assert "before" in text


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize(
    ("args", "missing", "present"),
    [
        (["lpars", "create"], ["name", "--system"], []),
        (["lpars", "create", "sample"], ["--system"], ["'name'"]),
        (["lpars", "refcodes"], ["system_name", "lpar_name"], []),
        (["storage", "create-disk"], ["vios", "--vg", "--name", "--capacity-mib"], []),
        (
            ["storage", "create-disk", "sample", "--vg", "pool"],
            ["--name", "--capacity-mib"],
            ["'vios'", "'--vg'"],
        ),
    ],
)
def test_missing_inputs_are_reported_together(offline, args, missing, present, color):
    result = runner.invoke(app, args, color=color)
    text = unstyle(result.stderr)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in text and "--help" in text
    assert "╭" not in text
    error = text.split("Error:", 1)[1]
    for name in missing:
        assert name in error
    for name in present:
        assert name not in error


@pytest.mark.parametrize("args", [["--unknown"], ["lpars", "create", "--unknown"]])
def test_unknown_options_do_not_trigger_missing_input_validation(offline, args):
    result = runner.invoke(app, args)
    text = unstyle(result.stderr)
    assert result.exit_code == 2
    assert "No such option" in text
    assert "Missing" not in text and "╭" not in text
    assert "Usage:" in text and "--help" in text


@pytest.mark.parametrize(
    "args", [["lpars", "--profile", "sample"], ["lpars", "list", "--profile", "sample"]]
)
def test_misplaced_profile_has_root_placement_repair(offline, args):
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "hmcpctl --profile NAME GROUP COMMAND" in unstyle(result.stderr)


def test_command_typo_preserves_suggestion(offline):
    result = runner.invoke(app, ["lpars", "lis"])
    assert result.exit_code == 2
    assert "Did you mean 'list'?" in unstyle(result.stderr)


def test_invalid_value_keeps_its_error(offline):
    result = runner.invoke(app, ["storage", "create-disk", "--capacity-mib", "bad"])
    assert result.exit_code == 2
    assert "Invalid value" in unstyle(result.stderr)
    assert "Missing" not in unstyle(result.stderr)


@pytest.mark.parametrize("group", list(get_command(app).commands))
def test_group_help_is_offline(offline, group):
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("kind", "command"),
    [("managed system", "systems list"), ("LPAR", "lpars list"), ("VIOS", "vios list")],
)
def test_cli_translates_typed_lookup_without_changing_domain_error(
    capsys, kind, command
):
    original = f"Use hmc_list_{command.split()[0]} to find a resource."
    error = ResourceNotFoundError(kind, "missing", original)
    with pytest.raises(typer.Exit) as caught:
        output.fail(error)
    assert caught.value.exit_code == 1
    text = unstyle(capsys.readouterr().err)
    assert f"hmcpctl {command}" in text
    assert "missing" in text and "hmc_list_" not in text
    assert str(error) == original


@pytest.mark.asyncio
async def test_domain_resolution_keeps_mcp_recovery():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = []
    with pytest.raises(ResourceNotFoundError, match="hmc_list_systems"):
        await resolve_system_uuid(hmc, "missing")


def test_partition_not_found_offers_cli_inventory(capsys):
    with pytest.raises(typer.Exit):
        output.partition_not_found("missing")
    assert "hmcpctl lpars list" in capsys.readouterr().err


def test_failed_inventory_read_is_not_reported_as_empty(monkeypatch):
    from hmcpctl.cli_commands.systems import core

    async def fail_read(*args, **kwargs):
        raise RuntimeError("inventory read failed")

    monkeypatch.setattr(
        core, "with_client", lambda fn: runtime.run_cli_coroutine(lambda: fail_read())
    )
    result = runner.invoke(app, ["systems", "list"])
    assert result.exit_code == 1
    assert "inventory read failed" in result.stderr
    assert "No results" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--profile", "sample", "systems", "list"],
        ["--profile", "sample", "lpars", "list", "--system", "system1"],
        ["--profile", "sample", "vios", "list"],
        ["--profile", "sample", "storage", "list-vgs", "vios1", "--system", "system1"],
    ],
)
def test_documented_examples_parse_without_execution(offline, args):
    root = get_command(app)
    with root.make_context("hmcpctl", args) as ctx:
        group_name = ctx._protected_args[0]
        group = root.commands[group_name]
        with group.make_context(group_name, ctx.args, parent=ctx) as group_ctx:
            name = group_ctx._protected_args[0]
            with group.commands[name].make_context(
                name, group_ctx.args, parent=group_ctx
            ):
                pass


@pytest.mark.parametrize(
    ("args", "code"), [(["lpars", "--help"], 0), (["storage", "create-disk"], 2)]
)
def test_installed_entry_point_help_and_errors(args, code):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("hmcpctl")), *args],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == code
    text = unstyle(result.stdout if code == 0 else result.stderr)
    assert "Usage:" in text
    if code == 2:
        assert "--vg" in text and "--capacity-mib" in text
        assert "╭" not in text


def test_aggregation_respects_sources_without_callbacks_or_callable_defaults(
    monkeypatch,
):
    from hmcpctl.cli_commands.app import CLICommand, CLIGroup

    calls = []
    local = typer.Typer(cls=CLIGroup)

    @local.callback()
    def root():
        pass

    @local.command(cls=CLICommand)
    def selected(
        name: str,
        source: str = typer.Option(..., envvar="TEST_REQUIRED_SOURCE"),
        mapped: str = typer.Option(...),
        dynamic: str = typer.Option(...),
        other: str = typer.Option(..., callback=lambda value: calls.append(value)),
    ):
        pytest.fail("invalid input executed the command")

    command = get_command(local)
    dynamic = next(
        p for p in command.commands["selected"].params if p.name == "dynamic"
    )
    dynamic.default = lambda: calls.append("default")
    monkeypatch.setenv("TEST_REQUIRED_SOURCE", "supplied")
    # Use the already-composed command so its callable-default metadata is retained.
    from click.testing import CliRunner as ClickRunner

    result = ClickRunner().invoke(
        command, ["selected"], default_map={"selected": {"mapped": "supplied"}}
    )
    assert result.exit_code == 2
    error = unstyle(result.stderr).split("Error:", 1)[1]
    assert "'name'" in error and "--other" in error
    assert (
        "--source" not in error and "--mapped" not in error and "--dynamic" not in error
    )
    assert calls == []


@pytest.mark.parametrize("args", [[], ["lpars"], ["storage"]])
def test_no_argument_help_remains_help(offline, args):
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "Usage:" in unstyle(result.output)
    assert "Traceback" not in result.output


def test_unknown_resource_kind_retains_original_failure(capsys):
    with pytest.raises(typer.Exit):
        output.fail(
            ResourceNotFoundError("other", "missing", "specific lookup failure")
        )
    assert "specific lookup failure" in capsys.readouterr().err
