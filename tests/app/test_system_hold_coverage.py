"""System hold checks at composed MCP and resolver boundaries (ADR 0339)."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from conftest import assert_no_mutating_requests, mock_hmc, mock_system_summary  # noqa: F401
from fastmcp import Client
from fastmcp.exceptions import ToolError

from hmcpctl.call_context import CallContext, calling, current_call
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.logical import holds
from hmcpctl.resource_identity import install_partition_guard, resolve_system_uuid
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.ssh import transport

SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"
ARGUMENTS = {
    "hmc_modify_system": {"new_name": "renamed"},
    "hmc_power_on_system": {},
    "hmc_power_off_system": {"immediate": True},
    "hmc_activate_system_profile": {"name": "profile-a", "action": "validate"},
    "hmc_restore_lpar_profiles": {
        "file_path": "backup",
        "restore_type": 3,
        "system_wide_restore_approved": True,
        "ownership_override": True,
    },
    "hmc_update_firmware": {
        "platform_update": {
            "SystemFirmwareUpdate": {"UpdateType": "Update", "UpdateOrder": 1}
        }
    },
}


@pytest.fixture
def environment(monkeypatch, mock_hmc):  # noqa: F811
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    mock_system_summary(mock_hmc, SYS, "sys-R1")
    monkeypatch.setattr(
        HMCClient,
        "get_console_info",
        AsyncMock(
            return_value={
                "Resource": {
                    "VersionInfo": {
                        "Version": "11",
                        "Release": "1",
                        "ServicePackName": "1111",
                    }
                }
            }
        ),
    )
    ssh = Mock(side_effect=AssertionError("SSH reached"))
    monkeypatch.setattr(transport.asyncssh, "connect", ssh)
    mock_hmc.route(method__in=["POST", "PUT", "DELETE"]).mock(
        return_value=httpx.Response(500, text="write reached")
    )
    app = create_mcp(
        compile_legacy_policy(
            TOOL_SECURITY, ("<default>",), include_arbitrary_command=False
        ),
        catalog="registered",
    )
    return app, mock_hmc, ssh


def test_system_marker_set_is_exact():
    assert {
        name
        for name, s in TOOL_SECURITY.items()
        if getattr(s, "system_partition_write", False)
    } == set(ARGUMENTS)


@pytest.mark.parametrize("tool", ARGUMENTS)
def test_each_system_tool_refuses_before_write_direct_and_invoke(environment, tool):
    app, router, ssh = environment
    placed = holds.place_hold(
        SYS, LPAR, consumer_label="consumer", agent_id="hmcpctl", connection="c"
    )

    async def check():
        async with Client(app) as client:
            listed = {t.name: t for t in await client.list_tools()}
            assert "hold_id" not in listed[tool].input_schema["properties"]
            for selector in (SYS, SYS.upper(), "sys-R1"):
                arguments = {"system_name_or_uuid": selector, **ARGUMENTS[tool]}
                for name, args in (
                    (tool, arguments),
                    ("hmc_invoke_tool", {"name": tool, "arguments": arguments}),
                ):
                    with pytest.raises(ToolError, match="system_held") as error:
                        await client.call_tool(name, args)
                    assert placed.hold_id not in str(error.value)

    asyncio.run(check())
    assert_no_mutating_requests(router)
    ssh.assert_not_called()
    assert current_call() is None


def test_unheld_system_can_reach_write_and_reads_ignore_holds(environment):
    app, router, _ = environment
    holds.place_hold(LPAR, SYS, consumer_label="other", agent_id="a", connection="c")

    async def check():
        async with Client(app) as client:
            with pytest.raises(ToolError, match="write reached"):
                await client.call_tool(
                    "hmc_power_on_system", {"system_name_or_uuid": SYS}
                )
            holds.place_hold(
                SYS, LPAR, consumer_label="held", agent_id="a", connection="c"
            )
            await client.call_tool("hmc_list_systems", {"state": "unlisted-state"})

    asyncio.run(check())
    assert any(c.request.url.path.endswith("/do/PowerOn") for c in router.calls)


@pytest.mark.asyncio
async def test_resolver_rejects_malformed_identity_only_with_frontend_guard():
    client = AsyncMock()
    client.list_system_summaries.return_value = [
        {"UUID": "bad", "SystemName": "sys-R1"}
    ]
    context = CallContext("system-write", False, None, system_partition_write=True)
    holds.place_hold(SYS, LPAR, consumer_label="held", agent_id="a", connection="c")
    with calling(context):
        assert await resolve_system_uuid(client, SYS) == SYS
        assert await resolve_system_uuid(client, "sys-R1") == "bad"
        install_partition_guard(holds.HOLD_GUARD)
        with pytest.raises(holds.HoldRefused, match="system_hold_unverified"):
            await resolve_system_uuid(client, "sys-R1")
        client.list_system_summaries.return_value = []
        with pytest.raises(ValueError, match="No managed system"):
            await resolve_system_uuid(client, "missing")
