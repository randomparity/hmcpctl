"""Unscoped name lookups and create-time name checks fail closed (#1309).

A 2026-10-13 read-only capture found the HMC-wide ``PartitionName`` searches
answering 204 for a name on a ``No Connection`` system, exactly as for a name
that exists nowhere. ``ManagedSystem/quick/All`` answered, and the captured
``rest-ms-quick-all-no-connection-v11r2`` record reports two of eleven systems
with ``State`` ``no connection``; ``rest-ms-quick-all`` is an all-operating fleet.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import (
    assert_no_mutating_requests,
    captured_lpar_entry,
    live_response,
    make_config,
)

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionStorage,
    preview_provision,
)

SYS_B = "bbbbbbbb-0000-4000-8000-00000000000b"
LPAR_UUID = "11111111-0000-4000-8000-000000000001"
VIOS_UUID = "22222222-0000-4000-8000-000000000002"

SEARCHES = {
    "LPAR": ("find_partition_by_name", "LogicalPartition", "lpar-1"),
    "VIOS": ("find_vios_by_name", "VirtualIOServer", "vios-1"),
}

DISCONNECTED = {
    "sys-R11": "000002ff-abcd-4ef0-8abc-0000000002ff",
    "sys-R8": "00000219-abcd-4ef0-8abc-000000000219",
}


def _quick_all(mock_hmc, fixture: str):
    path, response = live_response(fixture)
    return mock_hmc.get(path).mock(return_value=response)


def _feed(*entries: str) -> str:
    return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"


def _search(mock_hmc, collection: str, name: str, response: httpx.Response):
    return mock_hmc.get(
        f"/rest/api/uom/{collection}/search/(PartitionName=={name})"
    ).mock(return_value=response)


async def _lookup(label: str):
    method, _, name = SEARCHES[label]
    async with HMCClient(make_config()) as hmc:
        return await getattr(hmc, method)(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(SEARCHES))
async def test_a_miss_while_a_system_has_no_connection_names_it(mock_hmc, label):
    _, collection, name = SEARCHES[label]
    _search(mock_hmc, collection, name, httpx.Response(204))
    _quick_all(mock_hmc, "rest-ms-quick-all-no-connection-v11r2")

    with pytest.raises(HMCError) as raised:
        await _lookup(label)

    message = str(raised.value)
    assert f"No {label} named {name!r} was found on the readable managed" in message
    for system, uuid in DISCONNECTED.items():
        assert f"'{system}' ({uuid}) in State 'no connection'" in message
    assert "'sys-R1'" not in message and "'sys-R2'" not in message
    assert "supply a managed-system selector" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(SEARCHES))
async def test_a_miss_on_an_all_operating_fleet_is_not_found(mock_hmc, label):
    _, collection, name = SEARCHES[label]
    _search(mock_hmc, collection, name, httpx.Response(204))
    quick_all = _quick_all(mock_hmc, "rest-ms-quick-all")

    assert await _lookup(label) is None
    assert quick_all.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(SEARCHES))
async def test_a_hit_is_returned_without_reading_quick_all(mock_hmc, label):
    _, collection, name = SEARCHES[label]
    _search(
        mock_hmc,
        collection,
        name,
        httpx.Response(200, text=_feed(captured_lpar_entry(LPAR_UUID, name))),
    )
    quick_all = _quick_all(mock_hmc, "rest-ms-quick-all-no-connection-v11r2")

    found = await _lookup(label)

    assert found["UUID"] == LPAR_UUID
    assert not quick_all.called


@pytest.mark.asyncio
async def test_a_summary_that_is_not_an_object_cannot_prove_absence(mock_hmc):
    _search(mock_hmc, "LogicalPartition", "lpar-1", httpx.Response(204))
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, json=["sys-A"])
    )

    with pytest.raises(HMCError, match="an unidentified managed system"):
        await _lookup("LPAR")


@pytest.mark.asyncio
async def test_a_failed_quick_all_read_fails_the_lookup(mock_hmc):
    _search(mock_hmc, "LogicalPartition", "lpar-1", httpx.Response(204))
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(HMCError, match="quick/All failed"):
        await _lookup("LPAR")


TARGET_NAME = "sys-R11"
TARGET = DISCONNECTED[TARGET_NAME]


def _disconnected_target(mock_hmc):
    """The captured sys-R11: its scoped feed answers 204, quick/All says no connection.

    The 2026-10-13 capture saw ``ManagedSystem/{uuid}`` time out for such a
    system (#1500), so that route raises a timeout here and must stay unread.
    """
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{TARGET}/LogicalPartition").mock(
        return_value=httpx.Response(204)
    )
    _quick_all(mock_hmc, "rest-ms-quick-all-no-connection-v11r2")
    entry = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{TARGET}").mock(
        side_effect=httpx.ReadTimeout("ManagedSystem/{uuid} stalled")
    )
    hmc_wide = mock_hmc.get(
        url__regex=r"/rest/api/uom/LogicalPartition/search/.*"
    ).mock(return_value=httpx.Response(204))
    return entry, hmc_wide


def _assert_prompt_refusal(raised, mock_hmc, entry, hmc_wide) -> None:
    message = str(raised.value)
    assert f"{TARGET_NAME!r} ({TARGET})" in message
    assert "State 'no connection'" in message
    assert message.endswith("retry when it reconnects")
    assert not entry.called
    assert not hmc_wide.called
    assert_no_mutating_requests(mock_hmc)


@pytest.mark.asyncio
async def test_create_refuses_before_any_write_when_the_target_cannot_be_read(
    mock_hmc,
):
    entry, hmc_wide = _disconnected_target(mock_hmc)
    creation = LparCreation(
        name="lpar-1",
        partition_type="AIX/Linux",
        resources=LparResources(desired_memory=2048, desired_vcpus=1),
    )

    with (
        patch("hmcpctl.operations.lpar.core.create_lpar_via_cli") as via_cli,
        pytest.raises(HMCError) as raised,
    ):
        async with HMCClient(make_config()) as hmc:
            await create_and_stamp_lpar(hmc, TARGET, creation)

    via_cli.assert_not_called()
    _assert_prompt_refusal(raised, mock_hmc, entry, hmc_wide)


@pytest.mark.asyncio
async def test_provision_refuses_before_any_write_when_the_target_cannot_be_read(
    mock_hmc,
):
    entry, hmc_wide = _disconnected_target(mock_hmc)
    request = ProvisionRequest(
        name="lpar-1",
        adapters=ProvisionAdapters(port_vlan_id=100),
        storage=ProvisionStorage(vios_uuid=VIOS_UUID, storage_name="lv_boot"),
        resources=LparResources(desired_memory=2048, desired_vcpus=1),
    )

    with (
        patch(
            "hmcpctl.operations.lpar.core.create_lpar_via_cli", new=AsyncMock()
        ) as via_cli,
        patch("hmcpctl.operations.lpar.provision._check_profile_jobs", new=AsyncMock()),
        pytest.raises(HMCError) as raised,
    ):
        async with HMCClient(make_config()) as hmc:
            await preview_provision(hmc, TARGET, request)

    via_cli.assert_not_awaited()
    _assert_prompt_refusal(raised, mock_hmc, entry, hmc_wide)


@pytest.mark.asyncio
async def test_a_same_named_partition_on_another_system_does_not_block_create(
    mock_hmc,
):
    hmc_wide = mock_hmc.get(
        url__regex=r"/rest/api/uom/LogicalPartition/search/.*"
    ).mock(
        return_value=httpx.Response(
            200, text=_feed(captured_lpar_entry(LPAR_UUID, "lpar-1"))
        )
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYS_B}/LogicalPartition").mock(
        return_value=httpx.Response(
            200, text=_feed(captured_lpar_entry(VIOS_UUID, "other-lpar"))
        )
    )
    create = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYS_B}/LogicalPartition").mock(
        return_value=httpx.Response(
            201, text=_feed(captured_lpar_entry(LPAR_UUID, "lpar-1"))
        )
    )
    creation = LparCreation(
        name="lpar-1",
        partition_type="AIX/Linux",
        resources=LparResources(desired_memory=2048, desired_vcpus=1),
    )

    with (
        patch(
            "hmcpctl.operations.lpar.core.require_memory_within_system",
            new=AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership._resolve_system_name",
            new=AsyncMock(return_value="sys-B"),
        ),
    ):
        async with HMCClient(make_config()) as hmc:
            result = await create_and_stamp_lpar(hmc, SYS_B, creation)

    assert create.called
    assert not hmc_wide.called
    assert result.resource_created is True
