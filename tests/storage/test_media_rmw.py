"""Media-repository VolumeGroup writes are conditioned on the GET's ETag (#996, ADR 0171).

Each of the four media writes GETs the whole VolumeGroup and POSTs it back. Like the
virtual-disk writes, the POST carries If-Match set to the GET's ETag, a GET without one
refuses before any POST, and a 412 is the concurrent-change error with nothing written.
"""

import re
import xml.etree.ElementTree as ET

import httpx
import pytest
from conftest import make_config, volume_group_with_repository

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError

VIOS = "11111111-1111-1111-1111-111111111111"
VG = "22222222-2222-2222-2222-222222222222"
VG_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS}/VolumeGroup/{VG}"
ETAG = '"etag-1"'
UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"

_REPOSITORY = """
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia>
              <VirtualOpticalMedia>
                <MediaName>old.iso</MediaName>
              </VirtualOpticalMedia>
            </OpticalMedia>
            <RepositoryName>VMLibrary</RepositoryName>
            <RepositorySize>7</RepositorySize>
          </VirtualMediaRepository>
        </MediaRepositories>"""

# A repository present but holding no media — delete_media_repository's own
# refusal (#1012) only fires on media, so its generic ETag-mechanics tests use
# this rather than _REPOSITORY, which carries old.iso for the other operations.
_EMPTY_REPOSITORY = """
        <MediaRepositories>
          <VirtualMediaRepository>
            <OpticalMedia/>
            <RepositoryName>VMLibrary</RepositoryName>
            <RepositorySize>7</RepositorySize>
          </VirtualMediaRepository>
        </MediaRepositories>"""


def _feed(repository: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{VG}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>clientvg1</GroupName>{repository}
      </VolumeGroup>
    </content>
  </entry>
</feed>"""


# (operation, arguments, repository XML embedded in the fetched group, or None for a bare VG)
OPERATIONS = [
    ("create_media_repository", (2048,), None),
    ("create_optical_media", ("new.iso", 3072), _REPOSITORY),
    ("delete_media_repository", (), _EMPTY_REPOSITORY),
    ("delete_optical_media", ("old.iso",), _REPOSITORY),
]
IDS = [name for name, _, _ in OPERATIONS]


def _routes(
    mock_hmc, repository: str | None, *, etag: str | None = ETAG, post_status=200
):
    feed = _feed(repository or "")
    headers = {"ETag": etag} if etag else {}
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=feed, headers=headers)
    )
    return mock_hmc.post(VG_PATH).mock(
        return_value=httpx.Response(post_status, text=feed)
    )


async def _run(operation: str, arguments: tuple):
    async with HMCClient(make_config()) as hmc:
        return await getattr(hmc, operation)(VIOS, VG, *arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "arguments", "repository"), OPERATIONS, ids=IDS)
async def test_media_write_sends_if_match(mock_hmc, operation, arguments, repository):
    route = _routes(mock_hmc, repository)

    await _run(operation, arguments)

    assert route.call_count == 1
    assert route.calls.last.request.headers["If-Match"] == ETAG


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "arguments", "repository"), OPERATIONS, ids=IDS)
async def test_media_write_refuses_without_etag(
    mock_hmc, operation, arguments, repository
):
    route = _routes(mock_hmc, repository, etag=None)

    with pytest.raises(HMCError, match="no ETag"):
        await _run(operation, arguments)

    assert not route.called


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "arguments", "repository"), OPERATIONS, ids=IDS)
async def test_media_write_reports_stale_etag(
    mock_hmc, operation, arguments, repository
):
    route = _routes(mock_hmc, repository, post_status=412)

    with pytest.raises(HMCError, match="changed since it was read") as exc_info:
        await _run(operation, arguments)

    assert exc_info.value.status_code == 412
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments", "repository"),
    [
        ("create_media_repository", (7168,), _REPOSITORY),
        ("delete_media_repository", (), None),
        ("delete_optical_media", ("absent.iso",), _REPOSITORY),
    ],
    ids=["repository-exists", "no-repository", "no-such-medium"],
)
async def test_media_noop_needs_no_etag(mock_hmc, operation, arguments, repository):
    """A write that has nothing to change posts nothing, so it needs no ETag."""
    route = _routes(mock_hmc, repository, etag=None)

    await _run(operation, arguments)

    assert not route.called


@pytest.mark.asyncio
async def test_delete_media_repository_refuses_medium_seen_at_its_own_read(mock_hmc):
    """A medium appearing between the operation's emptiness check and this
    client's own GET is still caught here, with nothing written (#1012).

    The operation-level check (resources.py) does its own GET; this asserts the
    client refuses independently when *its* GET observes a VirtualOpticalMedia
    still inside the MediaRepositories block it is about to remove.
    """
    route = _routes(mock_hmc, _REPOSITORY)

    with pytest.raises(HMCError, match=r"contains 1 image\(s\): 'old\.iso'"):
        await _run("delete_media_repository", ())

    assert not route.called


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [True, False], ids=["two-media", "no-media"])
async def test_create_optical_media_adds_to_the_captured_container(mock_hmc, media):
    """A new medium joins the one captured OpticalMedia container (#1202)."""
    body = volume_group_with_repository(media=media)
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=body, headers={"ETag": ETAG})
    )
    route = mock_hmc.post(VG_PATH).mock(return_value=httpx.Response(200, text=body))

    await _run("create_optical_media", ("new.iso", 3072))

    posted = ET.fromstring(route.calls.last.request.content)
    containers = posted.findall(f".//{{{UOM_NS}}}OpticalMedia")
    assert len(containers) == 1
    names = [
        item.findtext(f"{{{UOM_NS}}}MediaName")
        for item in containers[0].findall(f"{{{UOM_NS}}}VirtualOpticalMedia")
    ]
    assert names == (["media-1", "media-2"] if media else []) + ["new.iso"]


def _shape(element: ET.Element) -> list[tuple[str, dict[str, str]]]:
    return [(child.tag.split("}")[-1], dict(child.attrib)) for child in element]


def _captured_medium() -> ET.Element:
    group = ET.fromstring(volume_group_with_repository(media=True))
    medium = group.find(f".//{{{UOM_NS}}}VirtualOpticalMedia")
    assert medium is not None
    return medium


@pytest.mark.asyncio
async def test_create_optical_media_matches_the_captured_medium(mock_hmc):
    """The new medium has the captured children, order and attributes, minus the
    server-set MediaUDID (kb="ROR"); V10R3 refused the earlier order with REST0001 (#1385).
    """
    body = volume_group_with_repository(media=False)
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=body, headers={"ETag": ETAG})
    )
    route = mock_hmc.post(VG_PATH).mock(return_value=httpx.Response(200, text=body))

    await _run("create_optical_media", ("new.iso", 3072))

    posted = ET.fromstring(route.calls.last.request.content)
    created = posted.find(f".//{{{UOM_NS}}}VirtualOpticalMedia")
    assert created is not None
    captured = _captured_medium()
    expected = [
        (tag, attrib) for tag, attrib in _shape(captured) if attrib.get("kb") != "ROR"
    ]
    assert [tag for tag, _ in expected] == [
        "Metadata",
        "MediaName",
        "MountType",
        "Size",
    ]
    assert created.attrib == captured.attrib
    assert _shape(created) == expected
    assert [
        created.findtext(f"{{{UOM_NS}}}{tag}")
        for tag in ("MediaName", "MountType", "Size")
    ] == [
        "new.iso",
        "rw",
        "3",
    ]


@pytest.mark.asyncio
async def test_create_optical_media_builds_the_captured_container(mock_hmc):
    """A repository with no OpticalMedia gains one shaped like the captured container,
    ahead of RepositoryName (#1385)."""
    captured_group = ET.fromstring(volume_group_with_repository(media=False))
    captured = captured_group.find(f".//{{{UOM_NS}}}OpticalMedia")
    assert captured is not None
    body = re.sub(
        r"\s*<OpticalMedia\b.*?</OpticalMedia>",
        "",
        volume_group_with_repository(media=False),
        count=1,
        flags=re.DOTALL,
    )
    mock_hmc.get(VG_PATH).mock(
        return_value=httpx.Response(200, text=body, headers={"ETag": ETAG})
    )
    route = mock_hmc.post(VG_PATH).mock(return_value=httpx.Response(200, text=body))

    await _run("create_optical_media", ("new.iso", 3072))

    posted = ET.fromstring(route.calls.last.request.content)
    repository = posted.find(f".//{{{UOM_NS}}}VirtualMediaRepository")
    assert repository is not None
    tags = [tag for tag, _ in _shape(repository)]
    assert tags.index("OpticalMedia") == tags.index("RepositoryName") - 1
    container = repository.find(f"{{{UOM_NS}}}OpticalMedia")
    assert container is not None
    assert container.attrib == captured.attrib
    assert [tag for tag, _ in _shape(container)] == ["Metadata", "VirtualOpticalMedia"]


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [UOM_NS, "", "urn:foreign"])
@pytest.mark.parametrize("has_anchor", [True, False], ids=["first-anchor", "append"])
async def test_create_optical_media_insertion_preserves_namespace_anchors(
    mock_hmc, namespace, has_anchor
):
    anchor = f'<RepositoryName xmlns="{namespace}">first</RepositoryName>'
    children = (
        "<Metadata/>"
        + (anchor + anchor if has_anchor else "")
        + "<RepositorySize>7</RepositorySize>"
    )
    route = _routes(
        mock_hmc,
        f"<MediaRepositories><VirtualMediaRepository>{children}</VirtualMediaRepository></MediaRepositories>",
    )

    await _run("create_optical_media", ("new.iso", 3072))

    posted = ET.fromstring(route.calls.last.request.content)
    repository = posted.find(f".//{{{UOM_NS}}}VirtualMediaRepository")
    assert repository is not None
    expected = [f"{{{UOM_NS}}}Metadata"]
    if has_anchor:
        expected += [f"{{{UOM_NS}}}OpticalMedia"] + [
            f"{{{namespace or UOM_NS}}}RepositoryName"
        ] * 2
    expected += [f"{{{UOM_NS}}}RepositorySize"]
    if not has_anchor:
        expected += [f"{{{UOM_NS}}}OpticalMedia"]
    assert [child.tag for child in repository] == expected
