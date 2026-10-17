"""Fleet-wide and system reads take their systems from ``ManagedSystem/quick/All`` (#1501).

The 2026-10-13 capture (#1290) saw the full ``ManagedSystem`` feed, and a
``No Connection`` system's ``ManagedSystem/{uuid}`` entry, time out.
``quick/All`` answered; its captured ``rest-ms-quick-all-no-connection-v11r2``
record lists eleven systems, two of them (``sys-R8``, ``sys-R11``) in
``no connection``. Here the full feed, the name search and a disconnected
system's entry raise read timeouts, and a disconnected system's scoped feeds
answer 204 as captured in #1289.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields, replace

import httpx
import pytest
from conftest import (
    captured_lpar_entry,
    captured_system_entry,
    live_fixture,
    live_response,
    make_config,
)

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.capacity import fetch_capacity_report
from hmcpctl.operations.inventory.composite import fetch_system_summary
from hmcpctl.operations.inventory.logical import read_inventory
from hmcpctl.operations.inventory.utilization import (
    FIGURE_GROUPS,
    _summary_mtms,
    fleet_systems,
    survey_hmc,
)
from hmcpctl.operations.lpar.memory_bound import require_memory_within_system
from hmcpctl.operations.lpar.ownership import _discover_owning_system
from hmcpctl.operations.lpar.plan import _enumerate as plan_enumerate
from hmcpctl.operations.systems.core import get_system, list_systems
from hmcpctl.operations.systems.health import fetch_fleet_health
from hmcpctl.operations.vios.core import _resolve_vios_backup_system_name
from hmcpctl.xmlutil import mtms_parts, parse_feed

QUICK_ALL = "rest-ms-quick-all-no-connection-v11r2"
SUMMARIES = json.loads(live_fixture(QUICK_ALL)["body"])
UUIDS = {summary["SystemName"]: summary["UUID"] for summary in SUMMARIES}
DISCONNECTED = {UUIDS["sys-R11"], UUIDS["sys-R8"]}
OPERATING = set(UUIDS.values()) - DISCONNECTED
LPAR = "11111111-0000-4000-8000-00000000000a"
STALLED = httpx.ReadTimeout("read timed out")
SCOPED = re.compile(
    r"/ManagedSystem/([0-9a-f-]{36})/(LogicalPartition|VirtualIOServer)$"
)
EVERY_FIGURE = sum(len(fields(kind)) for kind in FIGURE_GROUPS.values())
ENTRY = re.compile(r"/ManagedSystem/([0-9a-f-]{36})$")


def _feed(*entries: str) -> str:
    return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"


class _Fleet:
    """The captured fleet; ``owner`` is the system whose partition feed holds LPAR."""

    def __init__(self, mock_hmc, owner: str | None = None) -> None:
        self.owner = owner
        self.full_feed = mock_hmc.get(
            url__regex=r"/rest/api/uom/ManagedSystem/?$"
        ).mock(side_effect=STALLED)
        self.search = mock_hmc.get(
            url__regex=r"/rest/api/uom/ManagedSystem/search/.*"
        ).mock(side_effect=STALLED)
        self.entries = mock_hmc.get(url__regex=ENTRY.pattern).mock(
            side_effect=self._entry
        )
        self.scoped = mock_hmc.get(url__regex=SCOPED.pattern).mock(
            side_effect=self._scoped
        )
        path, response = live_response(QUICK_ALL)
        mock_hmc.get(path).mock(return_value=response)

    @staticmethod
    def _entry(request: httpx.Request) -> httpx.Response:
        uuid = ENTRY.search(request.url.path).group(1)
        if uuid in DISCONNECTED:
            raise STALLED
        name = next(name for name, value in UUIDS.items() if value == uuid)
        entry = captured_system_entry(uuid, name).replace(
            "<entry>", '<entry xmlns="http://www.w3.org/2005/Atom">', 1
        )
        return httpx.Response(200, text=entry)

    def _scoped(self, request: httpx.Request) -> httpx.Response:
        uuid, collection = SCOPED.search(request.url.path).groups()
        if uuid in DISCONNECTED:
            return httpx.Response(204)
        owned = collection == "LogicalPartition" and uuid == self.owner
        entries = [captured_lpar_entry(LPAR, "lpar-a", "running")] if owned else []
        return httpx.Response(200, text=_feed(*entries))

    def read(self, route) -> set[str]:
        return {re.search(r"[0-9a-f-]{36}", c.request.url.path)[0] for c in route.calls}

    def assert_disconnected_entries_unread(self) -> None:
        assert not self.full_feed.called and not self.search.called
        assert not self.read(self.entries) & DISCONNECTED


@pytest.mark.asyncio
async def test_fleet_health_reports_the_disconnected_systems(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        result = await fetch_fleet_health(hmc)

    assert sorted(system["name"] for system in result.systems) == ["sys-R11", "sys-R8"]
    assert {system["state"] for system in result.systems} == {"no connection"}
    assert len(result.warnings) == 4
    assert all("State 'no connection'" in warning for warning in result.warnings)
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_owning_system_discovery_skips_the_disconnected_systems(mock_hmc):
    fleet = _Fleet(mock_hmc, owner=UUIDS["sys-R3"])

    async with HMCClient(make_config()) as hmc:
        found = await _discover_owning_system(hmc, LPAR, "lpar-a")

    assert found == (UUIDS["sys-R3"], "sys-R3")
    fleet.assert_disconnected_entries_unread()


def _quick_all_rows(mock_hmc, rows: list[object]) -> None:
    """Replace the captured quick/All answer; respx keeps the later registration."""
    mock_hmc.get(live_response(QUICK_ALL)[0]).mock(
        return_value=httpx.Response(200, json=rows)
    )


@pytest.mark.asyncio
async def test_discovery_still_reads_a_standby_frame(mock_hmc):
    """ADR 0197: a frame in Standby can still answer its partition feed."""
    _Fleet(mock_hmc, owner=UUIDS["sys-R3"])
    _quick_all_rows(
        mock_hmc,
        [
            {**summary, "State": "standby"}
            if summary["SystemName"] == "sys-R3"
            else summary
            for summary in SUMMARIES
        ],
    )

    async with HMCClient(make_config()) as hmc:
        found = await _discover_owning_system(hmc, LPAR, "lpar-a")

    assert found == (UUIDS["sys-R3"], "sys-R3")


@pytest.mark.asyncio
async def test_discovery_counts_a_row_without_a_valid_uuid_as_unusable(mock_hmc):
    fleet = _Fleet(mock_hmc, owner=UUIDS["sys-R3"])
    rows = [{"UUID": "not-a-uuid", "SystemName": "sys-R0", "State": "operating"}]
    _quick_all_rows(mock_hmc, [*rows, *SUMMARIES])

    async with HMCClient(make_config()) as hmc:
        found = await _discover_owning_system(hmc, LPAR, "lpar-a")

    assert found == (UUIDS["sys-R3"], "sys-R3")
    assert all("not-a-uuid" not in call.request.url.path for call in fleet.scoped.calls)


@pytest.mark.asyncio
async def test_an_undiscovered_partition_names_the_disconnected_systems(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as raised:
            await _discover_owning_system(hmc, LPAR, "lpar-a")

    message = str(raised.value)
    assert "2 could not be read" in message
    for uuid in DISCONNECTED:
        assert f"{uuid} in State 'no connection'" in message
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_capacity_reads_only_the_operating_systems(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        report = await fetch_capacity_report(hmc)

    assert {item.system_uuid for item in report} == OPERATING
    assert fleet.read(fleet.entries) == OPERATING
    assert not fleet.read(fleet.scoped) & DISCONNECTED
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_utilization_reports_the_disconnected_systems_unread(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        readings = await survey_hmc(hmc, "default")

    unread = {r.name: r for r in readings if r.state != "operating"}
    assert len(readings) == len(SUMMARIES)
    assert sorted(unread) == ["sys-R11", "sys-R8"]
    for reading in unread.values():
        assert reading.state == "no connection"
        assert reading.gaps == (
            "ManagedSystem: not read, because the system is in State 'no connection'",
        )
        assert reading.unknown_figures == EVERY_FIGURE
    assert (unread["sys-R8"].machine_type, unread["sys-R8"].model) == ("9824", "42A")
    assert unread["sys-R8"].firmware == "RB1110_fw1110.20 (130)"
    assert fleet.read(fleet.entries) == OPERATING
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_an_unread_frame_another_hmc_reads_is_counted_once(mock_hmc):
    _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        readings = await survey_hmc(hmc, "hmc-a")

    unread = next(reading for reading in readings if reading.name == "sys-R8")
    read_elsewhere = replace(
        unread,
        profile="hmc-b",
        state="operating",
        gaps=(),
        memory=replace(unread.memory, configurable=1024),
    )

    (system,) = fleet_systems([unread, read_elsewhere])

    assert system.profiles == ("hmc-a", "hmc-b")
    assert system.reading.profile == "hmc-b"


def test_quick_all_mtms_names_the_machine_its_full_entry_names():
    """quick/All's ``MTMS`` and the full entry parse to one (type, model, serial) (#1534).

    ADR 0184 counts a frame once by these three values whether it was read or only
    listed. Both records come from one read-only 2026-10-14 V10R3 window. Tokenizing
    hides the serial, so the evidence that the serials agree is the scripted comparison
    of the raw capture that the fixtures README records.
    """
    entry = parse_feed(live_fixture("rest-ms-entry-mtms-pair")["body"])[0]
    summaries = json.loads(live_fixture("rest-ms-quick-all-mtms-pair")["body"])
    (summary,) = [s for s in summaries if s["UUID"].lower() == entry["UUID"].lower()]

    full = mtms_parts(entry["Resource"])

    assert full == ("8375", "42A", "<REDACTED-SERIAL>")
    assert _summary_mtms(summary) == full


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "gap"),
    [
        (
            {"UUID": "not-a-uuid", "SystemName": "sys-R0", "State": "operating"},
            "ManagedSystem: not read, because quick/All reports no valid UUID",
        ),
        ("sys-R0", "ManagedSystem: not read, because quick/All reports no State"),
    ],
)
async def test_an_unread_row_names_why_it_was_not_read(mock_hmc, row, gap):
    _quick_all_rows(mock_hmc, [row])

    async with HMCClient(make_config()) as hmc:
        (reading,) = await survey_hmc(hmc, "default")

    assert reading.gaps == (gap,)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [fetch_capacity_report, survey_hmc])
async def test_a_listed_system_without_an_entry_fails_the_read(mock_hmc, operation):
    path, response = live_response(QUICK_ALL)
    mock_hmc.get(path).mock(return_value=response)
    mock_hmc.get(url__regex=ENTRY.pattern).mock(return_value=httpx.Response(204))
    arguments = ("default",) if operation is survey_hmc else ()

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="listed by quick/All has no entry"):
            await operation(hmc, *arguments)


# --- system listings and full-entry readers (#1501 part 4) ---------------------------


def _sparse(name: str) -> dict:
    return {
        "UUID": UUIDS[name],
        "title": None,
        "link": None,
        "ResourceType": "ManagedSystem",
        "Resource": {"SystemName": name, "State": "no connection", "StateDetail": ""},
    }


@pytest.mark.asyncio
async def test_list_systems_gives_disconnected_systems_sparse_entries(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        entries = await list_systems(hmc)

    by_uuid = {entry["UUID"]: entry for entry in entries}
    assert len(entries) == len(SUMMARIES)
    assert by_uuid[UUIDS["sys-R8"]] == _sparse("sys-R8")
    assert by_uuid[UUIDS["sys-R11"]] == _sparse("sys-R11")
    assert by_uuid[UUIDS["sys-R1"]]["Resource"]["SystemName"] == "sys-R1"
    assert "AssociatedSystemMemoryConfiguration" in by_uuid[UUIDS["sys-R1"]]["Resource"]
    assert fleet.read(fleet.entries) == OPERATING
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_list_systems_filters_state_without_reading_entries(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        entries = await list_systems(hmc, "no connection")

    assert sorted(entry["Resource"]["SystemName"] for entry in entries) == [
        "sys-R11",
        "sys-R8",
    ]
    assert not fleet.entries.called
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "full"),
    [("sys-R8", False), (UUIDS["sys-R8"].upper(), False), ("sys-R1", True)],
)
async def test_get_system_selects_from_quick_all(mock_hmc, selector, full):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        entry = await get_system(hmc, selector)

    if full:
        assert entry["Resource"]["SystemName"] == "sys-R1"
        assert "MachineTypeModelAndSerialNumber" in entry["Resource"]
    else:
        assert entry == _sparse("sys-R8")
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "reading"),
    [
        (lambda hmc, uuid: fetch_system_summary(hmc, uuid), "the summary"),
        (_resolve_vios_backup_system_name, "the MTMS"),
    ],
    ids=["summary", "vios-backup"],
)
async def test_a_full_entry_reader_refuses_a_disconnected_system(
    mock_hmc, read, reading
):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await read(hmc, UUIDS["sys-R8"])

    assert str(raised.value) == (
        f"Cannot read {reading} of managed system 'sys-R8' ({UUIDS['sys-R8']}): "
        "it is in State 'no connection'; retry when it reconnects"
    )
    assert not fleet.scoped.called
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_the_memory_bound_is_unchecked_for_a_disconnected_system(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await require_memory_within_system(
            hmc, UUIDS["sys-R8"], LparResources(desired_memory=10**9)
        )
        with pytest.raises(ValueError, match="exceeds the managed system"):
            await require_memory_within_system(
                hmc, UUIDS["sys-R1"], LparResources(desired_memory=10**9)
            )

    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_logical_inventory_enumerates_from_quick_all(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        page = await read_inventory(
            hmc,
            connection="lab",
            admit=lambda _tool, _selector: None,
            systems=None,
            lpar_state=None,
            owner=None,
            limit=50,
            cursor=None,
        )

    assert {system.uuid for system in page.systems} == set(UUIDS.values())
    disconnected = [s for s in page.systems if s.uuid in DISCONNECTED]
    assert {s.state for s in disconnected} == {"no connection"}
    assert {s.sources.capacity.status for s in disconnected} == {"unavailable"}
    assert fleet.read(fleet.entries) == OPERATING
    fleet.assert_disconnected_entries_unread()


@pytest.mark.asyncio
async def test_plan_enumeration_reads_only_operating_entries(mock_hmc):
    fleet = _Fleet(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        candidates, blocker, truncated = await plan_enumerate(
            hmc, lambda _tool, _system, _vios: None
        )

    assert (blocker, truncated) == (None, False)
    states = {c.uuid: c.entry["Resource"]["State"] for c in candidates}
    assert {states[uuid] for uuid in DISCONNECTED} == {"no connection"}
    assert fleet.read(fleet.entries) == OPERATING
    fleet.assert_disconnected_entries_unread()
