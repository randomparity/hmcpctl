"""C03 presenters keep strict consent, target types and creation forwarding."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings, VtpmState
from hmcpctl.operations.lpar.vtpm import VtpmResult
from hmcpctl.server_tools.lpar.configuration import (
    hmc_configure_lpar_vtpm,
    hmc_get_lpar_vtpm_settings,
)
from hmcpctl.server_tools.vios.core import (
    hmc_configure_vios_vtpm,
    hmc_get_vios_vtpm_settings,
)

SETTINGS = LparVtpmSettings(False)
RESULT = VtpmResult("verified", SETTINGS, SETTINGS, ())


@pytest.mark.parametrize("kind", ["lpar", "vios"])
def test_mcp_target_specific_update_and_read_forward_strict_consent(monkeypatch, kind):
    from hmcpctl.server_tools.lpar import configuration
    from hmcpctl.server_tools.vios import core

    module = core if kind == "vios" else configuration
    update = AsyncMock(return_value=RESULT)
    read = AsyncMock(
        return_value=VtpmState(
            "vioserver" if kind == "vios" else "aixlinux", "Not Activated", SETTINGS
        )
    )
    monkeypatch.setattr(module, "configure_lpar_vtpm", update)
    monkeypatch.setattr(module, "read_lpar_vtpm", read)
    monkeypatch.setattr(module, "with_client", lambda f, **kw: asyncio.run(f("hmc")))
    setter = hmc_configure_vios_vtpm if kind == "vios" else hmc_configure_lpar_vtpm
    getter = (
        hmc_get_vios_vtpm_settings if kind == "vios" else hmc_get_lpar_vtpm_settings
    )
    assert setter("sys-R1", "target-R1", SETTINGS, acknowledge_data_loss=True) == RESULT
    assert update.await_args.kwargs["acknowledge_data_loss"] is True
    assert update.await_args.kwargs["ownership_override"] is False
    assert update.await_args.kwargs.get("vios", False) is (kind == "vios")
    getter("sys-R1", "target-R1")
    assert read.await_args.kwargs.get("vios", False) is (kind == "vios")


def test_mcp_metadata_separates_lpar_and_vios_targets():
    from hmcpctl.server import TOOL_SECURITY

    for kind in ("lpar", "vios"):
        security = TOOL_SECURITY[f"hmc_configure_{kind}_vtpm"]
        assert security.effect == "destructive"
        assert security.target_kind == kind
        assert any(
            t.kind == kind and t.argument == f"{kind}_name_or_uuid"
            for t in security.targets
        )
        assert TOOL_SECURITY[f"hmc_get_{kind}_vtpm_settings"].effect == "read"


@pytest.mark.parametrize("group", ["lpars", "vios"])
def test_cli_update_forwards_consented_false_and_exits_nonzero_on_uncertainty(
    monkeypatch, group
):
    from hmcpctl.cli_commands.lpar import config
    from hmcpctl.cli_commands.vios import core

    module = core if group == "vios" else config
    call = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(module, "configure_lpar_vtpm", call)
    monkeypatch.setattr(module, "with_client", lambda f: asyncio.run(f("hmc")))
    args = [
        group,
        "configure-vtpm",
        "target-R1",
        "sys-R1",
        "--no-vtpm-enabled",
        "--acknowledge-data-loss",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert call.await_args.args[2] == SETTINGS
    assert call.await_args.kwargs["acknowledge_data_loss"] is True
    assert call.await_args.kwargs["ownership_override"] is False
    assert call.await_args.kwargs.get("vios", False) is (group == "vios")
    for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
        call.return_value = replace(RESULT, status=status)
        assert CliRunner().invoke(app, args).exit_code == 1


def test_mcp_and_cli_creation_forward_all_three_fields_to_existing_owners(monkeypatch):
    from hmcpctl.cli_commands.lpar import create
    from hmcpctl.cli_commands.vios import core as vios_cli
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.server_tools.lpar import lifecycle_create
    from hmcpctl.server_tools.vios import core as vios_tools

    settings = LparVtpmSettings(True, "2.0", "Power10v2")
    workflow = LparPcieWorkflowResult(True, True, {"UUID": "created"}, True, (), ())
    call = AsyncMock(return_value=workflow)
    vios = AsyncMock(return_value={"UUID": "created-vios"})
    for module in (lifecycle_create, create):
        monkeypatch.setattr(module, "create_lpar", call)
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    for module in (vios_tools, vios_cli):
        monkeypatch.setattr(module, "create_vios", vios)
        monkeypatch.setattr(
            module, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
        )
    lifecycle_create.hmc_create_lpar("sys-R1", "p", vtpm_settings=settings)
    assert call.await_args.args[2].vtpm_settings == settings
    vios_tools.hmc_create_vios("sys-R1", "vios-R1", vtpm_settings=settings)
    assert vios.await_args.kwargs["vtpm_settings"] == settings
    args = [
        "lpars",
        "create",
        "p",
        "--system",
        "sys-R1",
        "--vtpm-enabled",
        "--vtpm-version",
        "2.0",
        "--vtpm-encryption",
        "Power10v2",
        "--yes",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert call.await_args.args[2].vtpm_settings == settings
    result = CliRunner().invoke(
        app,
        [
            "vios",
            "create",
            "vios-R1",
            "--system",
            "sys-R1",
            "--vtpm-settings",
            '{"vtpm_enabled":true,"vtpm_version":"2.0","vtpm_encryption":"Power10v2"}',
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert vios.await_args.kwargs["vtpm_settings"] == settings


def test_vios_create_bad_typed_json_and_declined_confirmation_have_no_call(monkeypatch):
    from hmcpctl.cli_commands.vios import core

    call = AsyncMock()
    monkeypatch.setattr(core, "create_vios", call)
    monkeypatch.setattr(core, "with_client", lambda f: asyncio.run(f("hmc")))
    for value in ('{"unknown":1}', '{"vtpm_enabled":"true"}', "[]"):
        result = CliRunner().invoke(
            app,
            [
                "vios",
                "create",
                "vios-R1",
                "--system",
                "sys-R1",
                "--vtpm-settings",
                value,
                "--yes",
            ],
        )
        assert result.exit_code != 0
    result = CliRunner().invoke(
        app, ["vios", "create", "vios-R1", "--system", "sys-R1"], input="n\n"
    )
    assert result.exit_code != 0
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_served_mcp_rejects_coerced_consent_and_unknown_settings_before_handler(
    monkeypatch,
):
    from fastmcp import Client, FastMCP

    from hmcpctl.server_tools.lpar import configuration

    reached = []
    monkeypatch.setattr(
        configuration, "with_client", lambda *args, **kw: reached.append(True)
    )
    server = FastMCP("vtpm-input-boundary")
    server.tool(hmc_configure_lpar_vtpm)
    async with Client(server) as client:
        for invalid in (1, "true", None):
            result = await client.call_tool(
                "hmc_configure_lpar_vtpm",
                {
                    "system_name_or_uuid": "sys-R1",
                    "lpar_name_or_uuid": "p",
                    "settings": {"vtpm_enabled": False},
                    "acknowledge_data_loss": invalid,
                },
                raise_on_error=False,
            )
            assert result.is_error
        result = await client.call_tool(
            "hmc_configure_lpar_vtpm",
            {
                "system_name_or_uuid": "sys-R1",
                "lpar_name_or_uuid": "p",
                "settings": {"unexpected": True},
            },
            raise_on_error=False,
        )
        assert result.is_error
    assert reached == []
