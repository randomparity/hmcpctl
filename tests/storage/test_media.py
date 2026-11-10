"""Tests for Virtual Media Repository / Virtual Optical Media (VolumeGroup POST).

The media-repository operations use a read-modify-write pattern: GET the full
VolumeGroup XML, mutate the in-memory element tree, then POST the modified XML
back.  These tests verify that the correct nodes are present in the POST body
after the mutation.
"""

import re

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError


@pytest.mark.asyncio
async def test_media_repository_rejects_xml_entities(mock_hmc):
    """An entity-bearing VolumeGroup GET body raises HMCError, not a raw DefusedXmlException."""
    document = '<!DOCTYPE x [<!ENTITY payload "expanded">]><x>&payload;</x>'
    mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    ).mock(return_value=httpx.Response(200, text=document))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="not valid XML"):
            await hmc.create_media_repository(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                2048,
            )


# Minimal VolumeGroup feed — no MediaRepositories block (bare VG).
_VG_FEED_BARE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>clientvg1</GroupName>
        <VirtualDisks/>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

# VolumeGroup feed with an existing VMLibrary (no media inside).
_VG_FEED_WITH_VMLIB = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>clientvg1</GroupName>
        <MediaRepositories>
          <VirtualMediaRepository>
            <RepositoryName>VMLibrary</RepositoryName>
            <RepositorySize>7</RepositorySize>
          </VirtualMediaRepository>
        </MediaRepositories>
      </VolumeGroup>
    </content>
  </entry>
</feed>"""

# Response after a successful POST — we just return the same bare feed.
_VG_POST_RESPONSE = _VG_FEED_BARE


@pytest.mark.asyncio
async def test_create_media_repository(mock_hmc):
    """create_media_repository GETs the VG, injects VMLibrary, then POSTs."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_VG_FEED_BARE, headers={"ETag": '"etag-1"'}
        )
    )
    post_route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(200, text=_VG_POST_RESPONSE)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.create_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            2048,
        )
    # POST body must contain the injected VMLibrary block.
    body = post_route.calls.last.request.content.decode()
    assert "VirtualMediaRepository" in body
    assert "VMLibrary" in body
    # RepositorySize is GiB on the HMC: 2048 MiB is sent as 2.
    assert re.search(r"RepositorySize>2</", body)


@pytest.mark.asyncio
@pytest.mark.parametrize("size_mib", [0, -1024, 1536, 1023])
async def test_create_media_repository_refuses_a_size_that_is_not_whole_gib(
    mock_hmc, size_mib
):
    """The HMC takes whole GiB; a MiB value that does not convert is refused unsent."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    get_route = mock_hmc.get(vg_path)
    post_route = mock_hmc.post(vg_path)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            ValueError, match="size_mib must be a positive multiple of 1024"
        ):
            await hmc.create_media_repository(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                size_mib,
            )

    assert not get_route.called
    assert not post_route.called


@pytest.mark.asyncio
async def test_create_media_repository_returns_matching_existing_repository(mock_hmc):
    """A matching create is idempotent and never rewrites the volume group."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(200, text=_VG_FEED_WITH_VMLIB)
    )
    post_route = mock_hmc.post(vg_path)

    async with HMCClient(make_config()) as hmc:
        result = await hmc.create_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            7168,
        )

    assert result["UUID"] == "22222222-2222-2222-2222-222222222222"
    assert result["Resource"]["MediaRepositories"]["VirtualMediaRepository"] == {
        "RepositoryName": "VMLibrary",
        "RepositorySize": "7",
    }
    assert not post_route.called


@pytest.mark.asyncio
async def test_create_media_repository_compares_sizes_numerically_in_gib(mock_hmc):
    """A stored "7.0" GiB is the same repository as a requested 7168 MiB."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    feed = _VG_FEED_WITH_VMLIB.replace(">7</RepositorySize>", ">7.0</RepositorySize>")
    mock_hmc.get(vg_path).mock(return_value=httpx.Response(200, text=feed))
    post_route = mock_hmc.post(vg_path)

    async with HMCClient(make_config()) as hmc:
        result = await hmc.create_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            7168,
        )

    assert result["UUID"] == "22222222-2222-2222-2222-222222222222"
    assert result["Resource"]["MediaRepositories"]["VirtualMediaRepository"] == {
        "RepositoryName": "VMLibrary",
        "RepositorySize": "7.0",
    }
    assert not post_route.called


@pytest.mark.asyncio
async def test_create_media_repository_returns_same_volume_group_on_repeat(mock_hmc):
    """Create and repeat expose the same repository nesting and Atom identity."""
    vios = "11111111-1111-1111-1111-111111111111"
    group = "22222222-2222-2222-2222-222222222222"
    path = f"/rest/api/uom/VirtualIOServer/{vios}/VolumeGroup/{group}"
    existing = _VG_FEED_WITH_VMLIB.replace(
        "<entry>", '<entry><title>clientvg1</title><link href="/group"/>'
    )
    mock_hmc.get(path).mock(
        side_effect=[
            httpx.Response(200, text=_VG_FEED_BARE, headers={"ETag": '"first"'}),
            httpx.Response(200, text=existing),
        ]
    )
    post = mock_hmc.post(path).mock(return_value=httpx.Response(200, text=existing))
    async with HMCClient(make_config()) as hmc:
        created = await hmc.create_media_repository(vios, group, 7168)
        repeated = await hmc.create_media_repository(vios, group, 7168)
    assert repeated == created
    assert repeated["UUID"] == group
    assert repeated["title"] == "clientvg1"
    assert repeated["link"] == "/group"
    repository = repeated["Resource"]["MediaRepositories"]["VirtualMediaRepository"]
    assert repository == {"RepositoryName": "VMLibrary", "RepositorySize": "7"}
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_create_media_repository_refuses_to_replace_different_size(mock_hmc):
    """Changing repository size requires a separately destructive operation."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(200, text=_VG_FEED_WITH_VMLIB)
    )
    post_route = mock_hmc.post(vg_path)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError,
            match=r"already exists with size 7 GiB; requested 2048 MiB \(2 GiB\)",
        ):
            await hmc.create_media_repository(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                2048,
            )

    assert not post_route.called


@pytest.mark.asyncio
async def test_create_optical_media(mock_hmc):
    """create_optical_media GETs the VG, appends VirtualOpticalMedia, then POSTs."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_VG_FEED_WITH_VMLIB, headers={"ETag": '"etag-1"'}
        )
    )
    post_route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(200, text=_VG_POST_RESPONSE)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.create_optical_media(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "aix.iso",
            3072,
        )
    body = post_route.calls.last.request.content.decode()
    assert "VirtualOpticalMedia" in body
    assert "aix.iso" in body
    # The medium's Size is GiB on the HMC: 3072 MiB is sent as 3.
    assert re.search(r"<Size\b[^>]*>3</", body)


@pytest.mark.asyncio
async def test_create_optical_media_refuses_a_size_that_is_not_whole_gib(mock_hmc):
    """Blank media takes the same whole-GiB conversion, checked before any request."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    get_route = mock_hmc.get(vg_path)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            ValueError, match="size_mib must be a positive multiple of 1024"
        ):
            await hmc.create_optical_media(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "aix.iso",
                1400,
            )

    assert not get_route.called


@pytest.mark.asyncio
async def test_delete_media_repository(mock_hmc):
    """delete_media_repository GETs the VG, removes MediaRepositories, then POSTs."""
    vg_path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222222222"
    mock_hmc.get(vg_path).mock(
        return_value=httpx.Response(
            200, text=_VG_FEED_WITH_VMLIB, headers={"ETag": '"etag-1"'}
        )
    )
    post_route = mock_hmc.post(vg_path).mock(
        return_value=httpx.Response(200, text=_VG_POST_RESPONSE)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.delete_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
    # VMLibrary must be absent from the POST body.
    body = post_route.calls.last.request.content.decode()
    assert "MediaRepositories" not in body
    assert post_route.called
