"""Tests for media repository and optical media operations layer."""

from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import (
    live_fixture,
    make_config,
    mock_change_location,
    volume_group_with_repository,
)

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.storage.resources import (
    get_media_repository,
    list_optical_media,
    unmount_optical_media,
)


@pytest.fixture(autouse=True)
def _authorize_lpar_mutations(monkeypatch):
    async def authorize(hmc, system, lpar, **_kwargs):
        from hmcpctl.resource_identity import resolve_lpar_uuid

        return await resolve_lpar_uuid(hmc, lpar, system_name_or_uuid=system)

    monkeypatch.setattr(
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        authorize,
    )


VIOS_UUID = "00000000-0000-0000-0000-000000000003"
VG_UUID = "22222222-2222-2222-2222-222222220001"

VG_ENTRY_WITH_REPO = volume_group_with_repository(media=False)

# The captured clientvg1 entry: a volume group with no media repository.
VG_ENTRY_EMPTY = live_fixture("rest-volume-group")["body"]


@pytest.mark.asyncio
async def test_get_media_repository_operation(mock_hmc):
    """get_media_repository calls client method and returns repository info."""
    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_WITH_REPO))

    async with HMCClient(make_config()) as hmc:
        result = await get_media_repository(hmc, VIOS_UUID, VG_UUID)

    assert route.called
    assert result is not None
    repo = result["Resource"]["MediaRepositories"]["VirtualMediaRepository"]
    assert repo["RepositoryName"] == "VMLibrary"
    assert repo["RepositorySize"] == "15"


@pytest.mark.asyncio
async def test_list_optical_media_operation(mock_hmc):
    """list_optical_media calls client method and returns optical media list."""
    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(200, text=volume_group_with_repository()))

    async with HMCClient(make_config()) as hmc:
        media_list = await list_optical_media(hmc, VIOS_UUID, VG_UUID)

    assert route.called
    assert [media.name for media in media_list] == ["media-1", "media-2"]
    assert media_list[0].size_mib == 928.0512


@pytest.mark.asyncio
async def test_get_media_repository_none_propagates(mock_hmc):
    """get_media_repository propagates None when repository not found."""
    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(404, text=""))

    async with HMCClient(make_config()) as hmc:
        result = await get_media_repository(hmc, VIOS_UUID, VG_UUID)

    assert route.called
    assert result is None


@pytest.mark.asyncio
async def test_list_optical_media_empty_propagates(mock_hmc):
    """list_optical_media propagates empty list when no media or VG not found."""
    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_EMPTY))

    async with HMCClient(make_config()) as hmc:
        media_list = await list_optical_media(hmc, VIOS_UUID, VG_UUID)

    assert route.called
    assert media_list == []


SYSTEM_UUID = "00000000-0000-0000-0000-000000000004"
LPAR_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_LPAR_UUID = "22222222-2222-2222-2222-222222222222"


def _lpar_link(lpar_uuid: str) -> str:
    """The absolute, system-scoped client-LPAR link the HMC returns (#940)."""
    return (
        f"https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"
        f"/LogicalPartition/{lpar_uuid}"
    )


VIOS_DOC_WITH_OPTICAL_MAPPINGS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{VIOS_UUID}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <Metadata><Atom><AtomID>{VIOS_UUID}</AtomID></Atom></Metadata>
      <PartitionName>vios1</PartitionName>
      <PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>
      <AssociatedManagedSystem rel="related"
        href="https://hmc/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"/>
      <VirtualSCSIMappings>
        <VirtualSCSIMapping>
          <AssociatedLogicalPartition rel="related" href="{_lpar_link(LPAR_UUID)}"/>
          <ServerAdapter><AdapterName>vhost0</AdapterName></ServerAdapter>
          <Storage><VirtualDisk><DiskName>lv_boot</DiskName></VirtualDisk></Storage>
          <TargetDevice><LogicalVolumeVirtualTargetDevice>
            <TargetName>vtscsi0</TargetName>
          </LogicalVolumeVirtualTargetDevice></TargetDevice>
        </VirtualSCSIMapping>
        <VirtualSCSIMapping>
          <AssociatedLogicalPartition rel="related" href="{_lpar_link(LPAR_UUID)}"/>
          <ServerAdapter><AdapterName>vhost0</AdapterName></ServerAdapter>
          <Storage>
            <VirtualOpticalMedia><MediaName>rhel9.iso</MediaName></VirtualOpticalMedia>
          </Storage>
          <TargetDevice><VirtualOpticalTargetDevice>
            <TargetName>vtopt0</TargetName>
          </VirtualOpticalTargetDevice></TargetDevice>
        </VirtualSCSIMapping>
        <VirtualSCSIMapping>
          <AssociatedLogicalPartition rel="related" href="{_lpar_link(OTHER_LPAR_UUID)}"/>
          <ServerAdapter><AdapterName>vhost1</AdapterName></ServerAdapter>
          <Storage>
            <VirtualOpticalMedia><MediaName>rhel9.iso</MediaName></VirtualOpticalMedia>
          </Storage>
          <TargetDevice><VirtualOpticalTargetDevice>
            <TargetName>vtopt1</TargetName>
          </VirtualOpticalTargetDevice></TargetDevice>
        </VirtualSCSIMapping>
      </VirtualSCSIMappings>
    </VirtualIOServer>
  </content>
</entry>
"""

_VIOS_GET_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}"
_MAPPINGS_GET_PATH = f"{_VIOS_GET_PATH}?group=ViosSCSIMapping"
# The detach RMW (ADR 0169) posts back to the same grouped URL it read, under If-Match.
_MAPPINGS_POST_PATH = _MAPPINGS_GET_PATH


def _posted_document(post_route) -> str:
    assert post_route.called, "unmount must write the modified VIOS document back"
    return post_route.calls.last.request.content.decode()


def _mock_unmount_reads(mock_hmc, document=VIOS_DOC_WITH_OPTICAL_MAPPINGS):
    """Mock the grouped GET both the inventory lookup and the RMW detach read hit.

    ``unmount_optical_media`` looks up the mapping via ``list_optical_mappings``
    before deleting it, then ``delete_storage_mapping`` re-reads the same grouped
    group under If-Match (ADR 0169); both land on the identical URL.
    """
    mock_change_location(mock_hmc, LPAR_UUID)
    return mock_hmc.get(_MAPPINGS_GET_PATH).mock(
        return_value=httpx.Response(200, text=document, headers={"ETag": '"etag-1"'})
    )


@pytest.mark.asyncio
async def test_unmount_optical_media_deletes_only_the_exact_mapping_identity():
    """LPAR-scoped inventory resolves MediaName by equality, never subtree text."""
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = None
    hmc.list_optical_mappings.return_value = [
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": "vtopt1"}},
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "rhel9.iso.bak"},
            },
        },
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "other.iso"},
            },
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": "rhel9.iso"}},
        },
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": "vtopt3"}},
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "rhel9.iso"},
            },
        },
    ]

    await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    hmc.list_optical_mappings.assert_awaited_once_with(VIOS_UUID, LPAR_UUID)
    hmc.delete_storage_mapping.assert_awaited_once_with(
        VIOS_UUID, "vhost0/vtopt3", LPAR_UUID
    )


@pytest.mark.asyncio
async def test_unmount_optical_media_rejects_empty_media_before_inventory():
    hmc = AsyncMock()

    with pytest.raises(ValueError, match="must not be empty"):
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="")

    hmc.list_optical_mappings.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmount_optical_media_rejects_ambiguous_exact_identity():
    hmc = AsyncMock()
    hmc.list_optical_mappings.return_value = [
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": target}},
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "rhel9.iso"},
            },
        }
        for target in ("vtopt0", "vtopt1")
    ]

    with pytest.raises(HMCError, match="ambiguous"):
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmount_optical_media_fails_closed_when_exact_mapping_is_absent():
    hmc = AsyncMock()
    hmc.list_optical_mappings.return_value = [
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "rhel9.iso.bak"},
            },
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": "rhel9.iso"}},
        }
    ]

    with pytest.raises(HMCError, match="not found"):
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmount_optical_media_rejects_a_mapping_without_identity():
    hmc = AsyncMock()
    hmc.list_optical_mappings.return_value = [
        {
            "Storage": {
                "VirtualOpticalMedia": {"MediaName": "rhel9.iso"},
            },
        }
    ]

    with pytest.raises(HMCError, match="no adapter/target identity"):
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmount_optical_media_removes_the_named_mapping_for_that_lpar(mock_hmc):
    """Unmount drops the addressed mapping and rewrites nothing else.

    The HMC has no addressable VirtualSCSIMapping sub-resource, so the
    operation is a read-modify-write of the whole VirtualIOServer document.
    This asserts what that document says afterwards, not that a call happened.

    The fixture also carries the same media name on another LPAR, proving the
    LPAR-scoped inventory identity is part of the selection.
    """
    _mock_unmount_reads(mock_hmc)
    post = mock_hmc.post(_MAPPINGS_POST_PATH).mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        result = await unmount_optical_media(
            hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso"
        )

    assert result == ChangeLocation("Disabled", "current-configuration")
    body = _posted_document(post)
    assert "vtopt0" not in body
    assert "vtscsi0" in body
    assert "vtopt1" in body


@pytest.mark.asyncio
async def test_unmount_optical_media_preserves_the_backing_iso(mock_hmc):
    """Unmount removes the mapping only; no VirtualOpticalMedia is deleted.

    The surviving mapping keeps its VirtualOpticalMedia element, and the
    operation issues no DELETE against the media repository, so the ISO
    container stays available for a later remount.
    """
    _mock_unmount_reads(mock_hmc)
    post = mock_hmc.post(_MAPPINGS_POST_PATH).mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    body = _posted_document(post)
    assert "VirtualOpticalMedia" in body
    assert "rhel9.iso" in body
    deletes = [
        call.request
        for call in mock_hmc.calls
        if call.request.method == "DELETE"
        and "/rest/api/web/Logon" not in call.request.url.path
    ]
    assert deletes == []


@pytest.mark.asyncio
async def test_unmount_optical_media_fails_without_post_when_mapping_is_absent(
    mock_hmc,
):
    """An unmatched exact identity fails closed without rewriting the VIOS."""
    mock_hmc.get(_MAPPINGS_GET_PATH).mock(
        return_value=httpx.Response(200, text=VIOS_DOC_WITH_OPTICAL_MAPPINGS)
    )
    post = mock_hmc.post(_MAPPINGS_POST_PATH).mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="not found"):
            await unmount_optical_media(
                hmc, VIOS_UUID, LPAR_UUID, media_name="aix73.iso"
            )

    assert not post.called


@pytest.mark.asyncio
async def test_unmount_optical_media_preserves_a_sibling_with_a_prefix_name(
    mock_hmc,
):
    """A strict prefix sibling survives while the exact mapping is removed."""
    doc = VIOS_DOC_WITH_OPTICAL_MAPPINGS.replace(
        "<Storage><VirtualDisk><DiskName>lv_boot</DiskName></VirtualDisk></Storage>",
        "<Storage><VirtualOpticalMedia><MediaName>rhel9.iso.bak</MediaName>"
        "</VirtualOpticalMedia></Storage>",
    )
    _mock_unmount_reads(mock_hmc, doc)
    post = mock_hmc.post(_MAPPINGS_POST_PATH).mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        await unmount_optical_media(hmc, VIOS_UUID, LPAR_UUID, media_name="rhel9.iso")

    body = _posted_document(post)
    assert "rhel9.iso.bak" in body
    assert "vtopt0" not in body


@pytest.mark.asyncio
async def test_unmount_optical_media_resolves_vios_and_lpar_names(mock_hmc):
    """Names are resolved to UUIDs before the mapping is addressed."""
    vios_search = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>urn:uuid:{VIOS_UUID}</id>
      <content type="application/vnd.ibm.powervm.uom+xml">
        <VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
          <PartitionName>vios1</PartitionName>
        </VirtualIOServer>
      </content>
    </entry></feed>"""
    lpar_search = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>urn:uuid:{LPAR_UUID}</id>
      <content type="application/vnd.ibm.powervm.uom+xml">
        <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
          <PartitionName>lpar1</PartitionName>
        </LogicalPartition>
      </content>
    </entry></feed>"""
    mock_hmc.get("/rest/api/uom/VirtualIOServer/search/(PartitionName==vios1)").mock(
        return_value=httpx.Response(200, text=vios_search)
    )
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==lpar1)").mock(
        return_value=httpx.Response(200, text=lpar_search)
    )
    _mock_unmount_reads(mock_hmc)
    post = mock_hmc.post(_MAPPINGS_POST_PATH).mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        await unmount_optical_media(hmc, "vios1", "lpar1", media_name="rhel9.iso")

    assert "vtopt0" not in _posted_document(post)


def test_detach_optical_mapping_alias_is_gone():
    """Issue #362: the duplicate name is removed outright, with no shim."""
    import hmcpctl.operations.storage.resources as ops

    assert not hasattr(ops, "detach_optical_mapping")


def test_optical_unmount_reuses_the_storage_mapping_remover():
    """The client has one parent-VIOS VirtualSCSIMapping remover."""
    assert not hasattr(HMCClient, "delete_optical_mapping")


@pytest.fixture(autouse=True)
def scoped_volume_group_inventory(monkeypatch):
    """Supply membership at the HMC boundary for these media/guard tests."""
    lookup = AsyncMock(
        return_value=[{"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}}]
    )
    monkeypatch.setattr(HMCClient, "list_volume_groups", lookup)
    return lookup
