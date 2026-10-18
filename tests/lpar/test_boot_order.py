"""LPAR boot order: Advanced-group read and PendingBootString read-modify-write (#980).

V10R3 keeps ``PendingBootString`` inside ``BootListInformation`` and rejects a sparse
``LogicalPartition`` that places it at the top level. The fixture follows the live V10R3
``?group=Advanced`` shape with synthetic values.
"""

import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import make_config
from defusedxml import ElementTree as DET

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.boot import join_boot_device_paths
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.boot_order import (
    clear_lpar_boot_order,
    read_lpar_boot_order,
    set_lpar_boot_order,
)
from hmcpctl.xmlutil import localname, parse_feed

LPAR = "aaaa0000-0000-0000-0000-000000000001"
LPAR_PATH = f"/rest/api/uom/LogicalPartition/{LPAR}"
ETAG = "-796546617"
DISK = "/vdevice/v-scsi@30000003/disk@8100000000000000"
LAN = (
    "/vdevice/l-lan@30000002:speed=auto,duplex=auto,"
    "192.0.2.10,,192.0.2.20,192.0.2.1,5,5,255.255.255.0,512"
)
UOM = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"


def _field(name: str, kb: str, value: str) -> str:
    attrs = f'group="Advanced" ksv="V1_5_0" kxe="false" kb="{kb}"'
    return f"<{name} {attrs}>{value}</{name}>" if value else f"<{name} {attrs}/>"


def _entry(
    *,
    pending: str = "",
    devices: str = "",
    last: str = "",
    boot_list: bool = True,
    pending_element: bool = True,
) -> str:
    pending_xml = _field("PendingBootString", "UOO", pending) if pending_element else ""
    boot_list_xml = (
        f"""
    <BootListInformation ksv="V1_5_0" kb="UOD" kxe="false" schemaVersion="V1_0">
        <Metadata>
            <Atom/>
        </Metadata>
        {pending_xml}
        {_field("BootDeviceList", "ROO", devices)}
        {_field("ShadowBootDeviceList", "ROO", ",," if devices else "")}
        {_field("LastBootedDeviceString", "ROO", last)}
    </BootListInformation>"""
        if boot_list
        else ""
    )
    return f"""<entry xmlns="http://www.w3.org/2005/Atom">
    <id>{LPAR}</id>
    <title>LogicalPartition</title>
    <link rel="SELF" href="https://hmc.example.invalid:12443{LPAR_PATH}?group=Advanced"/>
    <etag:etag xmlns:etag="{UOM}" xmlns="{UOM}">{ETAG}</etag:etag>
    <content type="application/vnd.ibm.powervm.uom+xml; type=LogicalPartition">
        <LogicalPartition:LogicalPartition xmlns:LogicalPartition="{UOM}" xmlns="{UOM}" xmlns:ns2="http://www.w3.org/XML/1998/namespace/k2" schemaVersion="V1_0">
    <Metadata>
        <Atom>
            <AtomID>{LPAR}</AtomID>
        </Atom>
    </Metadata>
    <AllowPerformanceDataCollection kxe="false" kb="CUD">false</AllowPerformanceDataCollection>
    <PartitionName kb="CUR" kxe="false">lpar-a</PartitionName>
    <DesignatedIPLSource kxe="false" kb="CUD">a</DesignatedIPLSource>{boot_list_xml}
    <AssociatedTrunkAdapters kb="ROO" kxe="false"/>
</LogicalPartition:LogicalPartition>
    </content>
</entry>"""


def _routes(
    mock_hmc,
    *,
    etag: str | None = ETAG,
    entry: str | None = None,
    get_status: int = 200,
    post_status: int = 200,
):
    headers = {"ETag": etag} if etag else {}
    body = entry if entry is not None else _entry()
    get = mock_hmc.get(LPAR_PATH, params={"group": "Advanced"}).mock(
        return_value=httpx.Response(get_status, text=body, headers=headers)
    )
    post = mock_hmc.post(LPAR_PATH, params={"group": "Advanced"}).mock(
        return_value=httpx.Response(post_status, text=body)
    )
    return get, post


def _lpar(xml: str | bytes) -> ET.Element:
    root = DET.fromstring(xml)
    if localname(root.tag) == "LogicalPartition":
        return root
    return next(el for el in root.iter() if localname(el.tag) == "LogicalPartition")


def _canonical(el: ET.Element) -> str:
    return ET.canonicalize(ET.tostring(el), strip_text=True)


async def _set(boot_string: str, lpar: str = LPAR):
    async with HMCClient(make_config()) as hmc:
        return await hmc.set_pending_boot_string(lpar, boot_string)


# ------------------------------------------------------------------ #
# join_boot_device_paths
# ------------------------------------------------------------------ #


def test_join_boot_device_paths_joins_in_order_with_single_spaces():
    assert join_boot_device_paths([DISK, LAN]) == f"{DISK} {LAN}"
    assert join_boot_device_paths([LAN]) == LAN


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        ([], "at least one Open Firmware device path"),
        (["disk"], "Invalid boot device path: 'disk'"),
        (["cd"], "Invalid boot device path"),
        ([DISK, "/a b"], "Invalid boot device path: '/a b'"),
        (["/a\tb"], "Invalid boot device path"),
        (["/a\nb"], "Invalid boot device path"),
        (["/a\x00"], "Invalid boot device path"),
        (["/a\x7f"], "Invalid boot device path"),
        (["/a\ufffe"], "Invalid boot device path"),
        (["/a\ud800"], "Invalid boot device path"),
        (["/caf\u00e9"], "Invalid boot device path"),
        ([""], "Invalid boot device path"),
    ],
)
def test_join_boot_device_paths_rejects_what_would_not_round_trip(paths, message):
    with pytest.raises(ValueError, match=message):
        join_boot_device_paths(paths)


# ------------------------------------------------------------------ #
# LparsMixin.set_pending_boot_string
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_set_pending_boot_string_posts_whole_partition_with_if_match(mock_hmc):
    get, post = _routes(mock_hmc, entry=_entry(devices=f"{DISK} {LAN}", last=DISK))

    await _set(f"{DISK} {LAN}")

    assert get.call_count == 1 and post.call_count == 1
    assert "X-HMC-Schema-Version" not in get.calls.last.request.headers
    request = post.calls.last.request
    assert request.headers["If-Match"] == ETAG
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"] == (
        "application/vnd.ibm.powervm.uom+xml; type=LogicalPartition"
    )
    posted = _lpar(request.content)
    expected = _lpar(
        _entry(pending=f"{DISK} {LAN}", devices=f"{DISK} {LAN}", last=DISK)
    )
    assert _canonical(posted) == _canonical(expected)
    pending = next(
        el for el in posted.iter() if localname(el.tag) == "PendingBootString"
    )
    assert pending.attrib == {
        "group": "Advanced",
        "ksv": "V1_5_0",
        "kxe": "false",
        "kb": "UOO",
    }
    assert pending.text == f"{DISK} {LAN}"


@pytest.mark.asyncio
async def test_set_pending_boot_string_clears_to_an_empty_element(mock_hmc):
    _, post = _routes(mock_hmc, entry=_entry(pending=DISK))

    await _set("")

    posted = _lpar(post.calls.last.request.content)
    assert _canonical(posted) == _canonical(_lpar(_entry()))


@pytest.mark.asyncio
async def test_set_pending_boot_string_escapes_the_value_once(mock_hmc):
    _, post = _routes(mock_hmc)

    await _set("/a&b<c")

    assert b"/a&amp;b&lt;c" in post.calls.last.request.content
    pending = next(
        el
        for el in _lpar(post.calls.last.request.content).iter()
        if localname(el.tag) == "PendingBootString"
    )
    assert pending.text == "/a&b<c"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"etag": None}, "no ETag"),
        ({"entry": _entry(boot_list=False)}, "BootListInformation/PendingBootString"),
        (
            {"entry": _entry(pending_element=False)},
            "BootListInformation/PendingBootString",
        ),
        (
            {"entry": "<entry xmlns='http://www.w3.org/2005/Atom'/>"},
            "no LogicalPartition",
        ),
        ({"entry": "<not-xml"}, "not valid XML"),
        (
            {
                "entry": '<!DOCTYPE entry [<!ENTITY unsafe "value">]>'
                "<entry>&unsafe;</entry>"
            },
            "not valid XML",
        ),
        ({"get_status": 500}, "failed"),
    ],
)
@pytest.mark.asyncio
async def test_set_pending_boot_string_refuses_before_any_post(
    mock_hmc, kwargs, message
):
    _, post = _routes(mock_hmc, **kwargs)

    with pytest.raises(HMCError, match=message):
        await _set(DISK)

    assert not post.called


@pytest.mark.asyncio
async def test_set_pending_boot_string_reports_a_412_as_nothing_written(mock_hmc):
    _routes(mock_hmc, post_status=412)

    with pytest.raises(HMCError, match="Nothing was written") as exc_info:
        await _set(DISK)

    assert exc_info.value.status_code == 412


@pytest.mark.asyncio
async def test_set_pending_boot_string_raises_on_a_rejected_post(mock_hmc):
    _routes(mock_hmc, post_status=400)

    with pytest.raises(HMCError, match="POST .* failed") as exc_info:
        await _set(DISK)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_set_pending_boot_string_refuses_a_non_uuid_before_any_request(mock_hmc):
    with pytest.raises(ValueError):
        await _set(DISK, lpar="../ManagedSystem")

    assert not any("LogicalPartition" in str(c.request.url) for c in mock_hmc.calls)


# ------------------------------------------------------------------ #
# Operations
# ------------------------------------------------------------------ #


def _parsed(entry: str) -> dict:
    return parse_feed(entry)[0]


@pytest.mark.asyncio
async def test_read_lpar_boot_order_returns_strings_from_the_advanced_group():
    hmc = AsyncMock()
    hmc.get_uom.return_value = _parsed(_entry(devices=f"{DISK} {LAN}", last=DISK))

    with patch(
        "hmcpctl.operations.lpar.boot_order.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR),
    ) as resolve:
        result = await read_lpar_boot_order(hmc, "system-a", "lpar-a")

    assert result == {
        "lpar_uuid": LPAR,
        "lpar_name": "lpar-a",
        "pending_boot_string": None,
        "boot_device_list": f"{DISK} {LAN}",
        "last_booted_device_string": DISK,
    }
    hmc.get_uom.assert_awaited_once_with("LogicalPartition", LPAR, group="Advanced")
    resolve.assert_awaited_once_with(hmc, "lpar-a", system_name_or_uuid="system-a")


@pytest.mark.asyncio
async def test_read_lpar_boot_order_reports_empty_fields_as_none():
    hmc = AsyncMock()
    hmc.get_uom.return_value = _parsed(_entry())

    with patch(
        "hmcpctl.operations.lpar.boot_order.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR),
    ):
        result = await read_lpar_boot_order(hmc, "system-a", "lpar-a")

    assert result["pending_boot_string"] is None
    assert result["boot_device_list"] is None
    assert result["last_booted_device_string"] is None


@pytest.mark.asyncio
async def test_read_lpar_boot_order_rejects_missing_lpar():
    hmc = AsyncMock()
    hmc.get_uom.return_value = None

    with (
        patch(
            "hmcpctl.operations.lpar.boot_order.resolve_lpar_uuid",
            new=AsyncMock(return_value="missing"),
        ),
        pytest.raises(ValueError, match="LPAR 'missing' not found"),
    ):
        await read_lpar_boot_order(hmc, "system-a", "missing")


@pytest.mark.asyncio
async def test_set_lpar_boot_order_validates_before_authorizing():
    hmc = AsyncMock()
    authorize = AsyncMock(return_value=LPAR)

    with (
        patch(
            "hmcpctl.operations.lpar.boot_order.resolve_and_authorize_lpar_mutation",
            new=authorize,
        ),
        pytest.raises(ValueError, match="Invalid boot device path"),
    ):
        await set_lpar_boot_order(hmc, "system-1", "lpar-1", ["cd", "disk"])

    authorize.assert_not_awaited()
    hmc.set_pending_boot_string.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_lpar_boot_order_authorizes_then_writes_the_joined_paths():
    events: list[object] = []
    hmc = AsyncMock()
    hmc.set_pending_boot_string.side_effect = lambda uuid, value: (
        events.append(("write", uuid, value)) or {"Resource": {"UUID": uuid}}
    )

    async def authorize(*args, **kwargs):
        events.append(("authorize", args[1:], kwargs))
        return LPAR

    with patch(
        "hmcpctl.operations.lpar.boot_order.resolve_and_authorize_lpar_mutation",
        side_effect=authorize,
    ):
        result = await set_lpar_boot_order(
            hmc, "system-1", "lpar-1", [DISK, LAN], ownership_override=True
        )

    assert events == [
        ("authorize", ("system-1", "lpar-1"), {"ownership_override": True}),
        ("write", LPAR, f"{DISK} {LAN}"),
    ]
    assert result == {"Resource": {"UUID": LPAR}}


@pytest.mark.asyncio
async def test_clear_lpar_boot_order_refuses_after_authorization():
    hmc = AsyncMock()

    with (
        patch(
            "hmcpctl.operations.lpar.boot_order.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(return_value=LPAR),
        ) as authorize,
        pytest.raises(HMCError, match="REST0126") as exc_info,
    ):
        await clear_lpar_boot_order(hmc, "system-1", "lpar-1")

    authorize.assert_awaited_once_with(
        hmc, "system-1", "lpar-1", ownership_override=False
    )
    assert hmc.mock_calls == []
    assert exc_info.value.status_code is None
    assert "set-boot-order" in str(exc_info.value)
    assert "activation" in str(exc_info.value)


@pytest.mark.asyncio
async def test_set_lpar_boot_order_translates_hmc_not_acceptable():
    body = "<Error><Message>schema mismatch</Message></Error>"
    hmc = AsyncMock()
    hmc.set_pending_boot_string.side_effect = HMCError(
        "write failed", status_code=406, body=body
    )

    with (
        patch(
            "hmcpctl.operations.lpar.boot_order.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(return_value=LPAR),
        ),
        pytest.raises(HMCError, match="Not Acceptable") as exc_info,
    ):
        await set_lpar_boot_order(hmc, "system-1", "lpar-1", [DISK])

    assert exc_info.value.status_code == 406
    assert exc_info.value.body == body
    assert isinstance(exc_info.value.__cause__, HMCError)
