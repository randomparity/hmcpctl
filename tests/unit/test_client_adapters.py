"""Direct contracts for the adapter client mixin delegation boundary."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.client_adapters import AdaptersMixin
from hmcpctl.client.client_contracts import ADAPTER_TYPES, validate_adapter_type


def test_adapter_type_validation_accepts_only_the_published_vocabulary():
    for adapter_type in ADAPTER_TYPES:
        assert validate_adapter_type(adapter_type) == adapter_type

    with pytest.raises(ValueError, match="Invalid adapter_type"):
        validate_adapter_type("not-an-adapter")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_and_delete_adapter_delegate_to_child_resources():
    client = SimpleNamespace(
        list_child=AsyncMock(return_value=[{"UUID": "adapter-1"}]),
        delete_child=AsyncMock(),
    )

    adapters = await AdaptersMixin.list_adapters(
        client, "lpar-1", "ClientNetworkAdapter"
    )
    await AdaptersMixin.delete_adapter(
        client, "lpar-1", "ClientNetworkAdapter", "adapter-1"
    )

    assert adapters == [{"UUID": "adapter-1"}]
    client.list_child.assert_awaited_once_with(
        "LogicalPartition", "lpar-1", "ClientNetworkAdapter"
    )
    client.delete_child.assert_awaited_once_with(
        "LogicalPartition", "lpar-1", "ClientNetworkAdapter", "adapter-1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arguments", "controls", "adapter_type", "xml_fragment"),
    [
        (
            AdaptersMixin.add_vscsi_adapter,
            ("lpar-1", 2, 12, 4),
            {},
            "VirtualSCSIClientAdapter",
            "VirtualSCSIClientAdapter",
        ),
        (
            AdaptersMixin.add_vfc_adapter,
            ("lpar-1", 2, 12, 4),
            {},
            "VirtualFibreChannelClientAdapter",
            "VirtualFibreChannelClientAdapter",
        ),
        (
            AdaptersMixin.add_network_adapter,
            ("lpar-1", 200),
            {
                "slot_number": 4,
                "virtual_switch_id": 0,
                "tagged": True,
                "mac_address": "020000000001",
            },
            "ClientNetworkAdapter",
            "ClientNetworkAdapter",
        ),
    ],
)
async def test_add_adapter_builds_document_and_creates_child(
    method, arguments, controls, adapter_type, xml_fragment
):
    client = SimpleNamespace(create_child=AsyncMock(return_value={"UUID": "adapter-1"}))

    result = await method(client, *arguments, **controls)

    assert result == {"UUID": "adapter-1"}
    resource, lpar_uuid, actual_adapter_type, xml = client.create_child.await_args.args
    assert (resource, lpar_uuid, actual_adapter_type) == (
        "LogicalPartition",
        "lpar-1",
        adapter_type,
    )
    assert xml_fragment in xml


def test_network_adapter_optional_controls_are_keyword_only():
    parameters = inspect.signature(AdaptersMixin.add_network_adapter).parameters

    for name in ("slot_number", "virtual_switch_id", "tagged", "mac_address"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_list_and_delete_adapter_reject_unknown_adapter_types():
    client = SimpleNamespace(list_child=AsyncMock(), delete_child=AsyncMock())

    with pytest.raises(ValueError, match="Invalid adapter_type"):
        await AdaptersMixin.list_adapters(client, "lpar-1", "UnknownAdapter")
    with pytest.raises(ValueError, match="Invalid adapter_type"):
        await AdaptersMixin.delete_adapter(
            client, "lpar-1", "UnknownAdapter", "adapter-1"
        )

    client.list_child.assert_not_awaited()
    client.delete_child.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_creation_selected_xml_uses_one_real_child_put(mock_hmc):
    import httpx
    from conftest import make_config

    from hmcpctl.client.core import HMCClient
    from hmcpctl.xmlutil import parse_feed

    partition = "11111111-1111-4111-8111-111111111111"
    feed = '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{partition}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=feed))
    async with HMCClient(make_config()) as client:
        await client.add_network_adapter(
            partition,
            100,
            tagged=True,
            addl_vlan_ids=[2],
            allowed_os_mac_addrs="none",
            qos_priority=0,
            vsi_manager_id=1,
            vsi_type_id=2,
            vsi_type_version=3,
        )
    assert route.call_count == 1
    resource = parse_feed(route.calls[0].request.content.decode())[0]["Resource"]
    assert resource["TaggedVLANIDs"] == "2"
    assert resource["AllowedOperatingSystemMACAddresses"] == "NONE"
    assert resource["QualityOfServicePriority"] == "0"
    assert resource["QualityOfServicePriorityEnabled"] == "true"
    assert resource["VirtualStationInterfaceManagerID"] == "1"
    assert resource["VirtualStationInterfaceTypeID"] == "2"
    assert resource["VirtualStationInterfaceTypeVersion"] == "3"
    assert "X-HMC-Schema-Version" not in route.calls[0].request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, body",
    [
        (200, ""),
        (204, ""),
        (200, "<broken"),
        (200, "<feed/>"),
        (200, '<entry xmlns="http://www.w3.org/2005/Atom"/>'),
        (
            200,
            '<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="/more"/></feed>',
        ),
    ],
)
async def test_network_creation_feed_refuses_missing_or_incomplete_inventory(
    mock_hmc, status, body
):
    import httpx
    from conftest import make_config

    from hmcpctl.client.core import HMCClient
    from hmcpctl.errors import HMCError

    partition = "11111111-1111-4111-8111-111111111111"
    route = mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{partition}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(status, text=body))
    async with HMCClient(make_config()) as client:
        with pytest.raises(HMCError):
            await client._network_adapter_creation_feed(partition)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_network_creation_feed_accepts_positively_empty_atom_feed(mock_hmc):
    import httpx
    from conftest import make_config

    from hmcpctl.client.core import HMCClient

    partition = "11111111-1111-4111-8111-111111111111"
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{partition}/ClientNetworkAdapter"
    ).mock(
        return_value=httpx.Response(
            200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>'
        )
    )
    async with HMCClient(make_config()) as client:
        assert await client._network_adapter_creation_feed(partition) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        (
            "<id>urn:uuid:11111111-1111-4111-8111-111111111111</id>"
            "<id>urn:uuid:22222222-2222-4222-8222-222222222222</id>"
            "<content><ClientNetworkAdapter/></content>"
        ),
        (
            "<id>urn:uuid:11111111-1111-4111-8111-111111111111</id>"
            "<content><ClientNetworkAdapter/></content>"
            "<content><ClientNetworkAdapter/></content>"
        ),
        (
            "<id>urn:uuid:11111111-1111-4111-8111-111111111111</id>"
            "<content><ClientNetworkAdapter/><ClientNetworkAdapter/></content>"
        ),
        (
            "<id>urn:uuid:11111111-1111-4111-8111-111111111111<Nested/></id>"
            "<content><ClientNetworkAdapter/></content>"
        ),
    ],
)
async def test_creation_inventory_refuses_ambiguous_raw_atom_entries(mock_hmc, entry):
    import httpx
    from conftest import make_config

    from hmcpctl.client.core import HMCClient
    from hmcpctl.errors import HMCError

    partition = "11111111-1111-4111-8111-111111111111"
    route = mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{partition}/ClientNetworkAdapter"
    ).mock(
        return_value=httpx.Response(
            200,
            text=f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>{entry}</entry></feed>',
        )
    )
    async with HMCClient(make_config()) as client:
        with pytest.raises(HMCError, match="unambiguous"):
            await client._network_adapter_creation_feed(partition)
    assert route.call_count == 1
