"""Optional vNIC policies survive presentation and framework validation."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.cli_commands.virtualization import vnic as cli_vnic
from hmcpctl.operations.virtualization.vnic import VnicChangeResult
from hmcpctl.server_tools.virtualization import vnic as tools


@pytest.fixture
def operation(monkeypatch):
    result = VnicChangeResult(
        "add", True, True, None, "2", (), (), (), (), True, True, "", ()
    )
    mock = AsyncMock(return_value=result)
    for module in (cli_vnic, tools):
        monkeypatch.setattr(module, "add_vnic", mock)
        monkeypatch.setattr(
            module, "with_client", lambda fn, **kw: asyncio.run(fn(object()))
        )
    return mock


def test_cli_forwards_both_lists_zero_false_and_mac(operation):
    invocation = CliRunner().invoke(
        app,
        [
            "network",
            "add-vnic",
            "system",
            "client",
            "--vios-name",
            "vios",
            "--vios-lpar-id",
            "100",
            "--adapter-id",
            "1",
            "--physical-port-id",
            "1",
            "--capacity-percent",
            "2",
            "--port-vlan-id",
            "0",
            "--pvid-priority",
            "0",
            "--no-auto-priority-failover",
            "--allowed-vlan-ids",
            "2,3",
            "--allowed-os-mac-addrs",
            "aabbccddeeff,001122334455",
            "--mac-addr",
            "aabbccddee01",
            "--yes",
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert (
        json.loads(invocation.stdout)["policy_verification"]["status"]
        == "not_requested"
    )
    assert operation.await_args.kwargs == {
        "system_name_or_uuid": "system",
        "ownership_override": False,
        "pvid_priority": 0,
        "auto_priority_failover": False,
        "allowed_vlan_ids": [2, 3],
        "allowed_os_mac_addrs": ["aabbccddeeff", "001122334455"],
        "mac_addr": "aabbccddee01",
    }


@pytest.mark.parametrize("sentinel", ["all", "none"])
def test_mcp_forwards_creation_sentinels(operation, sentinel):
    result = tools.hmc_add_vnic(
        "system",
        "client",
        "vios",
        "100",
        "1",
        "1",
        2,
        0,
        pvid_priority=0,
        auto_priority_failover=False,
        allowed_vlan_ids=sentinel,
        allowed_os_mac_addrs=sentinel,
        mac_addr="AABBCCDDEE01",
    )
    assert result["policy_verification"]["status"] == "not_requested"
    assert operation.await_args.kwargs["pvid_priority"] == 0
    assert operation.await_args.kwargs["auto_priority_failover"] is False
    assert operation.await_args.kwargs["allowed_vlan_ids"] == sentinel
    assert operation.await_args.kwargs["allowed_os_mac_addrs"] == sentinel
    assert operation.await_args.kwargs["mac_addr"] == "AABBCCDDEE01"


def test_mcp_schema_exposes_selected_policy_shape():
    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    async def read():
        from fastmcp import Client

        async with Client(
            create_mcp(
                compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
            )
        ) as client:
            return next(
                tool
                for tool in await client.list_tools()
                if tool.name == "hmc_add_vnic"
            )

    schema = asyncio.run(read()).input_schema["properties"]
    assert {
        "pvid_priority",
        "allowed_vlan_ids",
        "allowed_os_mac_addrs",
        "auto_priority_failover",
        "mac_addr",
    } <= schema.keys()
    assert "integer" in json.dumps(schema["pvid_priority"])
    assert "boolean" in json.dumps(schema["auto_priority_failover"])
    assert "array" in json.dumps(schema["allowed_vlan_ids"])
    assert "all" in json.dumps(schema["allowed_vlan_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("pvid_priority", True),
        ("pvid_priority", "0"),
        ("pvid_priority", 0.0),
        ("allowed_vlan_ids", [True]),
        ("allowed_vlan_ids", ["2"]),
        ("allowed_vlan_ids", [2.0]),
        ("auto_priority_failover", 0),
        ("auto_priority_failover", "false"),
    ],
)
async def test_framework_rejects_policy_coercion_before_client(
    monkeypatch, field, value
):
    from unittest.mock import Mock

    from fastmcp.exceptions import ValidationError
    from fastmcp.tools import FunctionTool

    connect = Mock(side_effect=AssertionError("client entered"))
    monkeypatch.setattr(tools, "with_client", connect)
    args = {
        "system_name_or_uuid": "system",
        "lpar_name_or_uuid": "client",
        "vios_name": "vios",
        "vios_lpar_id": "100",
        "adapter_id": "1",
        "physical_port_id": "1",
        "capacity_percent": 2,
        "port_vlan_id": 0,
    }
    with pytest.raises(ValidationError):
        await FunctionTool.from_function(tools.hmc_add_vnic).run({**args, field: value})
    connect.assert_not_called()
