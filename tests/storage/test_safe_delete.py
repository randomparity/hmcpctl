"""Tests for safe ISO and media-repository deletion operations."""

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.storage.resources import (
    delete_media_repository,
    delete_optical_media,
    delete_virtual_disk,
)

VIOS_UUID = "00000000-0000-0000-0000-000000000003"
VG_UUID = "22222222-2222-2222-2222-222222220002"
MEDIA_NAME = "test-image.iso"

VG_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
VIOS_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"

EMPTY_REPO_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia><Metadata><Atom/></Metadata></OpticalMedia>
            <RepositoryName>VMLibrary</RepositoryName>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

NONEMPTY_REPO_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia>
              <VirtualOpticalMedia>
                <MediaName>test-image.iso</MediaName>
                <Size>8.8262</Size>
              </VirtualOpticalMedia>
            </OpticalMedia>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

MEDIA_VG_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia>
              <VirtualOpticalMedia>
                <MediaName>test-image.iso</MediaName>
                <Size>8.8262</Size>
              </VirtualOpticalMedia>
            </OpticalMedia>
            <RepositoryName>VMLibrary</RepositoryName>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

NO_MAPPINGS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VirtualIOServer>
        <VirtualSCSIMappings/>
      </VirtualIOServer>
    </content>
  </entry>
</feed>"""

MOUNTED_MAPPINGS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VirtualIOServer>
        <VirtualSCSIMappings>
          <VirtualSCSIMapping>
            <ServerAdapter><AdapterName>vhost0</AdapterName></ServerAdapter>
            <Storage>
              <VirtualOpticalMedia>
                <MediaName>test-image.iso</MediaName>
              </VirtualOpticalMedia>
            </Storage>
            <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-001"/>
          </VirtualSCSIMapping>
        </VirtualSCSIMappings>
      </VirtualIOServer>
    </content>
  </entry>
</feed>"""

# &#10; (newline) and &#9; (tab) are legal XML character references, so an HMC
# can hand back a MediaName carrying raw control characters.
HOSTILE_MEDIA_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia>
              <VirtualOpticalMedia>
                <MediaName>evil&#10;image&#9;.iso</MediaName>
                <Size>8.8262</Size>
              </VirtualOpticalMedia>
            </OpticalMedia>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

HOSTILE_MOUNTED_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content>
      <VirtualIOServer>
        <VirtualSCSIMappings>
          <VirtualSCSIMapping>
            <ServerAdapter><AdapterName>vhost1</AdapterName></ServerAdapter>
            <Storage>
              <VirtualOpticalMedia>
                <MediaName>evil'name.iso</MediaName>
              </VirtualOpticalMedia>
            </Storage>
            <AssociatedLogicalPartition rel="related" href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/00000000-0000-4000-8000-000000000001/LogicalPartition/lpar-001"/>
          </VirtualSCSIMapping>
        </VirtualSCSIMappings>
      </VirtualIOServer>
    </content>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_delete_media_repository_refuses_nonempty(mock_hmc):
    """delete_media_repository raises when repository contains images."""
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=NONEMPTY_REPO_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        with pytest.raises(HMCError, match="contains 1 image"):
            await delete_media_repository(hmc, VIOS_UUID, VG_UUID)


@pytest.mark.asyncio
async def test_delete_media_repository_succeeds_when_empty(mock_hmc):
    """delete_media_repository succeeds when repository is empty."""
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(
            200, text=EMPTY_REPO_FEED, headers={"ETag": '"etag-1"'}
        )
    )
    post = mock_hmc.post(VG_PATH).mock(
        return_value=httpx.Response(200, text=EMPTY_REPO_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        result = await delete_media_repository(hmc, VIOS_UUID, VG_UUID)

    assert result == VIOS_UUID
    assert post.call_count == 1
    assert "MediaRepositories" not in post.calls.last.request.content.decode()


@pytest.mark.asyncio
async def test_delete_media_repository_refuses_medium_that_appeared_since_the_check(
    mock_hmc,
):
    """A medium created between the operation's check and the client's own read is
    still refused, with nothing written (#1012).

    delete_media_repository's list_optical_media emptiness check and the client's
    read-modify-write GET are two independent reads. This drives that interleaving
    literally rather than a single static GET: the first GET (the operation's own
    check) sees an empty repository, and the second GET (the client's own read)
    sees one that has since gained a medium.
    """
    mock_hmc.get(VG_PATH).mock(
        side_effect=[
            httpx.Response(200, text=EMPTY_REPO_FEED),
            httpx.Response(200, text=MEDIA_VG_FEED, headers={"ETag": '"etag-1"'}),
        ]
    )
    post_route = mock_hmc.post(VG_PATH).mock(
        return_value=httpx.Response(200, text=EMPTY_REPO_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        with pytest.raises(HMCError, match=r"contains 1 image\(s\): 'test-image\.iso'"):
            await delete_media_repository(hmc, VIOS_UUID, VG_UUID)

    assert not post_route.called


@pytest.mark.asyncio
async def test_delete_optical_media_refuses_when_mounted(mock_hmc):
    """delete_optical_media raises when media is mounted to an LPAR."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=MOUNTED_MAPPINGS_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        with pytest.raises(HMCError, match="mounted on 1 LPAR"):
            await delete_optical_media(hmc, VIOS_UUID, VG_UUID, MEDIA_NAME)


@pytest.mark.asyncio
async def test_delete_optical_media_succeeds_when_unmounted(mock_hmc):
    """delete_optical_media succeeds when no optical mappings reference it.

    delete_optical_media:
      1. GETs ViosSCSIMapping path (list_optical_mappings) — returns no mappings.
      2. GETs VG_PATH (hmc.delete_optical_media read step) — returns MEDIA_VG_FEED.
      3. POSTs VG_PATH with the media node removed.
    """
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=NO_MAPPINGS_FEED)
    )
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(
            200, text=MEDIA_VG_FEED, headers={"ETag": '"etag-1"'}
        )
    )
    mock_hmc.post(VG_PATH).mock(return_value=httpx.Response(200, text=EMPTY_REPO_FEED))

    config = make_config()
    async with HMCClient(config) as hmc:
        result = await delete_optical_media(hmc, VIOS_UUID, VG_UUID, MEDIA_NAME)

    assert result is not None


@pytest.mark.asyncio
async def test_delete_virtual_disk_refusal_is_repr_quoted(mock_hmc):
    """The refusal message repr-quotes the disk and LPAR names it echoes."""
    disk_name = "evil'name.iso"
    mapping = {
        "Storage": {
            "VirtualDisk": {
                "href": (
                    f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/"
                    f"{VG_UUID}/VirtualDisk/{disk_name}"
                )
            }
        },
        "AssociatedLogicalPartition": {"PartitionName": "lpar-001"},
    }

    config = make_config()
    async with HMCClient(config) as hmc:
        hmc.list_storage_mappings = AsyncMock(return_value=[mapping])
        with pytest.raises(HMCError) as exc_info:
            await delete_virtual_disk(hmc, VIOS_UUID, VG_UUID, disk_name)

    message = str(exc_info.value)
    assert repr(disk_name) in message
    assert repr("lpar-001") in message


@pytest.mark.asyncio
async def test_delete_media_repository_refusal_is_repr_quoted(mock_hmc):
    """HMC-derived MediaName text cannot carry control characters into str()."""
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=HOSTILE_MEDIA_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await delete_media_repository(hmc, VIOS_UUID, VG_UUID)

    message = str(exc_info.value)
    hostile_names = "evil\nimage\t.iso"
    assert repr(hostile_names) in message
    assert not any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "\u2028\u2029" for ch in message
    )


@pytest.mark.asyncio
async def test_delete_optical_media_refusal_is_repr_quoted(mock_hmc):
    """The mounted-media refusal repr-quotes the names it echoes."""
    media_name = "evil'name.iso"
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=HOSTILE_MOUNTED_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await delete_optical_media(hmc, VIOS_UUID, VG_UUID, media_name)

    message = str(exc_info.value)
    assert repr(media_name) in message


V10R3_MAPPINGS = (Path(__file__).with_name("vscsi_mapping_v10r3.xml")).read_text(
    encoding="utf-8"
)
UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
GROUP_LINK = (
    f"https://hmc.example.invalid/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup"
)


@pytest.mark.asyncio
async def test_delete_virtual_disk_refuses_disk_mapped_inline_by_name(mock_hmc):
    """V10R3 names a mapped disk inline by DiskName, with no href (#936)."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(
            200,
            text=f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>
      <VirtualIOServer xmlns="{UOM_NS}">{V10R3_MAPPINGS}</VirtualIOServer>
    </content></entry></feed>""",
        )
    )
    vg_get = mock_hmc.get(VG_PATH)
    vg_post = mock_hmc.post(VG_PATH)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="'vd-R1': it is mapped to"):
            await delete_virtual_disk(hmc, VIOS_UUID, VG_UUID, "vd-R1")

    assert not vg_get.called and not vg_post.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_link", "refused"),
    [
        (None, True),
        (f"{GROUP_LINK}/{VG_UUID}", True),
        (f"{GROUP_LINK}/{VG_UUID.upper()}", True),
        (f"{GROUP_LINK}/other-vg", False),
    ],
    ids=["no-group-link", "same-group", "same-group-other-case", "other-group"],
)
async def test_delete_virtual_disk_inline_match_honours_the_group_link(
    mock_hmc, group_link, refused
):
    backing = {"DiskName": "vd-1"}
    if group_link:
        backing["VolumeGroup"] = {"href": group_link, "rel": "related"}
    mapping = {"Storage": {"VirtualDisk": backing}, "AssociatedLogicalPartition": {}}

    async with HMCClient(make_config()) as hmc:
        hmc.list_storage_mappings = AsyncMock(return_value=[mapping])
        hmc.delete_virtual_disk = AsyncMock(return_value=None)
        if refused:
            with pytest.raises(HMCError, match="it is mapped to"):
                await delete_virtual_disk(hmc, VIOS_UUID, VG_UUID, "vd-1")
        else:
            await delete_virtual_disk(hmc, VIOS_UUID, VG_UUID, "vd-1")
    assert hmc.delete_virtual_disk.await_count == (0 if refused else 1)


@pytest.fixture(autouse=True)
def scoped_volume_group_inventory(monkeypatch):
    """Supply membership at the HMC boundary for these media/guard tests."""
    lookup = AsyncMock(
        return_value=[{"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}}]
    )
    monkeypatch.setattr(HMCClient, "list_volume_groups", lookup)
    return lookup
