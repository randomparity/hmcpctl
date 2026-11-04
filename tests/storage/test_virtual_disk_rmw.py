"""Virtual-disk create and delete by VolumeGroup read-modify-write (#936).

V10R3 rejects a sparse VolumeGroup document at schema validation. Both writes GET
the whole group, add or remove one VirtualDisk, and POST the whole element back
with If-Match set to the GET's ETag (not a server-enforced concurrency guard).
The fixture follows the live V10R3 shape with synthetic values.
"""

import xml.etree.ElementTree as ET

import httpx
import pytest
from conftest import make_config
from defusedxml import ElementTree as DET

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.xmlutil import localname

VIOS = "11111111-1111-1111-1111-111111111111"
VG = "22222222-2222-2222-2222-222222222222"
VG_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS}/VolumeGroup/{VG}"
ETAG = '"etag-1"'


def _disk(name: str, gib: int) -> str:
    return f"""
          <VirtualDisk schemaVersion="V1_0">
            <Metadata><Atom/></Metadata>
            <DiskCapacity kb="CUR" kxe="false">{gib}</DiskCapacity>
            <DiskLabel kb="CUR" kxe="false">None</DiskLabel>
            <DiskName kb="CUR" kxe="false">{name}</DiskName>
            <VolumeGroup kb="ROR" kxe="false" href="https://hmc.example.invalid{VG_PATH}" rel="related"/>
            <UniqueDeviceID kb="ROR" kxe="false">UDID-{name}</UniqueDeviceID>
          </VirtualDisk>"""


VIRTUAL_DISKS = f"""
        <VirtualDisks kb="CUD" kxe="false" schemaVersion="V1_0">
          <Metadata><Atom/></Metadata>{_disk("vd-A", 128)}{_disk("vd-B", 64)}
        </VirtualDisks>"""


def _feed(virtual_disks: str = VIRTUAL_DISKS) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{VG}</id>
    <content type="application/vnd.ibm.powervm.uom+xml; type=VolumeGroup">
      <VolumeGroup:VolumeGroup xmlns:VolumeGroup="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/" xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/" schemaVersion="V1_0">
        <Metadata><Atom/></Metadata>
        <AvailableSize kb="ROR" kxe="false">300</AvailableSize>
        <FreeSpace kb="ROR" kxe="false">300</FreeSpace>
        <GroupCapacity kb="CUR" kxe="false">492</GroupCapacity>
        <GroupName kb="CUR" kxe="false">datavg</GroupName>
        <PhysicalVolumes kb="CUD" kxe="false" schemaVersion="V1_0">
          <Metadata><Atom/></Metadata>
          <PhysicalVolume schemaVersion="V1_0">
            <Metadata><Atom/></Metadata>
            <UniqueDeviceID kb="ROR" kxe="false">UDID-hdisk-A</UniqueDeviceID>
            <VolumeName kb="CUR" kxe="false">hdisk-A</VolumeName>
          </PhysicalVolume>
        </PhysicalVolumes>{virtual_disks}
      </VolumeGroup:VolumeGroup>
    </content>
  </entry>
</feed>"""


def _routes(
    mock_hmc, *, etag: str | None = ETAG, feed: str | None = None, post_status=200
):
    headers = {"ETag": etag} if etag else {}
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=feed or _feed(), headers=headers)
    )
    return mock_hmc.post(VG_PATH).mock(
        return_value=httpx.Response(post_status, text=feed or _feed())
    )


def _named(root: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in root.iter() if localname(el.tag) == name]


def _canonical(elements: list[ET.Element]) -> list[str]:
    return [ET.canonicalize(ET.tostring(el), strip_text=True) for el in elements]


def _posted(route) -> ET.Element:
    return DET.fromstring(route.calls.last.request.content)


def _fetched(feed: str | None = None) -> ET.Element:
    return DET.fromstring((feed or _feed()).encode())


async def _create(name: str = "vd-new", capacity_mib: int = 51200):
    async with HMCClient(make_config()) as hmc:
        return await hmc.create_virtual_disk(VIOS, VG, name, capacity_mib)


async def _delete(name: str):
    async with HMCClient(make_config()) as hmc:
        return await hmc.delete_virtual_disk(VIOS, VG, name)


@pytest.mark.asyncio
async def test_create_posts_whole_group_with_if_match(mock_hmc):
    route = _routes(mock_hmc)

    await _create()

    request = route.calls.last.request
    assert request.headers["If-Match"] == ETAG
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"].endswith("type=VolumeGroup")
    posted, fetched = _posted(route), _fetched()
    assert localname(posted.tag) == "VolumeGroup"
    assert _canonical(_named(posted, "PhysicalVolume")) == _canonical(
        _named(fetched, "PhysicalVolume")
    )
    disks = _named(posted, "VirtualDisk")
    assert _canonical(disks[1:]) == _canonical(_named(fetched, "VirtualDisk"))
    new = disks[0]
    assert new.attrib == {"schemaVersion": "V1_0"}
    assert [localname(c.tag) for c in new] == ["Metadata", "DiskCapacity", "DiskName"]
    assert new[1].text == "50" and new[2].text == "vd-new"
    assert new[1].attrib == new[2].attrib == {"kb": "CUR", "kxe": "false"}
    collection = _named(posted, "VirtualDisks")[0]
    assert [localname(c.tag) for c in collection][:2] == ["Metadata", "VirtualDisk"]


@pytest.mark.asyncio
async def test_create_adds_missing_virtual_disks_collection(mock_hmc):
    route = _routes(mock_hmc, feed=_feed(virtual_disks=""))

    await _create()

    posted = _posted(route)
    assert _canonical(_named(posted, "PhysicalVolume")) == _canonical(
        _named(_fetched(_feed(virtual_disks="")), "PhysicalVolume")
    )
    assert localname(posted[-1].tag) == "VirtualDisks"
    assert posted[-1].attrib == {"kb": "CUD", "kxe": "false", "schemaVersion": "V1_0"}
    assert [localname(c.tag) for c in posted[-1]] == ["Metadata", "VirtualDisk"]


@pytest.mark.asyncio
async def test_delete_posts_group_without_the_disk(mock_hmc):
    route = _routes(mock_hmc)

    await _delete("vd-A")

    assert route.calls.last.request.headers["If-Match"] == ETAG
    posted, fetched = _posted(route), _fetched()
    assert _canonical(_named(posted, "VirtualDisk")) == _canonical(
        _named(fetched, "VirtualDisk")[1:]
    )
    assert _canonical(_named(posted, "PhysicalVolume")) == _canonical(
        _named(fetched, "PhysicalVolume")
    )


@pytest.mark.asyncio
async def test_create_refuses_without_etag(mock_hmc):
    route = _routes(mock_hmc, etag=None)

    with pytest.raises(HMCError, match="no ETag"):
        await _create()

    assert not route.called


@pytest.mark.asyncio
async def test_delete_refuses_without_etag(mock_hmc):
    route = _routes(mock_hmc, etag=None)

    with pytest.raises(HMCError, match="no ETag"):
        await _delete("vd-A")

    assert not route.called


@pytest.mark.asyncio
async def test_create_rejects_a_response_with_xml_entities(mock_hmc):
    """An entity-bearing VolumeGroup GET body raises HMCError, not a raw DefusedXmlException."""
    document = '<!DOCTYPE x [<!ENTITY payload "expanded">]><x>&payload;</x>'
    _routes(mock_hmc, feed=document)

    with pytest.raises(HMCError, match="not valid XML"):
        await _create()


@pytest.mark.asyncio
async def test_create_refuses_duplicate_name(mock_hmc):
    route = _routes(mock_hmc)

    with pytest.raises(HMCError, match="already exists") as exc_info:
        await _create("vd-B")

    assert exc_info.value.status_code == 409
    assert not route.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("virtual_disks", "status"),
    [
        (VIRTUAL_DISKS, 404),
        (VIRTUAL_DISKS.replace("vd-A", "vd-gone").replace("vd-B", "vd-gone"), 409),
        ("", 404),
    ],
    ids=["absent", "ambiguous", "no-collection"],
)
async def test_delete_refuses_unless_exactly_one_match(mock_hmc, virtual_disks, status):
    route = _routes(mock_hmc, feed=_feed(virtual_disks))

    with pytest.raises(HMCError, match="expected exactly one") as exc_info:
        await _delete("vd-gone" if status == 409 else "vd-missing")

    assert exc_info.value.status_code == status
    assert not route.called


@pytest.mark.asyncio
async def test_create_reports_stale_etag(mock_hmc):
    route = _routes(mock_hmc, post_status=412)

    with pytest.raises(HMCError, match="changed since it was read") as exc_info:
        await _create()

    assert exc_info.value.status_code == 412
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_create_on_a_bare_group_document_posts_the_whole_group(mock_hmc):
    """A bare VolumeGroup root is the group, not the nested VolumeGroup link in a disk."""
    fetched = _fetched().find(".//{http://www.w3.org/2005/Atom}content")[0]
    bare = ET.tostring(fetched, encoding="unicode")
    route = _routes(mock_hmc, feed=bare)

    await _create()

    posted = _posted(route)
    assert localname(posted.tag) == "VolumeGroup" and "href" not in posted.attrib
    assert _canonical(_named(posted, "PhysicalVolume")) == _canonical(
        _named(fetched, "PhysicalVolume")
    )
    assert len(_named(posted, "VirtualDisk")) == 3


@pytest.mark.asyncio
async def test_create_refuses_an_overlong_name_before_any_request(mock_hmc):
    _routes(mock_hmc)

    with pytest.raises(ValueError, match="15 characters"):
        await _create("lv_sixteen_chars")

    assert not any(call.request.url.path == VG_PATH for call in mock_hmc.calls)
