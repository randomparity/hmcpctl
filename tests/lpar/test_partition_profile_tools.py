"""Thin profile adapters retain connection selection and LPAR/VIOS policy targets."""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from hmcpctl.documents.partition_profile import ProfilePatch
from hmcpctl.server_tools.lpar import profiles as tools

CASES = [
    ("list_partition_profiles", "read", {}),
    ("read_partition_profile", "read", {"profile_selector": "saved"}),
    (
        "update_partition_profile",
        "mutate",
        {
            "profile_selector": "saved",
            "expected_etag": "etag",
            "patch": ProfilePatch(desired_memory=1024),
            "exclusive_writer_window": True,
        },
    ),
    (
        "save_current_partition_profile",
        "destructive",
        {
            "name": "saved",
            "overwrite": True,
            "expected_etag": "etag",
            "exclusive_writer_window": True,
        },
    ),
    (
        "set_default_partition_profile",
        "mutate",
        {"profile_selector": "saved", "exclusive_writer_window": True},
    ),
    (
        "delete_partition_profile",
        "destructive",
        {
            "profile_selector": "saved",
            "expected_etag": "etag",
            "exclusive_writer_window": True,
        },
    ),
]


@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize("operation,effect,arguments", CASES)
def test_profile_tool_delegation_and_security(
    monkeypatch, vios, operation, effect, arguments
):
    name = (
        "hmc_" + operation.replace("partition_profile", "vios_partition_profile")
        if vios
        else "hmc_" + operation
    )
    handler = getattr(tools, name, None)
    assert callable(handler), name
    domain = AsyncMock(return_value="bounded-result")
    hmc = MagicMock()
    connections = []
    monkeypatch.setattr(tools, operation, domain)
    monkeypatch.setattr(
        tools,
        "with_client",
        lambda fn, **kw: connections.append(kw) or asyncio.run(fn(hmc)),
    )
    target = "vios_name_or_uuid" if vios else "lpar_name_or_uuid"
    result = handler(
        system_name_or_uuid="system",
        **{target: "partition"},
        **arguments,
        profile="connection",
    )
    assert result == "bounded-result" and connections == [{"profile": "connection"}]
    call = domain.call_args
    assert call.args[:3] == (hmc, "system", "partition")
    assert call.kwargs["partition_type"] == (
        "VirtualIOServer" if vios else "LogicalPartition"
    )
    assert "partition_type" not in inspect.signature(handler).parameters
    metadata = tools.tool_security()[name]
    assert metadata.effect == effect and metadata.target_kind == (
        "vios" if vios else "lpar"
    )
    assert metadata.connection_argument == "profile"
    assert {t.argument for t in metadata.targets} == {"system_name_or_uuid", target}


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize("operation,effect,arguments", CASES)
async def test_mcp_serializes_profile_records_and_truthful_outcomes(
    monkeypatch, vios, operation, effect, arguments
):
    from fastmcp import Client

    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.operations.lpar.profiles import (
        PartitionProfileInventory,
        PartitionProfileOutcome,
        PartitionProfileRecord,
    )
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    system = "22222222-2222-4222-8222-222222222222"
    partition = "11111111-1111-4111-8111-111111111111"
    uuid = "33333333-3333-4333-8333-333333333333"
    kind = "VirtualIOServer" if vios else "LogicalPartition"
    record = PartitionProfileRecord(
        uuid, "saved", "17", "opaque", {"ProfileName": "saved"}
    )
    if operation == "list_partition_profiles":
        result = PartitionProfileInventory(system, partition, kind, (record,))
    elif operation == "read_partition_profile":
        result = record
    else:
        action = {
            "update_partition_profile": "update",
            "save_current_partition_profile": "save",
            "set_default_partition_profile": "default",
            "delete_partition_profile": "delete",
        }[operation]
        result = PartitionProfileOutcome(
            action,
            system,
            partition,
            kind,
            "saved",
            uuid,
            execution="completed",
            requested_change="verified",
            preservation="verified",
            comparison_complete=True,
        )
    monkeypatch.setattr(tools, "with_client", lambda fn, **kwargs: result)
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    name = (
        "hmc_" + operation.replace("partition_profile", "vios_partition_profile")
        if vios
        else "hmc_" + operation
    )
    target = "vios_name_or_uuid" if vios else "lpar_name_or_uuid"
    params = {"system_name_or_uuid": system, target: partition, **arguments}
    if "patch" in params:
        params["patch"] = params["patch"].model_dump(exclude_none=True)
    async with Client(create_mcp(policy)) as client:
        response = await client.call_tool(name, params)
    content = response.structured_content
    if operation == "list_partition_profiles":
        assert (
            content["profiles"][0]["uuid"] == uuid and content["partition_type"] == kind
        )
    elif operation == "read_partition_profile":
        assert content["name"] == "saved" and content["etag"] == "opaque"
    else:
        assert (
            content["execution"] == "completed"
            and content["requested_change"] == "verified"
        )
        assert (
            content["preservation"] == "verified" and content["partition_type"] == kind
        )
