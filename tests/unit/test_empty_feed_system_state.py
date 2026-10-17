"""An empty per-system feed from a system that is not operating (#1289).

V11R2 SP1120 answered ``ManagedSystem/{uuid}/LogicalPartition`` and
``.../VirtualIOServer`` with 204 and no body while a POWER10 9080-HEX read
``State`` ``recovery`` (``DetailedState`` ``Recovery``) and later
``no connection`` (``Unknown``). The values are in
``tests/fixtures/live/vocabulary/v11r2-p10-9080-hex.json``. The check reads
``State`` from ``ManagedSystem/quick/All``, because ``ManagedSystem/{uuid}``
timed out for a ``no connection`` system on the same release (#1500); that
route raises a timeout here and must stay unread.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import captured_lpar_entry, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.capacity import fetch_capacity_report
from hmcpctl.operations.inventory.composite import fetch_system_summary
from hmcpctl.operations.inventory.utilization import read_system
from hmcpctl.operations.lpar import decommission as decommission_module
from hmcpctl.operations.lpar.core import get_lpar, list_lpars
from hmcpctl.operations.lpar.decommission import decommission_lpar
from hmcpctl.operations.lpar.ownership import (
    _discover_owning_system,
    list_lpar_ownership,
)
from hmcpctl.operations.systems.fleet import FleetListing
from hmcpctl.operations.systems.health import fetch_fleet_health
from hmcpctl.operations.templates import core as templates_module
from hmcpctl.operations.templates.core import (
    BASELINE_SNAPSHOT_WARNING,
    POST_SNAPSHOT_WARNING,
    deploy_partition_template,
)
from hmcpctl.operations.vios.core import list_vios
from hmcpctl.resource_identity import (
    ResourceNotFoundError,
    resolve_lpar_uuid,
    resolve_vios_uuid,
)

SYSTEM_UUID = "11111111-1111-1111-1111-111111111111"
SYSTEM_NAME = "sys-R1"
SYSTEM_PATH = f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"
QUICK_ALL = "/rest/api/uom/ManagedSystem/quick/All"
OPERATING_UUID = "33333333-3333-3333-3333-333333333333"
OPERATING_FEED = f"/rest/api/uom/ManagedSystem/{OPERATING_UUID}/LogicalPartition"
LPAR_UUID = "22222222-2222-2222-2222-222222222222"
LPAR_FEED = f"{SYSTEM_PATH}/LogicalPartition"
VIOS_FEED = f"{SYSTEM_PATH}/VirtualIOServer"

_VOCABULARY = json.loads(
    (
        Path(__file__).parents[1] / "fixtures/live/vocabulary/v11r2-p10-9080-hex.json"
    ).read_text()
)["rest"]["values"]

NOT_OPERATING = ["recovery", "no connection"]

Read = Callable[[HMCClient], Awaitable[Any]]


async def _entries(listing: Awaitable[FleetListing]) -> list[dict[str, Any]]:
    return (await listing).entries


READS: dict[str, tuple[str, Read]] = {
    "client_list_lpars": (
        LPAR_FEED,
        lambda hmc: hmc.list_logical_partitions(SYSTEM_UUID),
    ),
    "client_list_vios": (VIOS_FEED, lambda hmc: hmc.list_vios(SYSTEM_UUID)),
    "list_lpars": (LPAR_FEED, lambda hmc: _entries(list_lpars(hmc, SYSTEM_UUID))),
    "get_lpar": (
        LPAR_FEED,
        lambda hmc: get_lpar(hmc, "lpar-a", system_name_or_uuid=SYSTEM_UUID),
    ),
    "resolve_lpar_uuid": (
        LPAR_FEED,
        lambda hmc: resolve_lpar_uuid(hmc, "lpar-a", system_name_or_uuid=SYSTEM_UUID),
    ),
    "list_vios": (VIOS_FEED, lambda hmc: _entries(list_vios(hmc, SYSTEM_UUID))),
    "list_lpar_ownership": (
        LPAR_FEED,
        lambda hmc: _entries(list_lpar_ownership(hmc, SYSTEM_UUID)),
    ),
    "decommission_target": (
        LPAR_FEED,
        lambda hmc: decommission_lpar(hmc, SYSTEM_UUID, "lpar-a", dry_run=True),
    ),
    "resolve_vios_uuid": (
        VIOS_FEED,
        lambda hmc: resolve_vios_uuid(hmc, "vios-a", system_name_or_uuid=SYSTEM_UUID),
    ),
}


def _system(*elements: str) -> str:
    body = "".join(elements)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{SYSTEM_UUID}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>{SYSTEM_NAME}</SystemName>{body}
    </ManagedSystem>
  </content>
</entry>
"""


def _summary(state: str, uuid: str = SYSTEM_UUID, name: str = SYSTEM_NAME, **extra):
    return {
        "UUID": uuid,
        "SystemName": name,
        "State": state,
        "StateDetail": "",
        **extra,
    }


def _quick_all(router, *summaries: object):
    """Answer quick/All with *summaries*, and stall the per-system entry (#1500)."""
    router.get(SYSTEM_PATH).mock(side_effect=httpx.ReadTimeout("stalled"))
    return router.get(QUICK_ALL).mock(
        return_value=httpx.Response(200, json=list(summaries))
    )


def _assert_entry_unread(router) -> None:
    assert SYSTEM_PATH not in [call.request.url.path for call in router.calls]


def test_states_are_the_captured_values() -> None:
    for state in [*NOT_OPERATING, "operating"]:
        assert state in _VOCABULARY["State"]


@pytest.mark.asyncio
@pytest.mark.parametrize("read", sorted(READS))
@pytest.mark.parametrize("state", NOT_OPERATING)
async def test_empty_feed_from_a_system_not_operating_raises(
    mock_hmc, read, state
) -> None:
    feed, call = READS[read]
    mock_hmc.get(feed).mock(return_value=httpx.Response(204))
    _quick_all(mock_hmc, _summary(state), _summary("operating", OPERATING_UUID))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as caught:
            await call(hmc)

    message = str(caught.value)
    assert f"{SYSTEM_NAME!r} ({SYSTEM_UUID})" in message
    assert f"State {state!r}," in message
    assert "StateDetail" not in message
    assert "retry when it reconnects" in message
    assert caught.value.status_code is None
    _assert_entry_unread(mock_hmc)


@pytest.mark.asyncio
async def test_a_state_detail_is_named_when_present(mock_hmc) -> None:
    mock_hmc.get(LPAR_FEED).mock(return_value=httpx.Response(204))
    _quick_all(mock_hmc, _summary("recovery", StateDetail="Recovery"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError, match=r"State 'recovery' \(StateDetail 'Recovery'\),"
        ):
            await hmc.list_logical_partitions(SYSTEM_UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read", ["list_lpars", "list_vios", "client_list_lpars", "client_list_vios"]
)
@pytest.mark.parametrize("state", ["operating", "Operating"])
async def test_empty_feed_from_an_operating_system_is_empty(
    mock_hmc, read, state
) -> None:
    feed, call = READS[read]
    mock_hmc.get(feed).mock(return_value=httpx.Response(204))
    summaries = _quick_all(
        mock_hmc,
        _summary("no connection", OPERATING_UUID),
        _summary(state, ReferenceCode="0102-0001-00004012"),
    )

    async with HMCClient(make_config()) as hmc:
        assert await call(hmc) == []

    assert summaries.call_count == 1
    _assert_entry_unread(mock_hmc)


@pytest.mark.asyncio
async def test_the_system_uuid_matches_in_any_letter_case(mock_hmc) -> None:
    upper = SYSTEM_UUID.upper().replace("1", "A")
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{upper}/LogicalPartition").mock(
        return_value=httpx.Response(204)
    )
    _quick_all(mock_hmc, _summary("operating", upper.lower()))

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_logical_partitions(upper) == []


@pytest.mark.asyncio
async def test_scoped_lpar_lookup_on_an_operating_system_is_not_found(mock_hmc):
    mock_hmc.get(LPAR_FEED).mock(return_value=httpx.Response(204))
    summaries = _quick_all(mock_hmc, _summary("operating"))

    async with HMCClient(make_config()) as hmc:
        assert await get_lpar(hmc, "lpar-a", system_name_or_uuid=SYSTEM_UUID) is None

    assert summaries.call_count == 1


@pytest.mark.asyncio
async def test_scoped_vios_lookup_on_an_operating_system_is_not_found(mock_hmc):
    mock_hmc.get(VIOS_FEED).mock(return_value=httpx.Response(204))
    summaries = _quick_all(mock_hmc, _summary("operating"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ResourceNotFoundError):
            await resolve_vios_uuid(hmc, "vios-a", system_name_or_uuid=SYSTEM_UUID)

    assert summaries.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summaries", "label"),
    [
        ([{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}], repr(SYSTEM_NAME)),
        ([_summary("operating", OPERATING_UUID)], SYSTEM_UUID),
        ([SYSTEM_UUID, {"UUID": None, "State": "operating"}], SYSTEM_UUID),
    ],
    ids=["no-state", "not-listed", "not-an-object"],
)
async def test_unreadable_state_reads_unknown(mock_hmc, summaries, label) -> None:
    mock_hmc.get(LPAR_FEED).mock(return_value=httpx.Response(204))
    _quick_all(mock_hmc, *summaries)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as caught:
            await list_lpars(hmc, SYSTEM_UUID)

    message = str(caught.value)
    assert f"managed system {label}" in message
    assert "State 'unknown'," in message


@pytest.mark.asyncio
async def test_a_failed_quick_all_read_propagates(mock_hmc) -> None:
    mock_hmc.get(LPAR_FEED).mock(return_value=httpx.Response(204))
    mock_hmc.get(QUICK_ALL).mock(return_value=httpx.Response(500))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="quick/All failed") as caught:
            await hmc.list_logical_partitions(SYSTEM_UUID)

    assert caught.value.status_code == 500


@pytest.mark.asyncio
async def test_non_empty_feed_does_not_read_the_system(mock_hmc) -> None:
    lpar_uuid = "22222222-2222-2222-2222-222222222222"
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"{captured_lpar_entry(lpar_uuid, 'lpar-a')}</feed>"
    )
    mock_hmc.get(LPAR_FEED).mock(return_value=httpx.Response(200, text=feed))
    summaries = _quick_all(mock_hmc, _summary("recovery"))

    async with HMCClient(make_config()) as hmc:
        assert (await list_lpars(hmc, SYSTEM_UUID, state="running")).entries == []
        assert await get_lpar(hmc, "lpar-b", system_name_or_uuid=SYSTEM_UUID) is None

    assert not summaries.called


@pytest.mark.asyncio
async def test_non_empty_vios_feed_does_not_read_the_system(mock_hmc) -> None:
    vios_uuid = "22222222-2222-2222-2222-222222222222"
    entry = captured_lpar_entry(vios_uuid, "vios-a", "running").replace(
        "LogicalPartition", "VirtualIOServer"
    )
    feed = f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'
    mock_hmc.get(VIOS_FEED).mock(return_value=httpx.Response(200, text=feed))
    summaries = _quick_all(mock_hmc, _summary("recovery"))

    async with HMCClient(make_config()) as hmc:
        (vios,) = await hmc.list_vios(SYSTEM_UUID)

    assert vios["UUID"] == vios_uuid
    assert not summaries.called


def _fleet_entry(state: str, uuid: str = SYSTEM_UUID, name: str = SYSTEM_NAME):
    return {"UUID": uuid, "Resource": {"SystemName": name, "State": state}}


def _mock_no_connection_system(router):
    """sys-R1 is ``no connection`` and sys-R2 operating; returns sys-R1's LPAR feed."""
    lpar_feed = router.get(LPAR_FEED).mock(return_value=httpx.Response(204))
    router.get(VIOS_FEED).mock(return_value=httpx.Response(204))
    _quick_all(
        router,
        _summary("no connection"),
        _summary("operating", OPERATING_UUID, "sys-R2"),
    )
    return lpar_feed


@pytest.mark.asyncio
async def test_fleet_health_warns_for_a_non_operating_system(mock_hmc) -> None:
    _mock_no_connection_system(mock_hmc)
    _quick_all(mock_hmc, _summary("no connection"))

    async with HMCClient(make_config()) as hmc:
        result = await fetch_fleet_health(hmc)

    assert [system["state"] for system in result.systems] == ["no connection"]
    assert result.lpars == () and result.vios == ()
    lpar_warning, vios_warning = result.warnings
    assert lpar_warning.startswith(f"LPAR inventory for system {SYSTEM_NAME}")
    assert vios_warning.startswith(f"VIOS inventory for system {SYSTEM_NAME}")
    assert all("State 'no connection'" in warning for warning in result.warnings)


def _capacity_entry(state: str, uuid: str, name: str) -> dict[str, Any]:
    entry = _fleet_entry(state, uuid, name)
    entry["Resource"]["AssociatedSystemMemoryConfiguration"] = {
        "ConfigurableSystemMemory": "131072",
        "CurrentAvailableSystemMemory": "112448",
    }
    entry["Resource"]["AssociatedSystemProcessorConfiguration"] = {
        "ConfigurableSystemProcessorUnits": "20",
        "CurrentAvailableSystemProcessorUnits": "18",
    }
    return entry


def _operating_entry(monkeypatch, hmc: HMCClient) -> None:
    """sys-R2's full entry, the only one the capacity report reads."""
    entry = _capacity_entry("operating", OPERATING_UUID, "sys-R2")
    read = AsyncMock(side_effect=lambda uuid: entry if uuid == OPERATING_UUID else None)
    monkeypatch.setattr(hmc, "get_managed_system", read)


@pytest.mark.asyncio
async def test_capacity_report_omits_a_non_operating_system(
    mock_hmc, monkeypatch, caplog
) -> None:
    no_connection = _mock_no_connection_system(mock_hmc)
    mock_hmc.get(OPERATING_FEED).mock(return_value=httpx.Response(204))

    async with HMCClient(make_config()) as hmc:
        _operating_entry(monkeypatch, hmc)
        with caplog.at_level(logging.WARNING):
            report = await fetch_capacity_report(hmc)

    assert [(item.system_name, item.total_lpars) for item in report] == [("sys-R2", 0)]
    assert any(SYSTEM_UUID in record.getMessage() for record in caplog.records)
    assert not no_connection.called


@pytest.mark.asyncio
async def test_capacity_report_fails_on_an_operating_system_refusal(
    mock_hmc, monkeypatch
) -> None:
    _mock_no_connection_system(mock_hmc)
    mock_hmc.get(OPERATING_FEED).mock(return_value=httpx.Response(500, text="failure"))

    async with HMCClient(make_config()) as hmc:
        _operating_entry(monkeypatch, hmc)
        with pytest.raises(HMCError) as caught:
            await fetch_capacity_report(hmc)

    assert caught.value.status_code == 500


@pytest.mark.asyncio
async def test_capacity_report_raises_when_every_system_is_omitted(mock_hmc) -> None:
    _mock_no_connection_system(mock_hmc)
    _quick_all(mock_hmc, _summary("no connection"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as caught:
            await fetch_capacity_report(hmc)

    assert f"{SYSTEM_NAME!r} ({SYSTEM_UUID}) in State 'no connection'" in str(
        caught.value
    )


@pytest.mark.asyncio
async def test_system_summary_refuses_a_non_operating_system(mock_hmc) -> None:
    """Its entry timed out for a ``no connection`` system (#1501), so it is not read."""
    lpar_feed = _mock_no_connection_system(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as caught:
            await fetch_system_summary(hmc, SYSTEM_UUID)

    assert str(caught.value) == (
        f"Cannot read the summary of managed system {SYSTEM_NAME!r} ({SYSTEM_UUID}): "
        "it is in State 'no connection'; retry when it reconnects"
    )
    assert not lpar_feed.called


@pytest.mark.asyncio
async def test_owning_system_discovery_reports_a_non_operating_system(
    mock_hmc,
) -> None:
    no_connection = _mock_no_connection_system(mock_hmc)
    _quick_all(mock_hmc, _summary("no connection"))
    lpar_uuid = "22222222-2222-2222-2222-222222222222"

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as caught:
            await _discover_owning_system(hmc, lpar_uuid, "lpar-a")

    assert f"1 could not be read: {SYSTEM_UUID} in State 'no connection'" in str(
        caught.value
    )
    assert no_connection.called


@pytest.mark.asyncio
async def test_utilization_survey_records_feed_gaps_for_a_non_operating_system(
    mock_hmc,
) -> None:
    _mock_no_connection_system(mock_hmc)
    system = _capacity_entry("no connection", SYSTEM_UUID, SYSTEM_NAME)

    async with HMCClient(make_config()) as hmc:
        reading = await read_system(hmc, "default", system)

    feed_gaps = [gap for gap in reading.gaps if "State 'no connection'" in gap]
    assert [gap.split(":")[0] for gap in feed_gaps] == [
        "LogicalPartition feed",
        "VirtualIOServer feed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", [resolve_lpar_uuid, resolve_vios_uuid])
async def test_partition_membership_check_names_the_system_state(
    mock_hmc, resolver
) -> None:
    _mock_no_connection_system(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as caught:
            await resolver(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    message = str(caught.value)
    assert repr(SYSTEM_NAME) in message
    assert "State 'no connection'" in message
    assert "omit the selector" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_decommission_refuses_an_untrusted_vios_feed(
    mock_hmc, monkeypatch, dry_run
) -> None:
    _mock_no_connection_system(mock_hmc)
    identity = (SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, "lpar-a", None)
    snapshot = ("lpar-a", 3, "not activated")
    precheck = AsyncMock()
    monkeypatch.setattr(
        decommission_module,
        "_resolve_inventory_identity",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        decommission_module, "_partition_snapshot", AsyncMock(return_value=snapshot)
    )
    monkeypatch.setattr(
        decommission_module, "_inventory_adapters", AsyncMock(return_value=())
    )
    monkeypatch.setattr(
        decommission_module, "authorize_decommission_lpar_ownership_snapshot", precheck
    )

    async with HMCClient(make_config()) as hmc:
        result = await decommission_lpar(hmc, SYSTEM_UUID, "lpar-a", dry_run=dry_run)

    assert result.resource_deleted is False
    assert result.workflow_completed is dry_run
    assert [step.status for step in result.steps] == (
        ["dry_run"] * 4 if dry_run else ["skipped", "error", "skipped", "skipped"]
    )
    message = " ".join(result.warnings)
    assert "Cannot list VIOSes" in message
    assert repr(SYSTEM_NAME) in message
    assert "State 'no connection'" in message
    precheck.assert_not_awaited()
    writes = [
        call.request
        for call in mock_hmc.calls
        if call.request.method != "GET"
        and not call.request.url.path.endswith("/web/Logon")
    ]
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_feeds", "warning"),
    [
        ([httpx.Response(204)], BASELINE_SNAPSHOT_WARNING),
        (
            [
                httpx.Response(
                    200,
                    text='<feed xmlns="http://www.w3.org/2005/Atom">'
                    f"{captured_lpar_entry(LPAR_UUID, 'lpar-a')}</feed>",
                ),
                httpx.Response(204),
            ],
            POST_SNAPSHOT_WARNING,
        ),
    ],
    ids=["baseline", "post"],
)
async def test_template_deployment_warns_for_a_non_operating_system(
    mock_hmc, monkeypatch, snapshot_feeds, warning
) -> None:
    _mock_no_connection_system(mock_hmc)
    mock_hmc.get(LPAR_FEED).mock(side_effect=snapshot_feeds)
    completed = {"Resource": {"Status": "COMPLETED_OK"}}
    stamp = AsyncMock()
    monkeypatch.setattr(
        templates_module, "wait_for_submitted_job", AsyncMock(return_value=completed)
    )
    monkeypatch.setattr(templates_module, "stamp_created_lpar_ownership", stamp)

    async with HMCClient(make_config()) as hmc:
        monkeypatch.setattr(hmc, "deploy_partition_template", AsyncMock())
        result = await deploy_partition_template(
            hmc,
            "draft-uuid",
            SYSTEM_UUID,
            wait=True,
            timeout_seconds=60,
            poll_interval=1,
        )

    assert result["job"] == completed
    assert result["ownership_stamped"] is None
    assert result["warnings"] == [warning]
    stamp.assert_not_awaited()
