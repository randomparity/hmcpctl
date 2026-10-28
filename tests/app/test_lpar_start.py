"""Direct CLI/MCP creation and boot surfaces retain their existing boundaries."""

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest
from app.test_app_lpar_affinity_group import adapter, application  # noqa: F401 — shared boundary fixture
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create, profiles
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.server import TOOL_SECURITY
from hmcpctl.server_tools.lpar import lifecycle_boot
from hmcpctl.server_tools.vios import core as vios_tools


def forwarded(boundary):
    call = boundary.operation.await_args
    return (
        call.args[2].start_settings
        if boundary.kind == "lpar"
        else call.kwargs["start_settings"]
    )


@pytest.mark.asyncio
async def test_creation_presentations_and_alltargets(adapter):  # noqa: F811 — imported pytest fixture
    async with Client(application(adapter.name)) as client:
        result = await client.call_tool(
            adapter.name,
            {
                "system_name_or_uuid": "sys-R1",
                "name": "new",
                "start_settings": {"auto_start": False, "power_ctrl_lpar_ids": [7]},
            },
        )
        assert not result.is_error
        assert forwarded(adapter) == LparStartSettings(
            auto_start=False, power_ctrl_lpar_ids=(7,)
        )
        assert TOOL_SECURITY[adapter.name].exhaustive_targets is False
        adapter.operation.reset_mock()
        adapter.connections.clear()
        for invalid in (
            {"auto_start": 0},
            {"power_ctrl_lpar_ids": [True]},
            {"unknown": 1},
        ):
            result = await client.call_tool(
                adapter.name,
                {
                    "system_name_or_uuid": "sys-R1",
                    "name": "new",
                    "start_settings": invalid,
                },
                raise_on_error=False,
            )
            assert result.is_error
        adapter.operation.assert_not_awaited()
        assert adapter.connections == []
    from hmcpctl.authorization.access_policy import AccessPolicyError

    with pytest.raises(AccessPolicyError, match="has no target selector"):
        application(adapter.name, targets={"managed_system": ["sys-R1"]})


def test_creation_cli_false_and_invalid_before_client(adapter):  # noqa: F811 — imported pytest fixture
    args = [
        adapter.group,
        "create",
        "new",
        "--system",
        "sys-R1",
        "--yes",
        "--start-settings",
    ]
    result = CliRunner().invoke(app, args + ['{"auto_start":false}'])
    assert result.exit_code == 0, result.output
    assert forwarded(adapter) == LparStartSettings(auto_start=False)
    adapter.operation.reset_mock()
    adapter.connections.clear()
    result = CliRunner().invoke(app, args + ['{"auto_start":0}'])
    assert result.exit_code != 0
    adapter.operation.assert_not_awaited()
    assert adapter.connections == []


def test_legacy_yes_and_boot_output(monkeypatch):
    for fn, index in [(create.lpars_create, 34), (vios_cli.vios_create, 5)]:
        assert list(inspect.signature(fn).parameters).index("yes") == index
    operation = AsyncMock(return_value=None)
    monkeypatch.setattr(profiles, "set_lpar_boot_order", operation)
    monkeypatch.setattr(profiles, "with_client", lambda fn: asyncio.run(fn(object())))
    text = "  λ raw  "
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "set-boot-order",
            "sys-R1",
            "target-R1",
            "--boot-string",
            text,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert operation.await_args.kwargs["boot_string"] == text
    assert operation.await_args.kwargs["devices"] is None
    assert "null" in result.output


def test_boot_raw_validation_precedes_client(monkeypatch):
    for module, name in [
        (lifecycle_boot, "hmc_set_lpar_boot_order"),
        (vios_tools, "hmc_set_vios_boot_order"),
    ]:
        opened = []
        monkeypatch.setattr(
            module, "with_client", lambda *a, opened=opened, **k: opened.append(True)
        )
        with pytest.raises(ValueError):
            getattr(module, name)("sys-R1", "target-R1", boot_string="\n")
        assert opened == []


def test_vios_boot_presentations_and_metadata(monkeypatch):
    expected = {
        "vios_uuid": "target-R1",
        "vios_name": "vios-R1",
        "pending_boot_string": "raw",
    }
    setter = AsyncMock(return_value=expected)
    for module in [vios_cli, vios_tools]:
        monkeypatch.setattr(module, "set_vios_boot_order", setter)
        monkeypatch.setattr(
            module, "with_client", lambda fn, **kwargs: asyncio.run(fn(object()))
        )
    assert (
        vios_tools.hmc_set_vios_boot_order("sys-R1", "target-R1", boot_string="raw")
        == expected
    )
    result = CliRunner().invoke(
        app, ["vios", "set-boot-order", "sys-R1", "target-R1", "--boot-string", "raw"]
    )
    assert result.exit_code == 0, result.output
    assert TOOL_SECURITY["hmc_set_vios_boot_order"].target_kind == "vios"
    assert TOOL_SECURITY["hmc_read_vios_boot_order"].effect == "read"


def test_vios_read_presentations_keep_three_observed_keys(monkeypatch):
    expected = {
        "vios_uuid": "target-R1",
        "vios_name": "vios-R1",
        "pending_boot_string": None,
    }
    reader = AsyncMock(return_value=expected)
    for module in [vios_cli, vios_tools]:
        monkeypatch.setattr(module, "read_vios_boot_order", reader)
        monkeypatch.setattr(
            module, "with_client", lambda fn, **kwargs: asyncio.run(fn(object()))
        )
    assert vios_tools.hmc_read_vios_boot_order("sys-R1", "target-R1") == expected
    result = CliRunner().invoke(app, ["vios", "read-boot-order", "sys-R1", "target-R1"])
    assert result.exit_code == 0 and '"pending_boot_string": null' in result.output


@pytest.mark.parametrize(
    "group,module,operation",
    [
        ("lpars", profiles, "set_lpar_boot_order"),
        ("vios", vios_cli, "set_vios_boot_order"),
    ],
)
def test_boot_domain_error_has_no_confirmation(monkeypatch, group, module, operation):
    from hmcpctl.errors import HMCError

    writer = AsyncMock(side_effect=HMCError("independent verification unavailable"))
    monkeypatch.setattr(module, operation, writer)
    monkeypatch.setattr(module, "with_client", lambda fn: asyncio.run(fn(object())))
    result = CliRunner().invoke(
        app, [group, "set-boot-order", "sys-R1", "target-R1", "--boot-string", "raw"]
    )
    assert result.exit_code != 0
    assert "Boot order set to:" not in result.output
    assert "Pending boot string:" not in result.output
    assert writer.await_count == 1
