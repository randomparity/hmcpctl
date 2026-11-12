"""Transport-layer tests for VirtualSCSI mapping inventory.

Regression coverage for issue #348 (mappings are read from the parsed Resource)
and issue #940: fixtures follow the observed V10R3 shape, where a mapping has no
UUID, is identified by server adapter and target device, and names its client
LPAR by an absolute, system-scoped href.
"""

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from conftest import live_fixture, make_config

from hmcpctl.client.client_parse import _parse_feed
from hmcpctl.client.client_storage import (
    lpar_uuid_from_href,
    mapping_lpar_uuid,
    storage_mapping_id,
)
from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError

VIOS_UUID = "00000000-0000-0000-0000-000000000003"

UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
HMC_SYSTEM_UUID = "00000000-0000-4000-8000-000000000001"
LPAR_A = "00000000-0000-4000-8000-0000000000AA"
LPAR_B = "00000000-0000-4000-8000-0000000000BB"
OBSERVED_MAPPINGS = (
    Path(__file__).with_name("vscsi_mapping_v10r3.xml").read_text(encoding="utf-8")
)


def lpar_href(lpar_uuid: str) -> str:
    """The absolute, system-scoped link the HMC returns for a client LPAR."""
    return (
        "https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/"
        f"{HMC_SYSTEM_UUID}/LogicalPartition/{lpar_uuid}"
    )


def vios_feed(mappings: str) -> str:
    return f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>
      <VirtualIOServer xmlns="{UOM_NS}">{mappings}</VirtualIOServer>
    </content></entry></feed>"""


def mapping_xml(
    lpar_uuid: str, adapter: str, storage: str, device: str, target: str
) -> str:
    """One VirtualSCSIMapping in the observed shape: no UUID, absolute LPAR link."""
    return f"""<VirtualSCSIMapping>
      <AssociatedLogicalPartition href="{lpar_href(lpar_uuid)}" rel="related"/>
      <ServerAdapter><AdapterName>{adapter}</AdapterName></ServerAdapter>
      <Storage>{storage}</Storage>
      <TargetDevice><{device}><TargetName>{target}</TargetName></{device}></TargetDevice>
    </VirtualSCSIMapping>"""


MAPPINGS_FEED = vios_feed(
    "<VirtualSCSIMappings>"
    + mapping_xml(
        LPAR_A,
        "vhost0",
        "<VirtualDisk><DiskName>lv_boot</DiskName></VirtualDisk>",
        "LogicalVolumeVirtualTargetDevice",
        "vtscsi0",
    )
    + mapping_xml(
        LPAR_B,
        "vhost1",
        "<PhysicalVolume><VolumeName>hdisk5</VolumeName></PhysicalVolume>",
        "PhysicalVolumeVirtualTargetDevice",
        "vtscsi1",
    )
    + "</VirtualSCSIMappings>"
)

EMPTY_MAPPINGS_FEED = vios_feed("<VirtualSCSIMappings/>")

# Two optical mappings split across two LPARs, to prove the lpar_uuid filter
# on list_optical_mappings bites.
OPTICAL_MAPPINGS_FEED = vios_feed(
    "<VirtualSCSIMappings>"
    + mapping_xml(
        LPAR_A,
        "vhost0",
        "<VirtualOpticalMedia><MediaName>aix72.iso</MediaName></VirtualOpticalMedia>",
        "VirtualOpticalTargetDevice",
        "vtopt0",
    )
    + mapping_xml(
        LPAR_B,
        "vhost1",
        "<VirtualOpticalMedia><MediaName>rhel8.iso</MediaName></VirtualOpticalMedia>",
        "VirtualOpticalTargetDevice",
        "vtopt1",
    )
    + "</VirtualSCSIMappings>"
)

VIOS_PATH = f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping"
DISK_ID = "vhost0/vtscsi0"
UNIDENTIFIABLE_MAPPING = f"""<VirtualSCSIMapping>
      <AssociatedLogicalPartition href="{lpar_href(LPAR_A)}" rel="related"/>
      <ServerAdapter><VirtualSlotNumber>9</VirtualSlotNumber></ServerAdapter>
    </VirtualSCSIMapping>"""
VIOS_PARENT = f"""<VirtualIOServer
  xmlns="{UOM_NS}">
  <Metadata><Atom><AtomID>{VIOS_UUID}</AtomID></Atom></Metadata>
  <PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>
  <UnrelatedLink href="/rest/api/uom/ManagedSystem/11111111-1111-1111-1111-111111111111"/>
  <VirtualSCSIMappings>
    {
    mapping_xml(
        LPAR_A,
        "vhost0",
        "<VirtualDisk><DiskName>lv_boot</DiskName></VirtualDisk>",
        "LogicalVolumeVirtualTargetDevice",
        "vtscsi0",
    )
}
    {
    mapping_xml(
        LPAR_A,
        "vhost0",
        "<VirtualDisk><DiskName>lv_data</DiskName></VirtualDisk>",
        "LogicalVolumeVirtualTargetDevice",
        "vtscsi10",
    )
}
    {UNIDENTIFIABLE_MAPPING}
  </VirtualSCSIMappings>
  <ResourceMonitoringControlState>active</ResourceMonitoringControlState>
</VirtualIOServer>"""


def _vios_parent_response(document: str = VIOS_PARENT, etag: str | None = '"etag-1"'):
    headers = {"ETag": etag} if etag else {}
    return httpx.Response(200, text=document, headers=headers)


@pytest.mark.asyncio
async def test_list_storage_mappings_reads_resource(mock_hmc):
    """Mappings live under entries[0]["Resource"], not on the entry itself."""
    mock_hmc.get(VIOS_PATH).mock(return_value=httpx.Response(200, text=MAPPINGS_FEED))

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_storage_mappings(VIOS_UUID)

    assert len(mappings) == 2
    names = sorted(
        next(iter(m["Storage"].values()))["VolumeName"]
        if "PhysicalVolume" in m["Storage"]
        else next(iter(m["Storage"].values()))["DiskName"]
        for m in mappings
    )
    assert names == ["hdisk5", "lv_boot"]


@pytest.mark.asyncio
async def test_list_storage_mappings_empty(mock_hmc):
    """list_storage_mappings returns empty list when no mappings exist."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=EMPTY_MAPPINGS_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_storage_mappings(VIOS_UUID)

    assert mappings == []


@pytest.mark.asyncio
async def test_list_storage_mappings_propagates_bad_request(mock_hmc):
    """A rejected documented group is an API error, not an empty inventory."""
    mock_hmc.get(VIOS_PATH).mock(return_value=httpx.Response(400, text="bad request"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.list_storage_mappings(VIOS_UUID)

    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_list_storage_mappings_filters_by_lpar(mock_hmc):
    """The lpar_uuid filter reads the LPAR from the HMC's absolute, system-scoped href."""
    mock_hmc.get(VIOS_PATH).mock(return_value=httpx.Response(200, text=MAPPINGS_FEED))

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_storage_mappings(VIOS_UUID, LPAR_B)

    assert [storage_mapping_id(m) for m in mappings] == ["vhost1/vtscsi1"]


@pytest.mark.asyncio
async def test_list_storage_mappings_filters_live_lpar_case_insensitively(mock_hmc):
    """A lower-case LPAR selector matches the upper-case UUID V10R3 links (#1202)."""
    capture = live_fixture("rest-vios-scsi-mapping")
    mock_hmc.get(capture["path"]).mock(
        return_value=httpx.Response(200, text=capture["body"])
    )

    async with HMCClient(make_config()) as hmc:
        mappings = await hmc.list_storage_mappings(
            "00000005-ABCD-4EF0-8ABC-000000000005",
            "00000004-abcd-4ef0-8abc-000000000004",
        )

    assert [storage_mapping_id(m) for m in mappings] == ["dev-369/dev-256"]


@pytest.mark.asyncio
async def test_an_unmapped_server_adapter_is_a_mapping_holding_only_its_adapter(
    mock_hmc,
):
    """A live V10R3 VIOS with an unmapped vSCSI server adapter (#1250).

    The HMC lists the adapter as a mapping with no client adapter, target, storage
    or partition link, so it has no identity and an LPAR-scoped list drops it.
    """
    capture = live_fixture("rest-vios-scsi-mapping-unmapped-adapter")
    mock_hmc.get(capture["path"]).mock(
        return_value=httpx.Response(200, text=capture["body"])
    )
    vios, test_lpar = (
        "00000001-ABCD-4EF0-8ABC-000000000001",
        "00000002-ABCD-4EF0-8ABC-000000000002",
    )

    async with HMCClient(make_config()) as hmc:
        everything = await hmc.list_storage_mappings(vios)
        scoped = await hmc.list_storage_mappings(vios, test_lpar)

    assert [storage_mapping_id(m) for m in everything] == [
        "dev-17/dev-11",
        None,
        "dev-17/dev-12",
    ]
    assert [storage_mapping_id(m) for m in scoped] == ["dev-17/dev-11", "dev-17/dev-12"]
    adapter_only = everything[1]
    assert mapping_lpar_uuid(adapter_only) is None
    assert {key for key in adapter_only if not key.startswith("@")} == {
        "Metadata",
        "ServerAdapter",
    }
    adapter = adapter_only["ServerAdapter"]
    assert (
        adapter["AdapterName"],
        adapter["VirtualSlotNumber"],
        adapter["RemoteLogicalPartitionID"],
        adapter["RemoteSlotNumber"],
    ) == ("dev-18", "5", "1", "4")


@pytest.mark.asyncio
async def test_list_optical_mappings_filters_by_exact_lpar_path(mock_hmc):
    """The optical filter reads the same absolute, system-scoped href."""
    mock_hmc.get(VIOS_PATH).mock(
        return_value=httpx.Response(200, text=OPTICAL_MAPPINGS_FEED)
    )

    config = make_config()
    async with HMCClient(config) as hmc:
        mappings = await hmc.list_optical_mappings(VIOS_UUID, LPAR_A)

    assert len(mappings) == 1
    assert mappings[0]["Storage"]["VirtualOpticalMedia"]["MediaName"] == "aix72.iso"


def _observed_mapping() -> dict:
    entries = _parse_feed(vios_feed(OBSERVED_MAPPINGS), "observed")
    return entries[0]["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]


def test_observed_mapping_has_no_uuid_but_an_adapter_target_identity():
    mapping = _observed_mapping()

    assert "UUID" not in mapping
    assert storage_mapping_id(mapping) == "vhost0/vtscsi0"
    assert lpar_uuid_from_href(mapping["AssociatedLogicalPartition"]["href"]) == LPAR_A


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("<TargetDevice ", '<TargetDevice ksv="V1_1_0" '),
        ("<AdapterName ", '<AdapterName ksv="V1_1_0" '),
    ],
    ids=["target-device-attribute", "adapter-name-attribute"],
)
def test_storage_mapping_id_tolerates_unignored_attributes(old, new):
    feed = vios_feed(OBSERVED_MAPPINGS.replace(old, new, 1))
    mapping = _parse_feed(feed, "observed")[0]["Resource"]["VirtualSCSIMappings"][
        "VirtualSCSIMapping"
    ]

    assert storage_mapping_id(mapping) == "vhost0/vtscsi0"


@pytest.mark.parametrize(
    "mapping",
    [
        {
            "TargetDevice": {
                "LogicalVolumeVirtualTargetDevice": {"TargetName": "vtscsi0"}
            }
        },
        {"ServerAdapter": {"AdapterName": "vhost0"}},
        {
            "ServerAdapter": {"AdapterName": ""},
            "TargetDevice": {"X": {"TargetName": "t"}},
        },
        {"ServerAdapter": {"AdapterName": "vhost0"}, "TargetDevice": {"X": {}}},
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"X": {"TargetName": "a"}, "Y": {"TargetName": "b"}},
        },
        {
            "ServerAdapter": {"AdapterName": "vhost/0"},
            "TargetDevice": {"X": {"TargetName": "t"}},
        },
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"X": {"TargetName": "a/b"}},
        },
        {"ServerAdapter": "vhost0", "TargetDevice": {"X": {"TargetName": "t"}}},
        {"ServerAdapter": {"AdapterName": "vhost0"}, "TargetDevice": "vtscsi0"},
    ],
    ids=[
        "no-adapter",
        "no-target",
        "empty-adapter-name",
        "no-target-name",
        "two-targets",
        "slash-in-adapter",
        "slash-in-target",
        "adapter-not-element",
        "target-not-element",
    ],
)
def test_storage_mapping_id_is_none_without_one_exact_identity(mapping):
    assert storage_mapping_id(mapping) is None


@pytest.mark.parametrize(
    "href",
    [
        lpar_href(LPAR_A),
        f"/rest/api/uom/LogicalPartition/{LPAR_A}",
        f"https://hmc.example.invalid/rest/api/uom/LogicalPartition/{LPAR_A}",
    ],
    ids=["absolute-system-scoped", "relative", "host-only"],
)
def test_lpar_uuid_from_href_reads_the_final_segment(href):
    assert lpar_uuid_from_href(href) == LPAR_A


@pytest.mark.parametrize(
    "href",
    [
        None,
        "",
        f"{lpar_href(LPAR_A)}/",
        f"{lpar_href(LPAR_A)}/VirtualSCSIClientAdapter",
        f"/rest/api/uom/ManagedSystem/{HMC_SYSTEM_UUID}",
        "/rest/api/uom/LogicalPartition/",
    ],
    ids=[
        "non-string",
        "empty",
        "trailing-slash",
        "child-resource",
        "no-marker",
        "empty-tail",
    ],
)
def test_lpar_uuid_from_href_is_none_when_malformed(href):
    assert lpar_uuid_from_href(href) is None


@pytest.mark.asyncio
async def test_delete_storage_mapping_posts_grouped_url_under_if_match(mock_hmc):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response())
    posted = mock_hmc.post(VIOS_PATH).mock(return_value=httpx.Response(200, text=""))

    async with HMCClient(make_config()) as hmc:
        await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)

    request = posted.calls[0].request
    assert request.headers["If-Match"] == '"etag-1"'
    assert request.headers["content-type"].endswith("type=VirtualIOServer")
    root = ET.fromstring(request.content)
    ns = {"uom": UOM_NS}
    remaining = root.findall(".//uom:VirtualSCSIMapping", ns)
    assert [
        node.findtext(".//uom:TargetName", namespaces=ns) for node in remaining
    ] == ["vtscsi10", None]
    assert (
        root.findtext("uom:ResourceMonitoringControlState", namespaces=ns) == "active"
    )


def test_delete_storage_mapping_serializes_default_uom_namespace_in_fresh_process():
    """The shared remover must not depend on another test's namespace registry."""
    script = textwrap.dedent(
        f"""
        import asyncio
        from types import SimpleNamespace

        from hmcpctl.client.client_storage import StorageMixin

        class FakeClient(StorageMixin):
            async def _request_with_uuid_path_arguments(
                self, method, *_args, uuid_path_arguments, **kwargs
            ):
                if method == "GET":
                    return SimpleNamespace(
                        status_code=200,
                        text={VIOS_PARENT!r},
                        headers={{"ETag": '"etag-1"'}},
                    )
                print(kwargs["content"])
                return SimpleNamespace(status_code=200, text="", headers={{}})

        asyncio.run(
            FakeClient().delete_storage_mapping({VIOS_UUID!r}, {DISK_ID!r}, {LPAR_A!r})
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert "<ns0:" not in result.stdout
    assert (
        '<VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/'
        'firmware/uom/mc/2012_10/">' in result.stdout
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mapping_id", "document", "lpar_uuid", "message"),
    [
        ("vhost9/vtscsi9", VIOS_PARENT, LPAR_A, "not found"),
        ("vhost0/vtscsi1", VIOS_PARENT, LPAR_A, "not found"),
        (DISK_ID, VIOS_PARENT.replace("vtscsi10", "vtscsi0"), LPAR_A, "duplicated"),
        (DISK_ID, VIOS_PARENT, LPAR_B, "does not belong"),
        (
            DISK_ID,
            VIOS_PARENT.replace(lpar_href(LPAR_A), lpar_href(LPAR_A) + "/", 1),
            LPAR_A,
            "does not belong",
        ),
    ],
    ids=["missing", "prefix", "duplicated", "other-lpar", "unparseable-lpar-link"],
)
async def test_delete_storage_mapping_fails_closed_without_post(
    mock_hmc, mapping_id, document, lpar_uuid, message
):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response(document))
    posted = mock_hmc.post(VIOS_PATH).mock(return_value=httpx.Response(200, text=""))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=message):
            await hmc.delete_storage_mapping(VIOS_UUID, mapping_id, lpar_uuid)
    assert not posted.called


@pytest.mark.asyncio
async def test_delete_storage_mapping_refuses_to_post_without_etag(mock_hmc):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response(etag=None))
    posted = mock_hmc.post(VIOS_PATH).mock(return_value=httpx.Response(200, text=""))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="no ETag"):
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)
    assert not posted.called


@pytest.mark.asyncio
async def test_delete_storage_mapping_reports_a_concurrent_change_on_412(mock_hmc):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response())
    mock_hmc.post(VIOS_PATH).mock(return_value=httpx.Response(412, text="etag stale"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="changed since they were read") as raised:
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)
    assert raised.value.status_code == 412


@pytest.mark.asyncio
async def test_delete_storage_mapping_rejects_malformed_parent(mock_hmc):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response("<broken>"))
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="not valid XML"):
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)


@pytest.mark.asyncio
async def test_delete_storage_mapping_rejects_xml_entities(mock_hmc):
    """An entity-bearing parent body raises HMCError, not a raw DefusedXmlException."""
    document = '<!DOCTYPE x [<!ENTITY payload "expanded">]><x>&payload;</x>'
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response(document))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="not valid XML"):
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)


@pytest.mark.asyncio
async def test_delete_storage_mapping_propagates_parent_post_failure(mock_hmc):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response())
    mock_hmc.post(VIOS_PATH).mock(
        return_value=httpx.Response(
            409,
            text="<HttpErrorResponse><Message>parent changed</Message></HttpErrorResponse>",
        )
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)
    assert raised.value.status_code == 409
    assert "parent changed" in str(raised.value)


@pytest.mark.asyncio
async def test_delete_storage_mapping_rejects_empty_selector(mock_hmc):
    fetched = mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response())
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="must not be empty"):
            await hmc.delete_storage_mapping(VIOS_UUID, "", LPAR_A)
    assert not fetched.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        f'<feed xmlns="http://www.w3.org/2005/Atom">{VIOS_PARENT}{VIOS_PARENT}</feed>',
        VIOS_PARENT.replace(
            f"<AtomID>{VIOS_UUID}</AtomID>", "<AtomID>wrong-vios</AtomID>"
        ),
        # AtomID matches but PartitionUUID names a different VIOS.
        VIOS_PARENT.replace(
            f'<PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>',
            '<PartitionUUID kb="ROO">wrong-vios</PartitionUUID>',
        ),
        # A duplicated PartitionUUID is ambiguous even though AtomID matches.
        VIOS_PARENT.replace(
            f'<PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>',
            f'<PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>'
            f'<PartitionUUID kb="ROO">{VIOS_UUID}</PartitionUUID>',
        ),
    ],
    ids=[
        "ambiguous-feed",
        "atomid-mismatch",
        "partitionuuid-mismatch",
        "partitionuuid-duplicate",
    ],
)
async def test_delete_storage_mapping_rejects_ambiguous_vios_document(
    mock_hmc, document
):
    mock_hmc.get(VIOS_PATH).mock(return_value=_vios_parent_response(document))
    posted = mock_hmc.post(VIOS_PATH).mock(return_value=httpx.Response(200, text=""))
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="VIOS resources|identity does not match"):
            await hmc.delete_storage_mapping(VIOS_UUID, DISK_ID, LPAR_A)
    assert not posted.called
