"""C01 presentation forwarding and truthful exits."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar import LparSettings
from hmcpctl.operations.lpar.configuration import LparSettingsResult
from hmcpctl.server_tools.lpar.configuration import (
    hmc_configure_lpar,
    hmc_get_lpar_settings,
)
from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar


def test_mcp_settings_and_create_forward_false_zero(monkeypatch):
    from hmcpctl.server_tools.lpar import configuration, lifecycle_create

    settings = LparSettings(False, 0, False, False, False)
    result = LparSettingsResult("verified", settings, settings, ())
    configure = AsyncMock(return_value=result)
    read = AsyncMock(return_value=settings)
    create = AsyncMock(return_value="created")
    monkeypatch.setattr(configuration, "configure_lpar_settings", configure)
    monkeypatch.setattr(configuration, "read_lpar_settings", read)
    monkeypatch.setattr(
        configuration, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
    )
    monkeypatch.setattr(lifecycle_create, "create_lpar", create)
    monkeypatch.setattr(
        lifecycle_create, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
    )
    assert hmc_configure_lpar("s", "l", settings) == result
    assert configure.await_args.args[2] == settings
    assert hmc_get_lpar_settings("s", "l") == settings
    hmc_create_lpar("s", "l", settings=settings)
    assert create.await_args.args[2].settings == settings


def test_cli_settings_and_outcome_exits(monkeypatch):
    from hmcpctl.cli_commands.lpar import config

    settings = LparSettings(allow_perf_collection=False, lpar_avail_priority=0)
    result = LparSettingsResult("verified", settings, settings, ())
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(config, "configure_lpar_settings", call)
    monkeypatch.setattr(config, "with_client", lambda f: asyncio.run(f("hmc")))
    args = [
        "lpars",
        "configure",
        "l",
        "s",
        "--no-allow-perf-collection",
        "--lpar-avail-priority",
        "0",
    ]
    run = CliRunner().invoke(app, args)
    assert run.exit_code == 0, run.output
    assert call.await_args.args[2] == settings
    for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
        call.return_value = replace(result, status=status)
        assert CliRunner().invoke(app, args).exit_code == 1


def test_cli_create_settings_forwarding(monkeypatch):
    from hmcpctl.cli_commands.lpar import create
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult

    call = AsyncMock(
        return_value=LparPcieWorkflowResult(True, True, {"UUID": "new"}, True, (), ())
    )
    monkeypatch.setattr(create, "create_lpar", call)
    monkeypatch.setattr(create, "with_client", lambda f: asyncio.run(f("hmc")))
    run = CliRunner().invoke(
        app,
        [
            "lpars",
            "create",
            "new",
            "--system",
            "s",
            "--yes",
            "--no-migration-disabled",
            "--lpar-avail-priority",
            "0",
        ],
    )
    assert run.exit_code == 0, run.output
    assert call.await_args.args[2].settings == LparSettings(
        lpar_avail_priority=0, migration_disabled=False
    )
