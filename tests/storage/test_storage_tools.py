"""Tool-layer tests for the virtual adapter / storage / SSP tools.

The document builders are covered in this dir's other tests; these tests call
the actual ``@mcp.tool`` functions in ``server_tools.storage`` against the respx
``mock_hmc`` router so the argument->URL and argument->XML mapping in the
tool bodies is exercised — the layer the client tests skip.  This mirrors
``tests/app/test_server_tools.py`` for the storage domain.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from conftest import COMPLETED_JOB_ENTRY, JOB_ENTRY, JOB_ID, mock_change_location

from hmcpctl.client.client_contracts import ADAPTER_TYPES
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.storage.resources import StorageMapResult
from hmcpctl.server_tools.storage.resources import (
    hmc_create_logical_unit,
    hmc_create_media_repository,
    hmc_create_optical_media,
    hmc_create_virtual_disk,
    hmc_create_volume_group,
    hmc_delete_logical_unit,
    hmc_delete_media_repository,
    hmc_detach_storage_mapping,
    hmc_get_shared_storage_pool,
    hmc_list_clusters,
    hmc_list_shared_storage_pools,
    hmc_list_volume_groups,
    hmc_map_storage_to_lpar,
    hmc_mount_optical_media,
)
from hmcpctl.server_tools.virtualization.adapters import (
    hmc_add_network_adapter,
    hmc_add_vfc_adapter,
    hmc_add_vscsi_adapter,
    hmc_delete_adapter,
    hmc_list_adapters,
)


@pytest.fixture(autouse=True)
def _authorize_lpar_mutations(monkeypatch):
    async def authorize(_hmc, _system, lpar, **_kwargs):
        return lpar

    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        authorize,
    )
    monkeypatch.setattr(
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        authorize,
    )


LPAR_UUID = "00000000-0000-0000-0000-000000000002"
UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
VG_UUID = "22222222-2222-2222-2222-222222220001"
ADAPTER_UUID = "44444444-4444-4444-4444-444444440001"
CLUSTER_UUID = "00000000-0000-0000-0000-000000000005"
SSP_UUID = "55555555-5555-5555-5555-555555555555"
SYSTEM_UUID = "00000000-0000-0000-0000-000000000004"


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _feed(uuid: str, rtype: str, **fields: str) -> str:
    """A single-resource Atom feed; {fields} render as resource elements."""
    body = "\n".join(
        f'        <{name} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">{value}</{name}>'
        for name, value in fields.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>{rtype}:{uuid}</title>
    <link rel="SELF" href="https://hmc.test:12443/rest/api/uom/{rtype}/{uuid}"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <{rtype} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
{body}
      </{rtype}>
    </content>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------- #
# hmc_list_adapters / hmc_add_*_adapter / hmc_delete_adapter
# ---------------------------------------------------------------------- #


def test_list_adapters_defaults_to_network(monkeypatch, mock_hmc):
    """hmc_list_adapters GETs ClientNetworkAdapter by default."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(
        return_value=httpx.Response(
            200,
            text=_feed(ADAPTER_UUID, "ClientNetworkAdapter", MACAddress="001122334455"),
        )
    )
    result = hmc_list_adapters(LPAR_UUID)
    assert result[0]["UUID"] == ADAPTER_UUID
    assert result[0]["Resource"]["MACAddress"] == "001122334455"


def test_list_adapters_vscsi_type(monkeypatch, mock_hmc):
    """hmc_list_adapters honors a non-default adapter_type."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/VirtualSCSIClientAdapter"
    ).mock(
        return_value=httpx.Response(
            200, text=_feed(ADAPTER_UUID, "VirtualSCSIClientAdapter")
        )
    )
    hmc_list_adapters(LPAR_UUID, adapter_type="VirtualSCSIClientAdapter")
    assert route.called


def test_all_adapter_types_reach_the_matching_resource(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    for adapter_type in ADAPTER_TYPES:
        route = mock_hmc.get(
            f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/{adapter_type}"
        ).mock(return_value=httpx.Response(200, text=""))
        hmc_list_adapters(LPAR_UUID, adapter_type=adapter_type)
        assert route.called


def test_invalid_adapter_type_fails_before_resource_request(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match="adapter_type"):
        hmc_list_adapters(LPAR_UUID, adapter_type="UnknownAdapter")
    assert {call.request.url.path for call in mock_hmc.calls} == {"/rest/api/web/Logon"}


_LPAR_LINK = (
    "https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/"
    f"{SYSTEM_UUID}/LogicalPartition/{LPAR_UUID}"
)
_VIOS_IDENTITY = (
    Path(__file__).with_name("vios_identity_v10r3.xml").read_text(encoding="utf-8")
)


def _mapping(target: str, lpar_link: str = _LPAR_LINK) -> str:
    """A VirtualSCSIMapping in the observed V10R3 shape (#940): no UUID."""
    return f"""<VirtualSCSIMapping>
      <AssociatedLogicalPartition href="{lpar_link}" rel="related"/>
      <ServerAdapter><AdapterName>vhost0</AdapterName></ServerAdapter>
      <TargetDevice><LogicalVolumeVirtualTargetDevice>
        <TargetName>{target}</TargetName>
      </LogicalVolumeVirtualTargetDevice></TargetDevice>
    </VirtualSCSIMapping>"""


def _mock_detach_reads(mock_hmc, mappings: str):
    """Mock the grouped GET both the pre-check lookup and the RMW detach read hit.

    The operations layer looks up the mapping via ``list_storage_mappings`` before
    authorizing, then ``delete_storage_mapping`` re-reads the same grouped group
    under If-Match (ADR 0169); both requests land on the identical URL, so one
    identity-bearing fixture (observed shape, #979) serves both.
    """
    mock_change_location(mock_hmc, LPAR_UUID)
    resource = _VIOS_IDENTITY.replace(
        "</VirtualIOServer>",
        f"<VirtualSCSIMappings>{mappings}</VirtualSCSIMappings>\n</VirtualIOServer>",
    )
    inventory = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>
      {resource}
      </content></entry></feed>"""
    mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"
    ).mock(
        return_value=httpx.Response(200, text=inventory, headers={"ETag": '"etag-1"'})
    )
    return mock_hmc.post(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"
    ).mock(return_value=httpx.Response(200, text=""))


def test_detach_storage_mapping_posts_parent_vios(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    posted = _mock_detach_reads(mock_hmc, _mapping("vtscsi0") + _mapping("vtscsi1"))

    guard = AsyncMock(return_value=LPAR_UUID)
    with patch(
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        new=guard,
    ):
        assert hmc_detach_storage_mapping(VIOS_UUID, "vhost0/vtscsi0") == {
            "mapping_id": "vhost0/vtscsi0",
            "change_location": {
                "current_profile_sync": "Disabled",
                "lives_in": "current-configuration",
                "profile_name": None,
            },
        }

    guard.assert_awaited_once_with(
        ANY,
        None,
        LPAR_UUID,
        ownership_override=False,
    )
    assert posted.called
    body = posted.calls.last.request.content.decode()
    assert "vtscsi0<" not in body
    assert "vtscsi1<" in body


@pytest.mark.parametrize(
    ("mappings", "message"),
    [
        (_mapping("vtscsi0") + _mapping("vtscsi0"), "ambiguous"),
        (_mapping("vtscsi0", _LPAR_LINK + "/"), "does not identify its client LPAR"),
        (_mapping("vtscsi1"), "was not found"),
    ],
    ids=["ambiguous", "unparseable-lpar-link", "missing"],
)
def test_detach_storage_mapping_fails_closed_before_authorizing(
    monkeypatch, mock_hmc, mappings, message
):
    _hmc_env(monkeypatch)
    posted = _mock_detach_reads(mock_hmc, mappings)

    guard = AsyncMock(return_value=LPAR_UUID)
    with (
        patch(
            "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
            new=guard,
        ),
        pytest.raises(ValueError, match=message),
    ):
        hmc_detach_storage_mapping(VIOS_UUID, "vhost0/vtscsi0")

    guard.assert_not_awaited()
    assert not posted.called


def test_add_network_adapter_builds_xml(monkeypatch, mock_hmc):
    """hmc_add_network_adapter maps its args into a ClientNetworkAdapter doc."""
    _hmc_env(monkeypatch)
    read = mock_change_location(mock_hmc, LPAR_UUID, "On")
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(
        return_value=httpx.Response(
            201, text=_feed(ADAPTER_UUID, "ClientNetworkAdapter")
        )
    )
    result = hmc_add_network_adapter(
        LPAR_UUID,
        port_vlan_id=42,
        slot_number=3,
        virtual_switch_id=1,
        tagged=True,
        mac_address="001122334455",
    )
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "<ClientNetworkAdapter" in body
    assert '<PortVLANID kb="CUR" kxe="false">42</PortVLANID>' in body
    assert '<VirtualSlotNumber kb="COD" kxe="false">3</VirtualSlotNumber>' in body
    assert '<VirtualSwitchID kb="ROR" kxe="false">1</VirtualSwitchID>' in body
    assert (
        '<TaggedVLANSupported kb="CUA" kxe="false">true</TaggedVLANSupported>' in body
    )
    assert '<MACAddress kb="CUR" kxe="false">001122334455</MACAddress>' in body
    assert result["UUID"] == ADAPTER_UUID
    # #981: the resource keeps its top-level keys; the location rides beside them.
    assert result["change_location"] == {
        "current_profile_sync": "On",
        "lives_in": "current-configuration-and-profile",
        "profile_name": None,
    }
    assert (
        read.calls[0].request.url.path == f"/rest/api/uom/LogicalPartition/{LPAR_UUID}"
    )


@pytest.mark.parametrize("port_vlan_id", [0, 4095, 100000])
def test_add_network_adapter_refuses_out_of_range_pvid_before_any_read(
    monkeypatch, mock_hmc, port_vlan_id
):
    """A PVID outside 1-4094 is refused before the partition is resolved or read."""
    _hmc_env(monkeypatch)
    read = mock_change_location(mock_hmc, LPAR_UUID, "On")
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    )
    with pytest.raises(
        Exception, match=r"port_vlan_id .* must be a VLAN id from 1 to 4094"
    ):
        hmc_add_network_adapter(LPAR_UUID, port_vlan_id=port_vlan_id)
    assert not route.called
    assert not read.called


def test_add_vscsi_adapter_builds_xml(monkeypatch, mock_hmc):
    """hmc_add_vscsi_adapter maps vios_partition_id/vios_slot into the doc."""
    _hmc_env(monkeypatch)
    mock_change_location(mock_hmc, LPAR_UUID)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/VirtualSCSIClientAdapter"
    ).mock(
        return_value=httpx.Response(
            201, text=_feed(ADAPTER_UUID, "VirtualSCSIClientAdapter")
        )
    )
    result = hmc_add_vscsi_adapter(
        LPAR_UUID, vios_partition_id=7, vios_slot=11, slot_number=4
    )
    assert result["change_location"]["lives_in"] == "current-configuration"
    body = route.calls.last.request.content.decode()
    assert "<VirtualSCSIClientAdapter" in body
    assert (
        '<RemoteLogicalPartitionID kb="CUR" kxe="false">7</RemoteLogicalPartitionID>'
        in body
    )
    assert '<RemoteSlotNumber kb="CUA" kxe="false">11</RemoteSlotNumber>' in body
    assert '<VirtualSlotNumber kb="COD" kxe="false">4</VirtualSlotNumber>' in body


def test_add_vfc_adapter_builds_xml(monkeypatch, mock_hmc):
    """hmc_add_vfc_adapter maps vios_partition_id/vios_slot into the doc."""
    _hmc_env(monkeypatch)
    mock_change_location(mock_hmc, LPAR_UUID)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/VirtualFibreChannelClientAdapter"
    ).mock(
        return_value=httpx.Response(
            201, text=_feed(ADAPTER_UUID, "VirtualFibreChannelClientAdapter")
        )
    )
    result = hmc_add_vfc_adapter(LPAR_UUID, vios_partition_id=7, vios_slot=11)
    assert result["change_location"]["current_profile_sync"] == "Disabled"
    body = route.calls.last.request.content.decode()
    assert "<VirtualFibreChannelClientAdapter" in body
    assert (
        '<ConnectingPartitionID kb="CUD" kxe="false">7</ConnectingPartitionID>' in body
    )
    assert (
        '<ConnectingVirtualSlotNumber kb="CUD" kxe="false">11</ConnectingVirtualSlotNumber>'
        in body
    )


def test_delete_adapter_returns_confirmation(monkeypatch, mock_hmc):
    """hmc_delete_adapter DELETEs the adapter and returns a confirmation."""
    _hmc_env(monkeypatch)
    mock_change_location(mock_hmc, LPAR_UUID)
    route = mock_hmc.delete(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/VirtualSCSIClientAdapter/{ADAPTER_UUID}"
    ).mock(return_value=httpx.Response(204))
    result = hmc_delete_adapter(LPAR_UUID, "VirtualSCSIClientAdapter", ADAPTER_UUID)
    assert route.called
    assert result.startswith(
        f"Deleted VirtualSCSIClientAdapter {ADAPTER_UUID} from {LPAR_UUID}. "
        "CurrentProfileSync is Disabled: the change lives only in the current configuration"
    )


# ---------------------------------------------------------------------- #
# Volume groups / virtual disks / storage mapping
# ---------------------------------------------------------------------- #


def test_list_volume_groups(monkeypatch, mock_hmc):
    """hmc_list_volume_groups GETs the VIOS VolumeGroup collection."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup", GroupName="vg_rootvg")
        )
    )
    result = hmc_list_volume_groups(VIOS_UUID)
    assert result[0] == {
        "uuid": VG_UUID,
        "name": "vg_rootvg",
        "capacity_gib": None,
        "free_space_gib": None,
        "free_space_diagnostic": None,
    }


def test_create_volume_group_builds_xml(monkeypatch, mock_hmc):
    """hmc_create_volume_group PUTs a VolumeGroup doc with the PV list."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(201, text=_feed(VG_UUID, "VolumeGroup"))
    )
    hmc_create_volume_group(VIOS_UUID, "vg_data", ["hdisk10", "hdisk11"])
    body = route.calls.last.request.content.decode()
    assert '<GroupName kb="CUR" kxe="false">vg_data</GroupName>' in body
    assert body.count("<PhysicalVolume ") == 2
    assert "hdisk10" in body and "hdisk11" in body


def test_create_volume_group_declares_hmc_resource_result() -> None:
    assert inspect.signature(hmc_create_volume_group).return_annotation == (
        "dict[str, Any] | None"
    )


def test_create_virtual_disk_builds_xml(monkeypatch, mock_hmc):
    """hmc_create_virtual_disk POSTs the fetched VolumeGroup with a new VirtualDisk."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup", GroupName="rootvg")
        )
    )
    path = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup"), headers={"ETag": '"etag-1"'}
        )
    )
    route = mock_hmc.post(path).mock(
        return_value=httpx.Response(201, text=_feed(VG_UUID, "VolumeGroup"))
    )
    hmc_create_virtual_disk(VIOS_UUID, VG_UUID, "lv_boot", 51200)
    body = route.calls.last.request.content.decode()
    assert "<VirtualDisks" in body
    assert '<DiskName kb="CUR" kxe="false">lv_boot</DiskName>' in body
    assert '<DiskCapacity kb="CUR" kxe="false">50</DiskCapacity>' in body


def _mapping_routes(mock_hmc):
    """Grouped VIOS GET (empty mapping set, with ETag) and the POST back to it.

    Returns ``(get, post)``: the GET carries an ``AssociatedManagedSystem`` link, the
    same shape a real V10R3 VIOS response carries (ADR 0179), so the href the POST
    builds is system-scoped.
    """
    mock_change_location(mock_hmc, LPAR_UUID)
    path = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"
    get = mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200,
            text=f'<VirtualIOServer xmlns="{UOM_NS}">'
            f"<Metadata><Atom><AtomID>{VIOS_UUID}</AtomID></Atom></Metadata>"
            f'<PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>'
            f'<AssociatedManagedSystem kb="CUD" kxe="false" rel="related" '
            f'href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"/>'
            "<VirtualSCSIMappings/></VirtualIOServer>",
            headers={"ETag": "etag-1"},
        )
    )
    post = mock_hmc.post(path).mock(
        return_value=httpx.Response(201, text=_feed(VIOS_UUID, "VirtualIOServer"))
    )
    return get, post


def test_map_storage_reorders_virtual_disk_default(monkeypatch, mock_hmc):
    """hmc_map_storage_to_lpar maps the default VirtualDisk storage_kind."""
    _hmc_env(monkeypatch)
    _, route = _mapping_routes(mock_hmc)
    result = hmc_map_storage_to_lpar(VIOS_UUID, "lv_boot", LPAR_UUID)
    assert result["change_location"]["lives_in"] == "current-configuration"
    body = route.calls.last.request.content.decode()
    assert "<VirtualSCSIMapping" in body
    # storage_kind lands as the element name; storage_name is DiskName.
    assert '<VirtualDisk schemaVersion="V1_0">' in body
    assert '<DiskName kb="CUR" kxe="false">lv_boot</DiskName>' in body
    assert (
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition/{LPAR_UUID}"
        in body
    )


def test_map_storage_physical_volume_with_target_device(monkeypatch, mock_hmc):
    """PhysicalVolume storage_kind uses VolumeName and emits TargetDevice."""
    _hmc_env(monkeypatch)
    _, route = _mapping_routes(mock_hmc)
    hmc_map_storage_to_lpar(
        VIOS_UUID,
        "hdisk5",
        LPAR_UUID,
        storage_kind="PhysicalVolume",
        target_device="vtscsi0",
    )
    body = route.calls.last.request.content.decode()
    assert '<PhysicalVolume schemaVersion="V1_0">' in body
    assert '<VolumeName kb="CUR" kxe="false">hdisk5</VolumeName>' in body
    assert '<TargetName kb="CUR" kxe="false">vtscsi0</TargetName>' in body
    assert (
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition/{LPAR_UUID}"
        in body
    )


def test_map_storage_invalid_kind_raises(monkeypatch, mock_hmc):
    """An invalid storage_kind is rejected before any write is sent, GET included."""
    _hmc_env(monkeypatch)
    get, post = _mapping_routes(mock_hmc)
    with pytest.raises(
        ValueError, match="storage_kind must be PhysicalVolume or VirtualDisk"
    ):
        hmc_map_storage_to_lpar(VIOS_UUID, "lv_boot", LPAR_UUID, storage_kind="Bogus")
    assert not get.called
    assert not post.called


# ---------------------------------------------------------------------- #
# Virtual media repository
# ---------------------------------------------------------------------- #

_BARE_VG_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>clientvg1</GroupName>
        <VirtualDisks/>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

_VMLIB_VG_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>clientvg1</GroupName>
        <MediaRepositories>
          <VirtualMediaRepository>
            <RepositoryName>VMLibrary</RepositoryName>
            <RepositorySize>7000</RepositorySize>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""


def test_create_media_repository_builds_xml(monkeypatch, mock_hmc):
    """hmc_create_media_repository GETs the VG then POSTs a VMLibrary repository doc.

    The read-modify-write pattern is used on V10R3 firmware; no kb= attributes
    are emitted by ET.tostring() — assertions check for element/text presence only.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup", GroupName="rootvg")
        )
    )
    vg_path = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_BARE_VG_FEED, headers={"ETag": '"etag-1"'}
        )
    )
    route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(201, text=_feed(VG_UUID, "VolumeGroup"))
    )
    hmc_create_media_repository(VIOS_UUID, VG_UUID, 40960)
    body = route.calls.last.request.content.decode()
    assert "VirtualMediaRepository" in body
    assert "VMLibrary" in body
    assert "RepositorySize>40</" in body  # 40960 MiB is 40 GiB


def test_create_optical_media_builds_xml(monkeypatch, mock_hmc):
    """hmc_create_optical_media GETs the VG then POSTs a blank-media doc."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup", GroupName="rootvg")
        )
    )
    vg_path = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_VMLIB_VG_FEED, headers={"ETag": '"etag-1"'}
        )
    )
    route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(201, text=_feed(VG_UUID, "VolumeGroup"))
    )
    hmc_create_optical_media(VIOS_UUID, VG_UUID, "aix.iso", 4096)
    body = route.calls.last.request.content.decode()
    assert "VirtualOpticalMedia" in body
    assert "aix.iso" in body
    assert re.search(r"<Size\b[^>]*>4</", body)  # 4096 MiB is 4 GiB
    assert "MountType" in body


def test_delete_media_repository_returns_confirmation(monkeypatch, mock_hmc):
    """hmc_delete_media_repository GETs the VG, removes VMLibrary, POSTs, and confirms.

    The GET is used twice: once by list_optical_media (safety check) and once by
    delete_media_repository (read-modify-write). Both calls hit the same mock.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup").mock(
        return_value=httpx.Response(
            200, text=_feed(VG_UUID, "VolumeGroup", GroupName="rootvg")
        )
    )
    vg_path = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    # Both list_optical_media and delete_media_repository GET the same path.
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_VMLIB_VG_FEED, headers={"ETag": '"etag-1"'}
        )
    )
    route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(201, text=_feed(VG_UUID, "VolumeGroup"))
    )
    result = hmc_delete_media_repository(VIOS_UUID, VG_UUID)
    # POST body must not contain MediaRepositories (it was removed by the mutation).
    body = route.calls.last.request.content.decode()
    assert "MediaRepositories" not in body
    assert result == f"Deleted media repository from VolumeGroup {VG_UUID}"


# ---------------------------------------------------------------------- #
# Clusters / Shared Storage Pools
# ---------------------------------------------------------------------- #


def test_list_clusters(monkeypatch, mock_hmc):
    """hmc_list_clusters GETs the Cluster collection."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/Cluster").mock(
        return_value=httpx.Response(200, text=_feed(CLUSTER_UUID, "Cluster"))
    )
    result = hmc_list_clusters()
    assert result[0]["UUID"] == CLUSTER_UUID


def test_shared_storage_pools_lists_all(monkeypatch, mock_hmc):
    """hmc_list_shared_storage_pools() GETs the SharedStoragePool collection."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/SharedStoragePool").mock(
        return_value=httpx.Response(200, text=_feed(SSP_UUID, "SharedStoragePool"))
    )
    result = hmc_list_shared_storage_pools()
    assert result[0]["UUID"] == SSP_UUID


def test_get_shared_storage_pool_gets_one(monkeypatch, mock_hmc):
    """hmc_get_shared_storage_pool GETs one SSP by UUID."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/SharedStoragePool/{SSP_UUID}").mock(
        return_value=httpx.Response(200, text=_feed(SSP_UUID, "SharedStoragePool"))
    )
    result = hmc_get_shared_storage_pool(SSP_UUID)
    assert result["UUID"] == SSP_UUID


def test_create_logical_unit_submits_job(monkeypatch, mock_hmc):
    """hmc_create_logical_unit PUTs a CreateLogicalUnit job with the params."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{CLUSTER_UUID}/do/CreateLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    result = hmc_create_logical_unit(
        CLUSTER_UUID, "lu_data", 100, lu_type="THICK", device_type="VirtualIO_Image"
    )
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "CreateLogicalUnit</OperationName>" in body
    # Params render as ParameterName/ParameterValue pairs.
    assert '<ParameterName kb="ROR" kxe="false">LUName</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">lu_data</ParameterValue>' in body
    assert '<ParameterName kb="ROR" kxe="false">LUSize</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">100</ParameterValue>' in body
    assert '<ParameterValue kb="CUR" kxe="false">THICK</ParameterValue>' in body
    assert (
        '<ParameterValue kb="CUR" kxe="false">VirtualIO_Image</ParameterValue>' in body
    )
    assert "ClonedFrom" not in body
    assert result["Resource"]["JobID"] == JOB_ID


@pytest.mark.parametrize(
    "lu_type,device_type,match",
    [
        ("SPARSE", "VirtualIO_Disk", "lu_type"),
        ("THIN", "PhysicalDisk", "device_type"),
    ],
)
def test_create_logical_unit_rejects_invalid_types_before_resource_request(
    monkeypatch, mock_hmc, lu_type, device_type, match
):
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match=match):
        hmc_create_logical_unit(
            CLUSTER_UUID,
            "lu_data",
            100,
            lu_type=lu_type,
            device_type=device_type,
        )
    assert {call.request.url.path for call in mock_hmc.calls} == {"/rest/api/web/Logon"}


def test_create_logical_unit_with_clone(monkeypatch, mock_hmc):
    """cloned_from adds a ClonedFrom param to the job body."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{CLUSTER_UUID}/do/CreateLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_create_logical_unit(CLUSTER_UUID, "lu_clone", 50, cloned_from="udid-1234")
    body = route.calls.last.request.content.decode()
    assert '<ParameterName kb="ROR" kxe="false">ClonedFrom</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">udid-1234</ParameterValue>' in body


def test_delete_logical_unit_submits_job(monkeypatch, mock_hmc):
    """hmc_delete_logical_unit PUTs a DeleteLogicalUnit job by UDID."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{CLUSTER_UUID}/do/DeleteLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    result = hmc_delete_logical_unit(CLUSTER_UUID, "udid-1234")
    body = route.calls.last.request.content.decode()
    assert "DeleteLogicalUnit</OperationName>" in body
    assert '<ParameterName kb="ROR" kxe="false">LogicalUnitUDID</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">udid-1234</ParameterValue>' in body
    assert result["Resource"]["JobID"] == JOB_ID


# ---------------------------------------------------------------------- #
# wait=True path: create/delete LU blocks until job reaches terminal state
# ---------------------------------------------------------------------- #


def test_create_logical_unit_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    """hmc_create_logical_unit(wait=True) submits then polls until COMPLETED_OK."""
    _hmc_env(monkeypatch)
    submit_route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{CLUSTER_UUID}/do/CreateLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_create_logical_unit(
        CLUSTER_UUID, "lu_data", 100, wait=True, timeout_seconds=60, poll_interval=1
    )
    assert submit_route.called
    assert poll_route.called
    assert result["Resource"]["Status"] == "COMPLETED_OK"


def test_delete_logical_unit_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    """hmc_delete_logical_unit(wait=True) submits then polls until COMPLETED_OK."""
    _hmc_env(monkeypatch)
    submit_route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{CLUSTER_UUID}/do/DeleteLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_delete_logical_unit(
        CLUSTER_UUID, "udid-1234", wait=True, timeout_seconds=60, poll_interval=1
    )
    assert submit_route.called
    assert poll_route.called
    assert result["Resource"]["Status"] == "COMPLETED_OK"


def test_mount_optical_media_keeps_the_mapping_keys_beside_its_location(
    monkeypatch, mock_hmc
):
    """#981: scripts/live_test reads the mapping identity from top-level keys."""
    _hmc_env(monkeypatch)
    location = ChangeLocation("Disabled", "current-configuration")
    mount = AsyncMock(
        return_value=StorageMapResult(LPAR_UUID, {"UUID": "mapping-1"}, location)
    )
    monkeypatch.setattr(
        "hmcpctl.server_tools.storage.resources.mount_optical_media", mount
    )

    result = hmc_mount_optical_media(VIOS_UUID, "install.iso", LPAR_UUID)

    assert result == {
        "UUID": "mapping-1",
        "change_location": {
            "current_profile_sync": "Disabled",
            "lives_in": "current-configuration",
            "profile_name": None,
        },
    }


def test_mount_optical_media_without_a_resource_still_reports_its_location(
    monkeypatch, mock_hmc
):
    _hmc_env(monkeypatch)
    location = ChangeLocation(None, "unknown")
    monkeypatch.setattr(
        "hmcpctl.server_tools.storage.resources.mount_optical_media",
        AsyncMock(return_value=StorageMapResult(LPAR_UUID, None, location)),
    )

    result = hmc_mount_optical_media(VIOS_UUID, "install.iso", LPAR_UUID)

    assert result == {
        "change_location": {
            "current_profile_sync": None,
            "lives_in": "unknown",
            "profile_name": None,
        }
    }
