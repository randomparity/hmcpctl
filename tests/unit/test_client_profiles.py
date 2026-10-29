"""Fixed profile routes, raw preservation and exact HMC/partition containment."""

import xml.etree.ElementTree as ET
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hmcpctl.client.client_profiles import (
    profile_path,
    read_native_profile,
    require_fresh_profile,
    write_native_profile,
)
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.partition_profile import ATOM, UOM, N
from hmcpctl.errors import HMCError

P = "11111111-1111-4111-8111-111111111111"
S = "22222222-2222-4222-8222-222222222222"
F = "33333333-3333-4333-8333-333333333333"


def native_xml(*, href=None, uuid=F, published="before"):
    href = href or f"https://hmc.test:443/rest/api/uom/LogicalPartition/{P}"
    return f'''<entry xmlns="{ATOM}"><id>urn:uuid:{uuid}</id><published>{published}</published><content>
      <LogicalPartitionProfile xmlns="{UOM}" schemaVersion="V1_0">
      <Metadata><Atom><AtomID>{uuid}</AtomID><AtomCreated>created</AtomCreated></Atom></Metadata>
      <AssociatedPartition kb="ROR" href="{href}"/>
      <ProfileName kb="CUR">saved</ProfileName><ProfileType kb="ROR">REG_LPAR_PROFILE_TYPE</ProfileType><SettingID kb="ROO">17</SettingID>
      </LogicalPartitionProfile></content></entry>'''


def client(*responses):
    hmc = MagicMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    hmc._request_with_uuid_path_arguments = AsyncMock(side_effect=responses)
    return hmc


def response(xml=None, *, status=200, etag="opaque"):
    return httpx.Response(
        status,
        content=native_xml() if xml is None else xml,
        headers={"ETag": etag} if etag else {},
    )


@pytest.mark.asyncio
async def test_native_read_retains_xml_etag_identity_and_fixed_route():
    hmc = client(response())
    r = await read_native_profile(hmc, "LogicalPartition", P, F, S)
    assert (r.uuid, r.name, r.setting_id, r.etag) == (F, "saved", "17", "opaque")
    assert r.raw == native_xml()
    assert r.element.tag == N + "LogicalPartitionProfile"
    assert hmc._request_with_uuid_path_arguments.call_args.args == (
        "GET",
        f"/rest/api/uom/LogicalPartition/{P}/LogicalPartitionProfile/{F}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("upper_caller", [False, True])
@pytest.mark.parametrize("upper_entry", [False, True])
@pytest.mark.parametrize("upper_parent", [False, True])
async def test_native_profile_uuid_case(upper_caller, upper_entry, upper_parent):
    parent = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    profile = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    system = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    caller_parent = parent.upper() if upper_caller else parent
    caller_profile = profile.upper() if upper_caller else profile
    caller_system = system.upper() if upper_caller else system
    linked_parent = parent.upper() if upper_parent else parent
    linked_system = system.upper() if upper_parent else system
    raw = native_xml(
        uuid=profile.upper() if upper_entry else profile,
        href=f"https://hmc.test/rest/api/uom/ManagedSystem/{linked_system}/LogicalPartition/{linked_parent}",
    ).replace(">17</SettingID>", ">MiXeD-SettingID</SettingID>")
    hmc = client(response(raw))
    result = await read_native_profile(
        hmc, "LogicalPartition", caller_parent, caller_profile, caller_system
    )
    assert result.uuid == caller_profile
    assert result.raw == raw and result.setting_id == "MiXeD-SettingID"
    assert (
        hmc._request_with_uuid_path_arguments.call_args.args[1]
        == f"/rest/api/uom/LogicalPartition/{caller_parent}/LogicalPartitionProfile/{caller_profile}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["logicalpartition", "VirtualIOServer"])
async def test_native_profile_uuid_case_keeps_resource_kind(kind):
    with pytest.raises(ValueError):
        await read_native_profile(
            client(
                response(native_xml(href=f"https://hmc.test/rest/api/uom/{kind}/{P}"))
            ),
            "LogicalPartition",
            P,
            F,
            S,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href",
    [
        f"https://other.test:443/rest/api/uom/LogicalPartition/{P}",
        f"https://hmc.test:12443/rest/api/uom/LogicalPartition/{P}",
        f"https://user@hmc.test/rest/api/uom/LogicalPartition/{P}",
        f"https://hmc.test/rest/api/uom/LogicalPartition/{S}",
        f"https://hmc.test/rest/api/uom/VirtualIOServer/{P}",
        f"https://hmc.test/rest/api/uom/LogicalPartition/{P}?query=1",
    ],
)
async def test_parent_or_hmc_confusion_refused(href):
    hmc = client(response(native_xml(href=href)))
    with pytest.raises(ValueError):
        await read_native_profile(hmc, "LogicalPartition", P, F, S)


@pytest.mark.asyncio
async def test_vios_and_managed_system_anchored_parent_are_contained():
    hmc = client(
        response(
            native_xml(
                href=f"https://hmc.test/rest/api/uom/ManagedSystem/{S}/VirtualIOServer/{P}"
            )
        )
    )
    r = await read_native_profile(hmc, "VirtualIOServer", P, F, S)
    assert r.uuid == F


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        native_xml(uuid=S),
        "<entry/>",
        native_xml().replace('<SettingID kb="ROO">17</SettingID>', ""),
    ],
)
async def test_foreign_or_malformed_identity_refused(raw):
    hmc = client(response(raw))
    with pytest.raises(ValueError):
        await read_native_profile(hmc, "LogicalPartition", P, F, S)


@pytest.mark.asyncio
async def test_non_success_native_read_is_not_absence():
    hmc = client(response(status=404))
    with pytest.raises(HMCError):
        await read_native_profile(hmc, "LogicalPartition", P, F, S)


@pytest.mark.asyncio
async def test_native_post_carries_opaque_if_match_and_preserves_document():
    hmc = client(response(), response(status=200))
    r = await read_native_profile(hmc, "LogicalPartition", P, F, S)
    await write_native_profile(hmc, "LogicalPartition", P, r.element, r)
    call = hmc._request_with_uuid_path_arguments.call_args
    assert call.args == (
        "POST",
        f"/rest/api/uom/LogicalPartition/{P}/LogicalPartitionProfile/{F}",
    )
    assert call.kwargs["headers"]["If-Match"] == "opaque"
    assert ET.fromstring(call.kwargs["content"]).find(N + "SettingID").text == "17"
    assert "X-HMC-Schema-Version" not in call.kwargs["headers"]
    with pytest.raises(ValueError):
        await write_native_profile(
            hmc, "LogicalPartition", P, r.element, replace(r, etag=None)
        )


@pytest.mark.asyncio
async def test_freshness_ignores_only_outer_publication_text():
    hmc = client(response(), response(native_xml(published="later")))
    a = await read_native_profile(hmc, "LogicalPartition", P, F, S)
    b = await read_native_profile(hmc, "LogicalPartition", P, F, S)
    require_fresh_profile(a, b)
    for raw in [
        native_xml().replace("created", "changed"),
        native_xml().replace(
            "<published>before</published>",
            '<published attr="changed">before</published>',
        ),
    ]:
        with pytest.raises(ValueError):
            require_fresh_profile(a, replace(b, raw=raw))
    with pytest.raises(ValueError):
        require_fresh_profile(a, replace(b, etag="other"))


@pytest.mark.parametrize(
    "args",
    [
        ("LogicalPartition", "../other", F),
        ("Other", P, F),
        ("VirtualIOServer", P, "bad/id"),
    ],
)
def test_invalid_paths_refused_before_transport(args):
    with pytest.raises((ValueError, HMCError)):
        profile_path(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 204])
async def test_native_delete_preserves_acceptance_status_and_never_sends_conditional_delete(
    status,
):
    from hmcpctl.client.client_profiles import delete_native_profile

    hmc = client(response(status=status))
    assert await delete_native_profile(hmc, "LogicalPartition", P, F) == status
    call = hmc._request_with_uuid_path_arguments.call_args
    assert call.args == (
        "DELETE",
        f"/rest/api/uom/LogicalPartition/{P}/LogicalPartitionProfile/{F}",
    )
    assert "If-Match" not in call.kwargs["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid", ["not-a-uuid", F.replace("-", ""), "g" * 36])
async def test_native_profile_uuid_case_rejects_malformed_return(uuid):
    with pytest.raises(ValueError, match="identity differs"):
        await read_native_profile(
            client(response(native_xml(uuid=uuid))), "LogicalPartition", P, F, S
        )
