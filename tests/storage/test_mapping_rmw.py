"""Mapping writes read-modify-write the VIOS ViosSCSIMapping group (issue #962, ADR 0169).

The existing mapping is the observed V10R3 fixture; a create must post it back
unchanged beside the new mapping, under the GET's ETag. A detach (issue #1037)
shares the same grouped GET / If-Match POST sequence through the generalized
helper, so its ETag/412 transport contract is pinned here alongside create's.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from conftest import live_fixture, live_response, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError

UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
VIOS_UUID = "00000000-0000-4000-8000-000000000003"
LPAR_UUID = "00000000-0000-4000-8000-0000000000BB"
SYSTEM_UUID = "00000000-0000-4000-8000-000000000004"
PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"
OBSERVED = (
    Path(__file__).with_name("vscsi_mapping_v10r3.xml").read_text(encoding="utf-8")
)
OBSERVED_MAPPINGS = OBSERVED[OBSERVED.index("<VirtualSCSIMappings") :]


def vios_entry(
    mappings: str = OBSERVED_MAPPINGS,
    uuid: str = VIOS_UUID,
    system_uuid: str | None = SYSTEM_UUID,
) -> str:
    """The observed V10R3 VIOS shape (#979): AssociatedManagedSystem beside PartitionUUID.

    ``system_uuid=None`` omits that link, for the create-fails-closed case (ADR 0179).
    """
    system_link = (
        f'<AssociatedManagedSystem kb="CUD" kxe="false" rel="related" '
        f'href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/{system_uuid}"/>'
        if system_uuid is not None
        else ""
    )
    return f"""<entry xmlns="http://www.w3.org/2005/Atom"><content>
      <VirtualIOServer xmlns="{UOM_NS}" schemaVersion="V1_0">
        <Metadata><Atom><AtomID>{uuid}</AtomID></Atom></Metadata>
        <PartitionUUID kb="ROO">{uuid}</PartitionUUID>
        {system_link}
        {mappings}
      </VirtualIOServer></content></entry>"""


def _mappings(xml: str) -> list[ET.Element]:
    return ET.fromstring(xml).findall(f".//{{{UOM_NS}}}VirtualSCSIMapping")


def _canonical(element: ET.Element) -> str:
    return ET.canonicalize(ET.tostring(element, encoding="unicode"), strip_text=True)


def _children(element: ET.Element) -> list[str]:
    return [child.tag.split("}")[1] for child in element]


def _routes(mock_hmc, get_response, post_status=200):
    get = mock_hmc.get(PATH).mock(return_value=get_response)
    post = mock_hmc.post(PATH).mock(return_value=httpx.Response(post_status, text=""))
    return get, post


def _ok(body: str = vios_entry()) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"ETag": '"etag-1"'})


async def _map(hmc: HMCClient) -> None:
    await hmc.map_storage_to_lpar(
        VIOS_UUID, "VirtualDisk", "vd-R2", LPAR_UUID, "vtscsi9"
    )


async def _mount(hmc: HMCClient) -> None:
    await hmc.create_optical_mapping(VIOS_UUID, "install.iso", LPAR_UUID, "vtopt9")


# The observed fixture's one mapping (vhost0/vtscsi0) belongs to this LPAR.
EXISTING_MAPPING_ID = "vhost0/vtscsi0"
EXISTING_MAPPING_LPAR = "00000000-0000-4000-8000-0000000000AA"


async def _detach(hmc: HMCClient) -> None:
    await hmc.delete_storage_mapping(
        VIOS_UUID, EXISTING_MAPPING_ID, EXISTING_MAPPING_LPAR
    )


_parametrize_create = pytest.mark.parametrize(
    "create", [_map, _mount], ids=["map_storage_to_lpar", "create_optical_mapping"]
)


@pytest.mark.asyncio
async def test_detach_posts_grouped_url_under_if_match(mock_hmc):
    get, post = _routes(mock_hmc, _ok())

    async with HMCClient(make_config()) as hmc:
        await _detach(hmc)

    assert get.call_count == 1 and post.call_count == 1
    request = post.calls.last.request
    assert request.headers["If-Match"] == '"etag-1"'
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"].endswith("; type=VirtualIOServer")
    assert _mappings(request.content.decode()) == []


@pytest.mark.asyncio
async def test_detach_refuses_to_post_without_etag(mock_hmc):
    _, post = _routes(mock_hmc, httpx.Response(200, text=vios_entry()))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="no ETag"):
            await _detach(hmc)

    assert not post.called


@pytest.mark.asyncio
async def test_detach_reports_a_concurrent_change_on_412(mock_hmc):
    _, post = _routes(mock_hmc, _ok(), post_status=412)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="changed since they were read") as raised:
            await _detach(hmc)

    assert raised.value.status_code == 412
    assert post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("create", "storage"), [(_map, "VirtualDisk"), (_mount, "VirtualOpticalMedia")]
)
async def test_create_posts_existing_mappings_unchanged_under_if_match(
    mock_hmc, create, storage
):
    get, post = _routes(mock_hmc, _ok())

    async with HMCClient(make_config()) as hmc:
        await create(hmc)

    assert get.call_count == 1 and post.call_count == 1
    request = post.calls.last.request
    assert request.headers["If-Match"] == '"etag-1"'
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"].endswith("; type=VirtualIOServer")
    existing, new = _mappings(request.content.decode())
    assert _canonical(existing) == _canonical(_mappings(vios_entry())[0])
    assert _children(new) == [
        "Metadata",
        "AssociatedLogicalPartition",
        "Storage",
        "TargetDevice",
    ]
    assert new.find(f"{{{UOM_NS}}}Storage/{{{UOM_NS}}}{storage}") is not None
    link = new.find(f"{{{UOM_NS}}}AssociatedLogicalPartition").get("href")
    assert link == (
        f"https://hmc.test/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"
        f"/LogicalPartition/{LPAR_UUID}"
    )


@pytest.mark.asyncio
@_parametrize_create
async def test_create_fails_closed_without_associated_managed_system(mock_hmc, create):
    """No AssociatedManagedSystem on the fetched VIOS: no href to build, no POST (ADR 0179)."""
    _, post = _routes(mock_hmc, _ok(body=vios_entry(system_uuid=None)))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="AssociatedManagedSystem"):
            await create(hmc)

    assert not post.called


@pytest.mark.asyncio
@_parametrize_create
async def test_create_fails_closed_with_malformed_associated_managed_system(
    mock_hmc, create
):
    """A present but non-UUID AssociatedManagedSystem segment fails closed too."""
    _, post = _routes(mock_hmc, _ok(body=vios_entry(system_uuid="not-a-uuid")))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="AssociatedManagedSystem"):
            await create(hmc)

    assert not post.called


@pytest.mark.asyncio
async def test_detach_unaffected_without_associated_managed_system(mock_hmc):
    """A detach never builds a client-LPAR href, so a missing link doesn't block it."""
    get, post = _routes(mock_hmc, _ok(body=vios_entry(system_uuid=None)))

    async with HMCClient(make_config()) as hmc:
        await _detach(hmc)

    assert get.call_count == 1 and post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text=vios_entry()), "no ETag"),
        (
            httpx.Response(200, text=vios_entry(""), headers={"ETag": "e"}),
            "no VirtualSCSIMappings",
        ),
        (
            httpx.Response(200, text=vios_entry(uuid=LPAR_UUID), headers={"ETag": "e"}),
            "does not match",
        ),
    ],
)
async def test_create_refuses_to_post_without_etag_collection_or_identity(
    mock_hmc, response, message
):
    _, post = _routes(mock_hmc, response)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=message):
            await _map(hmc)

    assert not post.called


@pytest.mark.asyncio
async def test_create_rejects_a_response_with_xml_entities(mock_hmc):
    """An entity-bearing GET body raises HMCError, not a raw DefusedXmlException."""
    document = '<!DOCTYPE x [<!ENTITY payload "expanded">]><x>&payload;</x>'
    _, post = _routes(
        mock_hmc, httpx.Response(200, text=document, headers={"ETag": '"etag-1"'})
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="not valid XML"):
            await _map(hmc)

    assert not post.called


@pytest.mark.asyncio
async def test_create_reports_a_concurrent_change_on_412(mock_hmc):
    _, post = _routes(mock_hmc, _ok(), post_status=412)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="changed since they were read") as raised:
            await _mount(hmc)

    assert raised.value.status_code == 412
    assert post.call_count == 1


LIVE_MAPPING = live_fixture("rest-vios-scsi-mapping")
LIVE_VIOS = "00000005-ABCD-4EF0-8ABC-000000000005"
LIVE_LPAR = "00000004-ABCD-4EF0-8ABC-000000000004"


@pytest.mark.asyncio
async def test_detach_matches_upper_case_live_identities_case_insensitively(mock_hmc):
    """V10R3 prints VIOS and LPAR UUIDs upper case; a lower-case selector names them too.

    The captured grouped read carries AtomID, PartitionUUID and the mapping's
    AssociatedLogicalPartition href in upper case (#1202); the HMC answers the
    lower-case path with the same VIOS.
    """
    path = f"/rest/api/uom/VirtualIOServer/{LIVE_VIOS.lower()}?group=ViosSCSIMapping"
    mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200, text=LIVE_MAPPING["body"], headers={"ETag": '"etag-1"'}
        )
    )
    post = mock_hmc.post(path).mock(return_value=httpx.Response(200, text=""))

    async with HMCClient(make_config()) as hmc:
        await hmc.delete_storage_mapping(
            LIVE_VIOS.lower(), "dev-369/dev-256", LIVE_LPAR.lower()
        )

    assert post.call_count == 1
    assert _mappings(post.calls.last.request.content.decode()) == []


ADAPTER_LISTING = (
    "lshwres -r virtualio --rsubtype scsi -m <managed-system> --level lpar "
    "--filter lpar_names=<vios-name> -F slot_num,remote_lpar_name,remote_slot_num"
)


@pytest.mark.asyncio
@_parametrize_create
async def test_create_5xx_names_the_server_adapter_listing(mock_hmc, create):
    """A failed mapping create can leave an unmapped server adapter (#1237).

    In the #1085 window a REST0269 refusal left VIOS slot 5 paired to the
    client's slot 3 with no mapping; list-mappings did not show it, and only the
    lshwres listing did. The side-effect error names that read-only listing.
    """
    _routes(mock_hmc, _ok(), post_status=500)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="possible side effect") as raised:
            await create(hmc)

    message = str(raised.value)
    assert "server adapter" in message
    assert ADAPTER_LISTING in message
    assert "unmount-optical-media" not in message


@pytest.mark.asyncio
async def test_detach_5xx_does_not_claim_an_adapter_was_created(mock_hmc):
    _routes(mock_hmc, _ok(), post_status=500)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="possible side effect") as raised:
            await _detach(hmc)

    assert "lshwres" not in str(raised.value)


_MEDIA_FEED = live_fixture("rest-ms-vios-feed-media")["body"]
# The captured VIOS's mapping collection: dev-277 and dev-278 are
# VirtualOpticalTargetDevices, dev-265 a PhysicalVolumeVirtualTargetDevice.
LIVE_MEDIA_MAPPINGS = _MEDIA_FEED[
    _MEDIA_FEED.index("<VirtualSCSIMappings") : _MEDIA_FEED.index(
        "</VirtualSCSIMappings>"
    )
    + len("</VirtualSCSIMappings>")
]


async def _mount_onto(hmc: HMCClient, target_device: str | None) -> None:
    await hmc.create_optical_mapping(VIOS_UUID, "install.iso", LPAR_UUID, target_device)


@pytest.mark.asyncio
async def test_mount_refuses_an_optical_target_device_already_mapped(mock_hmc):
    """A VTD name in use fails on the HMC with a 500 (#1283); refuse it before POST."""
    _, post = _routes(mock_hmc, _ok(vios_entry(LIVE_MEDIA_MAPPINGS)))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="'dev-277' is already mapped") as raised:
            await _mount_onto(hmc, "dev-277")

    assert raised.value.status_code == 409
    message = str(raised.value)
    assert (
        "to LPAR 00000099-ABCD-4EF0-8ABC-000000000099 with media 'media-2'" in message
    )
    assert "unmount-optical-media" in message
    assert not post.called


@pytest.mark.asyncio
@pytest.mark.parametrize("target_device", ["vtopt9", None, "dev-265"])
async def test_mount_posts_when_target_device_is_free(mock_hmc, target_device):
    """Only an optical TargetName blocks the mount; dev-265 is a physical-volume VTD."""
    _, post = _routes(mock_hmc, _ok(vios_entry(LIVE_MEDIA_MAPPINGS)))

    async with HMCClient(make_config()) as hmc:
        await _mount_onto(hmc, target_device)

    assert post.call_count == 1


@pytest.mark.asyncio
async def test_mount_refused_on_an_unapplied_partition_reports_the_adapter(mock_hmc):
    """The captured REST0269 refusal of an optical create (#1284, 2026-11-02 window).

    That refusal left a VIOS server adapter with no mapping, shown by the lshwres
    listing alone, so the error names that listing and no VTD collision.
    """
    _, refused = live_response("rest-vios-optical-mapping-create-refused")
    mock_hmc.get(PATH).mock(return_value=_ok())
    mock_hmc.post(PATH).mock(return_value=refused)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="possible side effect") as raised:
            await _mount(hmc)

    assert raised.value.status_code == 500
    message = str(raised.value)
    assert "REST0269" in message
    assert ADAPTER_LISTING in message
    assert "is already mapped" not in message


@pytest.mark.asyncio
async def test_mount_5xx_name_in_use_names_the_collision(mock_hmc):
    """The HMC's own VTD-name refusal keeps the side-effect report and adds the remedy."""
    get = mock_hmc.get(PATH).mock(return_value=_ok())
    mock_hmc.post(PATH).mock(
        return_value=httpx.Response(
            500,
            text="<HttpErrorResponse><Message>VTD vtopt9 name is already used in "
            "another mapping</Message></HttpErrorResponse>",
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="possible side effect") as raised:
            await _mount(hmc)

    assert raised.value.status_code == 500
    assert get.call_count == 2
    message = str(raised.value)
    assert "readback completed" in message
    assert "'vtopt9' is already mapped" in message
    assert message.index("unmount-optical-media") < message.index(ADAPTER_LISTING)
