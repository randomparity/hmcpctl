"""C02 presentation forwarding and truthful failures."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar import LparReportingSettings
from hmcpctl.operations.lpar.configuration import LparReportingSettingsResult
from hmcpctl.server_tools.lpar.configuration import (
    hmc_configure_lpar_reporting,
    hmc_get_lpar_reporting_settings,
)


def test_mcp_read_update_create_forward_values(monkeypatch):
    from hmcpctl.server_tools.lpar import configuration, lifecycle_create

    settings = LparReportingSettings(
        work_group_id="none", electronic_err_reporting=False
    )
    result = LparReportingSettingsResult("verified", settings, settings, ())
    call = AsyncMock(return_value=result)
    read = AsyncMock(return_value=settings)
    create = AsyncMock(return_value="created")
    monkeypatch.setattr(configuration, "configure_lpar_reporting_settings", call)
    monkeypatch.setattr(configuration, "read_lpar_reporting_settings", read)
    for module in (configuration, lifecycle_create):
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    monkeypatch.setattr(lifecycle_create, "create_lpar", create)
    assert hmc_configure_lpar_reporting("s", "l", settings) == result
    assert call.await_args.args[2] == settings
    assert hmc_get_lpar_reporting_settings("s", "l") == settings
    lifecycle_create.hmc_create_lpar(
        "s", "l", partition_type="OS400", reporting_settings=settings
    )
    assert create.await_args.args[2].reporting_settings == settings


def test_cli_update_and_create(monkeypatch):
    from hmcpctl.cli_commands.lpar import config, create
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult

    settings = LparReportingSettings(
        ipl_source="b", work_group_id=0, os400_restricted_io_mode=False
    )
    result = LparReportingSettingsResult("verified", settings, settings, ())
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(config, "configure_lpar_reporting_settings", call)
    monkeypatch.setattr(config, "with_client", lambda f: asyncio.run(f("hmc")))
    args = [
        "lpars",
        "configure-reporting",
        "l",
        "s",
        "--ipl-source",
        "b",
        "--work-group-id",
        "0",
        "--no-os400-restricted-io-mode",
    ]
    run = CliRunner().invoke(app, args)
    assert run.exit_code == 0, run.output
    assert call.await_args.args[2] == settings
    for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
        call.return_value = replace(result, status=status)
        assert CliRunner().invoke(app, args).exit_code == 1
    make = AsyncMock(
        return_value=LparPcieWorkflowResult(True, True, {"UUID": "new"}, True, (), ())
    )
    monkeypatch.setattr(create, "create_lpar", make)
    monkeypatch.setattr(create, "with_client", lambda f: asyncio.run(f("hmc")))
    run = CliRunner().invoke(
        app,
        [
            "lpars",
            "create",
            "new",
            "--system",
            "s",
            "--type",
            "OS400",
            "--yes",
            "--work-group-id",
            "none",
            "--no-electronic-err-reporting",
            "--no-redundant-err-path-reporting",
        ],
    )
    assert run.exit_code == 0, run.output
    assert make.await_args.args[2].reporting_settings == LparReportingSettings(
        work_group_id="none",
        electronic_err_reporting=False,
        redundant_err_path_reporting=False,
    )
