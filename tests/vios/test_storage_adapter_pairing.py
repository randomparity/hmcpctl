"""Client storage adapter reads, paired identity and the remote VIOS check (ADR 0374).

Resources mirror the element set a V10R3 M1060 HMC served for vSCSI and vFC client
adapters; every identifier here is synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hmcpctl.errors import HMCError
from hmcpctl.operations.virtualization import adapters as ops
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.xmlutil import parse_feed

SYSTEM = "aaaaaaaa-0000-4000-8000-000000000001"
LPAR = "bbbbbbbb-0000-4000-8000-000000000002"
VIOS = "cccccccc-0000-4000-8000-000000000003"
ADAPTER = "dddddddd-0000-4000-8000-000000000004"
OTHER_VIOS = "eeeeeeee-0000-4000-8000-000000000005"
BASE = "https://hmc.test:443/rest/api/uom"
UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
VIOS_LINK = (
    '<ConnectingPartition kb="CUR" kxe="false" rel="related" '
    f'href="{BASE}/ManagedSystem/{SYSTEM}/VirtualIOServer/{VIOS}"/>'
)


def _entry(element: str, body: str) -> dict[str, Any]:
    xml = (
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        f"<id>{ADAPTER}</id><title>{element}</title>"
        f'<content type="application/vnd.ibm.powervm.uom+xml; type={element}">'
        f'<{element}:{element} xmlns:{element}="{UOM_NS}" '
        f'xmlns="{UOM_NS}" schemaVersion="V1_0">'
        '<AdapterType kxe="false" kb="ROR">Client</AdapterType>'
        '<LocalPartitionID kxe="false" kb="CUR">7</LocalPartitionID>'
        f"{body}</{element}:{element}></content></entry>"
    )
    return parse_feed(xml)[0]


def _vscsi(link: str = VIOS_LINK) -> dict[str, Any]:
    return _entry(
        "VirtualSCSIClientAdapter",
        '<VirtualSlotNumber kb="COD" kxe="false">5</VirtualSlotNumber>'
        '<RemoteLogicalPartitionID kb="CUR" kxe="false">100</RemoteLogicalPartitionID>'
        '<RemoteSlotNumber kb="CUA" kxe="false">42</RemoteSlotNumber>' + link,
    )


def _vfc(wwpns: str = "C050760000AA0000 c050760000aa0001") -> dict[str, Any]:
    return _entry(
        "VirtualFibreChannelClientAdapter",
        '<VirtualSlotNumber kb="COD" kxe="false">4</VirtualSlotNumber>'
        + VIOS_LINK
        + '<ConnectingPartitionID kb="CUR" kxe="false">100</ConnectingPartitionID>'
        '<ConnectingVirtualSlotNumber kb="CUR" kxe="false">40</ConnectingVirtualSlotNumber>'
        f'<WWPNs kxe="false" kb="CUR">{wwpns}</WWPNs>',
    )


def _partition(partition_id: str = "7", system_link: bool = True) -> dict[str, Any]:
    resource: dict[str, Any] = {"PartitionID": partition_id}
    if system_link:
        resource["AssociatedManagedSystem"] = {
            "href": f"{BASE}/ManagedSystem/{SYSTEM}",
            "rel": "related",
        }
    return {"UUID": LPAR, "ResourceType": "LogicalPartition", "Resource": resource}


def _vios(uuid: str, partition_id: str) -> dict[str, Any]:
    return {"UUID": uuid, "Resource": {"PartitionID": partition_id}}


def test_vscsi_paired_identity_reads_the_served_remote_fields() -> None:
    entry = _vscsi()
    assert ops.paired_identity(entry["Resource"], "VirtualSCSIClientAdapter") == {
        "remote_partition_id": 100,
        "remote_slot": 42,
        "remote_vios_uuid": VIOS,
    }


def test_vfc_paired_identity_normalizes_the_wwpn_pair() -> None:
    entry = _vfc()
    assert ops.paired_identity(
        entry["Resource"], "VirtualFibreChannelClientAdapter"
    ) == {
        "remote_partition_id": 100,
        "remote_slot": 40,
        "remote_vios_uuid": VIOS,
        "wwpns": ["c050760000aa0000", "c050760000aa0001"],
    }


@pytest.mark.parametrize(
    "wwpns",
    [
        "c050760000aa0000",
        "c050760000aa0000 c050760000aa0001 c050760000aa0002",
        "c050760000aa000g c050760000aa0001",
        "c050760000aa000 c050760000aa0001",
    ],
)
def test_vfc_paired_identity_refuses_a_malformed_wwpn_value(wwpns: str) -> None:
    identity = ops.paired_identity(
        _vfc(wwpns)["Resource"], "VirtualFibreChannelClientAdapter"
    )
    assert identity["wwpns"] is None
    assert identity["remote_partition_id"] == 100


def test_paired_identity_accepts_only_a_vios_link_and_numeric_ids() -> None:
    lpar_link = VIOS_LINK.replace("VirtualIOServer", "LogicalPartition")
    resource = dict(_vscsi(lpar_link)["Resource"])
    resource["RemoteLogicalPartitionID"] = "1x"
    resource["RemoteSlotNumber"] = ""
    assert ops.paired_identity(resource, "VirtualSCSIClientAdapter") == {
        "remote_partition_id": None,
        "remote_slot": None,
        "remote_vios_uuid": None,
    }
    assert ops.paired_identity({}, "VirtualFibreChannelClientAdapter") == {
        "remote_partition_id": None,
        "remote_slot": None,
        "remote_vios_uuid": None,
        "wwpns": None,
    }


def _hmc(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "get_logical_partition": AsyncMock(return_value=_partition()),
        "get_storage_adapter": AsyncMock(return_value=_vfc()),
        "list_vios": AsyncMock(return_value=[_vios(VIOS, "100")]),
        "add_vscsi_adapter": AsyncMock(return_value=_vscsi()),
        "add_vfc_adapter": AsyncMock(return_value=_vfc()),
        "list_adapters": AsyncMock(return_value=[_vscsi()]),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_get_adapter_returns_the_owned_adapter_with_its_pairing() -> None:
    hmc = _hmc()
    result = await ops.get_adapter(
        hmc, LPAR, "VirtualFibreChannelClientAdapter", ADAPTER
    )
    assert result["UUID"] == ADAPTER
    assert result["paired_identity"]["wwpns"] == [
        "c050760000aa0000",
        "c050760000aa0001",
    ]
    hmc.get_storage_adapter.assert_awaited_once_with(
        LPAR, "VirtualFibreChannelClientAdapter", ADAPTER
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "partition",
    [AsyncMock(return_value=None), AsyncMock(side_effect=HMCError("gone", 404))],
)
async def test_get_adapter_names_a_missing_partition(partition: AsyncMock) -> None:
    hmc = _hmc(get_logical_partition=partition)
    with pytest.raises(ResourceNotFoundError) as caught:
        await ops.get_adapter(hmc, LPAR, "VirtualSCSIClientAdapter", ADAPTER)
    assert caught.value.resource_kind == "partition"
    hmc.get_storage_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_adapter_names_a_missing_adapter() -> None:
    hmc = _hmc(get_storage_adapter=AsyncMock(side_effect=HMCError("absent", 404)))
    with pytest.raises(ResourceNotFoundError) as caught:
        await ops.get_adapter(hmc, LPAR, "VirtualSCSIClientAdapter", ADAPTER)
    assert caught.value.resource_kind == "adapter"


@pytest.mark.asyncio
async def test_get_adapter_keeps_other_hmc_failures() -> None:
    hmc = _hmc(get_storage_adapter=AsyncMock(side_effect=HMCError("boom", 500)))
    with pytest.raises(HMCError) as caught:
        await ops.get_adapter(hmc, LPAR, "VirtualSCSIClientAdapter", ADAPTER)
    assert caught.value.status_code == 500


def _other_uuid() -> dict[str, Any]:
    return {**_vscsi(), "UUID": OTHER_VIOS}


def _foreign() -> dict[str, Any]:
    entry = _vscsi()
    return {**entry, "Resource": {**entry["Resource"], "LocalPartitionID": "8"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, _other_uuid(), _foreign()])
async def test_get_adapter_refuses_a_different_foreign_or_empty_answer(
    answer: Any,
) -> None:
    hmc = _hmc(get_storage_adapter=AsyncMock(return_value=answer))
    with pytest.raises(HMCError, match="different, foreign or no resource"):
        await ops.get_adapter(hmc, LPAR, "VirtualSCSIClientAdapter", ADAPTER)


@pytest.mark.asyncio
async def test_get_adapter_refuses_a_non_storage_type_before_any_read() -> None:
    hmc = _hmc()
    with pytest.raises(ValueError, match="Invalid adapter_type"):
        await ops.get_adapter(hmc, LPAR, "ClientNetworkAdapter", ADAPTER)  # type: ignore[arg-type]
    hmc.get_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_attaches_pairing_only_to_storage_types() -> None:
    hmc = _hmc()
    [vscsi] = await ops.list_adapters(hmc, LPAR, "VirtualSCSIClientAdapter")
    assert vscsi["paired_identity"]["remote_slot"] == 42
    hmc.list_adapters = AsyncMock(return_value=[{"UUID": ADAPTER}])
    assert await ops.list_adapters(hmc, LPAR, "ClientNetworkAdapter") == [
        {"UUID": ADAPTER}
    ]


@pytest.fixture
def authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def authorize(*_args: Any, **_kwargs: Any) -> str:
        return LPAR

    async def location(*_args: Any, **_kwargs: Any) -> Any:
        return "location"

    monkeypatch.setattr(ops, "resolve_and_authorize_lpar_mutation", authorize)
    monkeypatch.setattr(ops, "read_change_location", location)


ADDS = [
    (ops.add_vscsi_adapter, "add_vscsi_adapter"),
    (ops.add_vfc_adapter, "add_vfc_adapter"),
]


@pytest.mark.asyncio
@pytest.mark.usefixtures("authorized")
@pytest.mark.parametrize(("add", "method"), ADDS)
async def test_storage_add_pairs_with_the_one_local_vios(add: Any, method: str) -> None:
    hmc = _hmc()
    result = await add(hmc, LPAR, 100, 40)
    getattr(hmc, method).assert_awaited_once_with(LPAR, 100, 40, None)
    hmc.list_vios.assert_awaited_once_with(SYSTEM)
    assert result.resource["paired_identity"]["remote_partition_id"] == 100


@pytest.mark.asyncio
@pytest.mark.usefixtures("authorized")
@pytest.mark.parametrize(("add", "method"), ADDS)
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"list_vios": AsyncMock(return_value=[_vios(VIOS, "99")])}, "not exactly one"),
        (
            {
                "list_vios": AsyncMock(
                    return_value=[_vios(VIOS, "100"), _vios(OTHER_VIOS, "100")]
                )
            },
            "not exactly one",
        ),
        ({"get_logical_partition": AsyncMock(return_value=None)}, "unavailable"),
        (
            {
                "get_logical_partition": AsyncMock(
                    return_value=_partition(system_link=False)
                )
            },
            "unavailable",
        ),
    ],
)
async def test_storage_add_refuses_before_any_put(
    add: Any, method: str, overrides: dict[str, Any], message: str
) -> None:
    hmc = _hmc(**overrides)
    with pytest.raises(ValueError, match=message):
        await add(hmc, LPAR, 100, 40)
    getattr(hmc, method).assert_not_awaited()
