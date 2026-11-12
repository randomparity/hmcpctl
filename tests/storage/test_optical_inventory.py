"""Transport-layer tests for optical mapping inventory and operations."""

from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.profiles.sync import ChangeLocation
from hmcpctl.operations.storage import optical

UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"
SYSTEM_UUID = "00000000-0000-0000-0000-000000000004"

OPTICAL_MAPPINGS_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:uom="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
  <entry>
    <content>
      <VirtualIOServer>
        <VirtualSCSIMappings>
          <VirtualSCSIMapping>
            <Metadata><Atom/></Metadata>
            <ServerAdapter><AdapterName>vhost0</AdapterName></ServerAdapter>
            <Storage>
              <VirtualOpticalMedia>
                <MediaName>aix72.iso</MediaName>
              </VirtualOpticalMedia>
            </Storage>
            <TargetDevice><VirtualOpticalTargetDevice><TargetName>vtopt0</TargetName></VirtualOpticalTargetDevice></TargetDevice>
            <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-uuid-001"/>
          </VirtualSCSIMapping>
          <VirtualSCSIMapping>
            <Metadata><Atom/></Metadata>
            <ServerAdapter><AdapterName>vhost1</AdapterName></ServerAdapter>
            <Storage>
              <VirtualDisk>
                <DiskName>lv_boot</DiskName>
              </VirtualDisk>
            </Storage>
            <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-uuid-002"/>
          </VirtualSCSIMapping>
          <VirtualSCSIMapping>
            <Metadata><Atom/></Metadata>
            <ServerAdapter><AdapterName>vhost2</AdapterName></ServerAdapter>
            <Storage>
              <VirtualOpticalMedia>
                <MediaName>rhel8.iso</MediaName>
              </VirtualOpticalMedia>
            </Storage>
            <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-uuid-001"/>
          </VirtualSCSIMapping>
        </VirtualSCSIMappings>
      </VirtualIOServer>
    </content>
  </entry>
</feed>
"""

EMPTY_MAPPINGS_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VirtualIOServer>
        <VirtualSCSIMappings/>
      </VirtualIOServer>
    </content>
  </entry>
</feed>
"""

CREATE_MAPPING_RESPONSE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VirtualSCSIMapping>
        <UUID>mapping-uuid-new-001</UUID>
        <Storage>
          <VirtualOpticalMedia>
            <MediaName>test.iso</MediaName>
          </VirtualOpticalMedia>
        </Storage>
        <TargetDevice>cd1</TargetDevice>
        <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-uuid-001"/>
      </VirtualSCSIMapping>
    </content>
  </entry>
</feed>
"""

VIOS_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"


@pytest.mark.asyncio
async def test_list_optical_mappings_filters_optical_only(mock_hmc):
    """list_optical_mappings returns only VirtualOpticalMedia-backed mappings."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=OPTICAL_MAPPINGS_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_optical_mappings(VIOS_UUID)

    assert len(mappings) == 2
    assert all(m.get("Storage", {}).get("VirtualOpticalMedia") for m in mappings)

    media_names = [m["Storage"]["VirtualOpticalMedia"]["MediaName"] for m in mappings]
    assert "aix72.iso" in media_names
    assert "rhel8.iso" in media_names


@pytest.mark.asyncio
async def test_list_optical_mappings_empty(mock_hmc):
    """list_optical_mappings returns empty list for no mappings."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=EMPTY_MAPPINGS_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_optical_mappings(VIOS_UUID)

    assert mappings == []


@pytest.mark.asyncio
async def test_list_optical_mappings_propagates_bad_request(mock_hmc):
    """A rejected documented group is an API error, not an empty inventory."""
    mock_hmc.get(VIOS_PATH).mock(return_value=httpx.Response(400, text="bad request"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.list_optical_mappings(VIOS_UUID)

    assert raised.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [CREATE_MAPPING_RESPONSE, ""])
@pytest.mark.parametrize("consumer", ["client", "operation"])
async def test_create_optical_mapping_submits_document(
    mock_hmc, monkeypatch, consumer, response
):
    """create_optical_mapping submits a focused document to the VIOS endpoint."""
    mock_hmc.get(VIOS_PATH).mock(
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
    post_route = mock_hmc.post(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=response)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        if consumer == "client":
            result = await hmc.create_optical_mapping(
                VIOS_UUID, "test.iso", LPAR_UUID, target_device="cd1"
            )
            if result is not None:
                assert result["ResourceType"] == "VirtualSCSIMapping"
            resource = result["Resource"] if result is not None else None
        else:
            location = ChangeLocation("Disabled", "current-configuration")
            monkeypatch.setattr(
                optical,
                "resolve_and_authorize_lpar_mutation",
                AsyncMock(return_value=LPAR_UUID),
            )
            monkeypatch.setattr(
                optical, "read_change_location", AsyncMock(return_value=location)
            )
            result = await optical.mount_optical_media(
                hmc, VIOS_UUID, LPAR_UUID, media_name="test.iso", target_device="cd1"
            )
            assert result.change_location == location
            assert result.lpar_uuid == LPAR_UUID
            resource = result.raw
            assert resource is None or "Resource" not in resource

    assert post_route.called
    req_body = post_route.calls.last.request.content.decode()
    assert "VirtualOpticalMedia" in req_body
    assert "test.iso" in req_body
    assert LPAR_UUID in req_body
    assert "<TargetDevice" in req_body
    assert '<TargetName kb="CUR" kxe="false">cd1</TargetName>' in req_body
    if not response:
        assert resource is None
        return
    assert resource["UUID"] == "mapping-uuid-new-001"
    assert resource["Storage"]["VirtualOpticalMedia"]["MediaName"] == "test.iso"
