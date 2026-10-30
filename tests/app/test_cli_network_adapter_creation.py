"""Actual CLI/MCP presentations of selected Ethernet creation policies (#1821)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click import unstyle
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.virtualization import adapters as cli_adapters
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.virtualization.adapters import AdapterResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.virtualization import adapters as tools

LPAR = "11111111-1111-4111-8111-111111111111"
CREATED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIELDS = {
    "addl_vlan_ids": [2, 3],
    "allowed_os_mac_addrs": "all",
    "qos_priority": 0,
    "vsi_manager_id": 1,
    "vsi_type_id": 2,
    "vsi_type_version": 3,
}


def result(verification=None):
    return AdapterResult(
        LPAR,
        {"UUID": CREATED, "Resource": {}},
        ChangeLocation("Disabled", "current-configuration"),
        verification,
    )


def verification(status="verified"):
    return {
        "status": status,
        "checked_fields": list(FIELDS),
        "mismatched_fields": [] if status != "mismatched" else ["qos_priority"],
        "unavailable_fields": [] if status != "unavailable" else ["qos_priority"],
    }


@pytest.fixture
def adapter(monkeypatch):
    operation = AsyncMock(return_value=result())
    connections = []

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(object()))

    for module in (cli_adapters, tools):
        monkeypatch.setattr(module, "with_client", run)
        monkeypatch.setattr(module, "add_network_adapter", operation)
    return SimpleNamespace(operation=operation, connections=connections)


def policy(tool="hmc_add_network_adapter"):
    return compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [tool],
                            "connections": ["<default>"],
                            "targets": "all-targets",
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )


def test_existing_tool_shape_and_profile_tail_remain_compatible(adapter):
    returned = tools.hmc_add_network_adapter(
        "part-R1", 100, None, None, False, None, "profile-R1", None, False
    )
    assert returned["UUID"] == CREATED
    assert "change_location" in returned
    assert "creation_verification" not in returned
    assert adapter.connections == [{"profile": "profile-R1"}]
    assert not set(FIELDS) & adapter.operation.await_args.kwargs.keys()


def test_tool_forwards_six_fields_and_adds_metadata_without_wrapping_identity(adapter):
    adapter.operation.return_value = result(verification())
    returned = tools.hmc_add_network_adapter("part-R1", 100, tagged=True, **FIELDS)
    assert returned["UUID"] == CREATED
    assert returned["creation_verification"] == verification()
    assert returned["change_location"]["lives_in"] == "current-configuration"
    for key, value in FIELDS.items():
        assert adapter.operation.await_args.kwargs[key] == value


def test_cli_accepts_structured_lists_and_preserves_zero(adapter):
    adapter.operation.return_value = result(verification())
    invoked = CliRunner().invoke(
        app,
        [
            "adapters",
            "add-network",
            "part-R1",
            "--vlan",
            "100",
            "--tagged",
            "--addl-vlan-ids",
            "[2,3]",
            "--allowed-os-mac-addrs",
            "all",
            "--qos-priority",
            "0",
            "--vsi-manager-id",
            "1",
            "--vsi-type-id",
            "2",
            "--vsi-type-version",
            "3",
            "--yes",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    for key, value in FIELDS.items():
        assert adapter.operation.await_args.kwargs[key] == value
    assert "verified" in unstyle(invoked.output)


@pytest.mark.parametrize("status", ["mismatched", "unavailable"])
def test_cli_verification_failure_never_claims_no_effect_or_retries(adapter, status):
    adapter.operation.return_value = result(verification(status))
    invoked = CliRunner().invoke(
        app,
        [
            "adapters",
            "add-network",
            "part-R1",
            "--vlan",
            "100",
            "--qos-priority",
            "0",
            "--yes",
        ],
    )
    assert invoked.exit_code == 1, invoked.output
    output = unstyle(invoked.output)
    assert status in output
    assert "Added network adapter" not in output
    assert "retry" in output.lower() and "occurred" in output.lower()
    adapter.operation.assert_awaited_once()


@pytest.mark.parametrize(
    "option, value",
    [
        ("--addl-vlan-ids", "{"),
        ("--addl-vlan-ids", "{}"),
        ("--addl-vlan-ids", "[true]"),
        ("--addl-vlan-ids", "[4095]"),
        ("--allowed-os-mac-addrs", "[]"),
        ("--allowed-os-mac-addrs", "[1]"),
        ("--qos-priority", "invalid"),
        ("--qos-priority", "8"),
        ("--vsi-type-id", "2147483648"),
    ],
)
def test_cli_bad_policy_is_refused_before_confirmation_or_connection(
    adapter, option, value
):
    invoked = CliRunner().invoke(
        app,
        [
            "adapters",
            "add-network",
            "part-R1",
            "--vlan",
            "100",
            "--tagged",
            option,
            value,
        ],
        input="y\n",
    )
    assert invoked.exit_code != 0
    assert "Add network adapter" not in unstyle(invoked.output)
    assert adapter.connections == []
    adapter.operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_mcp_schema_rejects_bool_and_mac_numbers_before_connection(adapter):
    async with Client(create_mcp(policy())) as client:
        for field, value in [
            ("qos_priority", True),
            ("vsi_manager_id", True),
            ("vsi_type_id", False),
            ("vsi_type_version", True),
            ("addl_vlan_ids", [True]),
            ("allowed_os_mac_addrs", [200000000001]),
        ]:
            called = await client.call_tool(
                "hmc_add_network_adapter",
                {
                    "lpar_name_or_uuid": "part-R1",
                    "port_vlan_id": 100,
                    "tagged": True,
                    field: value,
                },
                raise_on_error=False,
            )
            assert called.is_error, field
            assert adapter.connections == []
        adapter.operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_mcp_result_exposes_verification_and_original_identity(adapter):
    adapter.operation.return_value = result(verification())
    async with Client(create_mcp(policy())) as client:
        called = await client.call_tool(
            "hmc_add_network_adapter",
            {
                "lpar_name_or_uuid": "part-R1",
                "port_vlan_id": 100,
                "tagged": True,
                **FIELDS,
            },
            raise_on_error=False,
        )
    assert not called.is_error
    rendered = "\n".join(
        block.text for block in called.content if hasattr(block, "text")
    )
    assert (
        CREATED in rendered
        and "creation_verification" in rendered
        and "verified" in rendered
    )
    assert adapter.operation.await_args.kwargs["qos_priority"] == 0


@pytest.mark.asyncio
async def test_new_fields_do_not_bypass_a_read_only_tool_grant(adapter):
    async with Client(create_mcp(policy("hmc_list_adapters"))) as client:
        called = await client.call_tool(
            "hmc_add_network_adapter",
            {
                "lpar_name_or_uuid": "part-R1",
                "port_vlan_id": 100,
                "qos_priority": 0,
            },
            raise_on_error=False,
        )
    assert called.is_error
    assert adapter.connections == []


@pytest.mark.asyncio
async def test_real_mcp_semantic_policy_validation_precedes_credentials(adapter):
    async with Client(create_mcp(policy())) as client:
        for fields in [
            {"addl_vlan_ids": list(range(1, 22))},
            {"allowed_os_mac_addrs": []},
            {"qos_priority": 8},
            {"vsi_manager_id": 2**31},
        ]:
            called = await client.call_tool(
                "hmc_add_network_adapter",
                {
                    "lpar_name_or_uuid": "part-R1",
                    "port_vlan_id": 100,
                    "tagged": True,
                    **fields,
                },
                raise_on_error=False,
            )
            assert called.is_error
            assert adapter.connections == []
        adapter.operation.assert_not_awaited()
