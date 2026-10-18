"""The logical inventory behind hmc_inventory (#1220, ADR 0196)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.operations.inventory.logical import (
    CAPACITY_TOOL,
    DELEGATED_TOOLS,
    OWNERSHIP_TOOL,
    PARTITIONS_TOOL,
    SYSTEMS_TOOL,
    InventoryPage,
    encode_cursor,
    read_inventory,
)


def _uuid(system: int, partition: int = 0) -> str:
    return f"{system:08x}-0000-4000-8000-{partition:012x}"


def _system(number: int, name: str | None = None, *, capacity: bool = True) -> dict:
    resource: dict[str, Any] = {
        "SystemName": name or f"sys{number}",
        "State": "operating",
    }
    if capacity:
        resource["AssociatedSystemMemoryConfiguration"] = {
            "ConfigurableSystemMemory": "1048576",
            "CurrentAvailableSystemMemory": "524288",
        }
        resource["AssociatedSystemProcessorConfiguration"] = {
            "ConfigurableSystemProcessorUnits": "32",
            "CurrentAvailableSystemProcessorUnits": "12.5",
        }
    return {"UUID": _uuid(number), "Resource": resource}


def _lpar(
    system: int,
    number: int,
    name: str | None = None,
    state: str = "running",
    *,
    owner: str | None = None,
    memory: str | None = "4096",
) -> dict:
    resource: dict[str, Any] = {
        "PartitionName": name or f"lpar{number}",
        "PartitionState": state,
        "PartitionID": str(number),
        "PartitionType": "AIX/Linux",
        "ResourceMonitoringControlState": "active",
        "PartitionProcessorConfiguration": {
            "HasDedicatedProcessors": "false",
            "CurrentSharedProcessorConfiguration": {"CurrentProcessingUnits": "0.5"},
        },
    }
    if memory is not None:
        resource["PartitionMemoryConfiguration"] = {"CurrentMemory": memory}
    if owner is not None:
        resource["Description"] = f"[hmcpctl owner:{owner} created:2026-10-01]"
    return {"UUID": _uuid(system, number), "Resource": resource}


class FakeHMC:
    """The reads the inventory makes, with a log of every call.

    Systems are listed from quick/All; *failures* maps the 1-based number of a
    quick/All read to the error it raises, *missing* names systems it omits, and
    *extra* adds summaries.
    """

    def __init__(
        self,
        systems: list[dict],
        partitions: dict[str, list[dict] | Exception] | None = None,
        *,
        failures: dict[int, Exception] | None = None,
        missing: frozenset[str] = frozenset(),
        extra: tuple[dict, ...] = (),
        systems_error: Exception | None = None,
        entry_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.systems = systems
        self.partitions = partitions or {}
        self.failures = failures or {}
        self.missing = missing
        self.extra = extra
        self.systems_error = systems_error
        self.entry_errors = entry_errors or {}
        self.calls: list[tuple[str, str | None]] = []

    async def list_system_summaries(self) -> list[dict]:
        self.calls.append(("list_system_summaries", None))
        reads = self.calls.count(("list_system_summaries", None))
        failure = self.systems_error or self.failures.get(reads)
        if failure is not None:
            raise failure
        return [
            {
                "UUID": entry["UUID"],
                **{k: entry["Resource"][k] for k in ("SystemName", "State")},
            }
            for entry in self.systems
            if entry["Resource"]["SystemName"] not in self.missing
        ] + list(self.extra)

    async def get_managed_system(self, uuid: str) -> dict | None:
        self.calls.append(("get_managed_system", uuid))
        if uuid in self.entry_errors:
            raise self.entry_errors[uuid]
        return next((entry for entry in self.systems if entry["UUID"] == uuid), None)

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(("list_logical_partitions", uuid))
        found = self.partitions.get(uuid, [])
        if isinstance(found, Exception):
            raise found
        return found


class Admit:
    """An admit stub denying the (tool, selector) pairs it is given."""

    def __init__(self, *denied: tuple[str, str | None]) -> None:
        self.denied = set(denied)
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, tool: str, selector: str | None) -> str | None:
        self.calls.append((tool, selector))
        if (tool, selector) in self.denied or (tool, "*") in self.denied:
            return f"{tool} is not permitted on {selector}"
        return None


def _read(hmc: FakeHMC, admit: Admit | None = None, **kwargs: Any) -> InventoryPage:
    kwargs.setdefault("connection", "lab")
    kwargs.setdefault("systems", None)
    kwargs.setdefault("lpar_state", None)
    kwargs.setdefault("owner", None)
    kwargs.setdefault("limit", 50)
    kwargs.setdefault("cursor", None)
    return asyncio.run(read_inventory(hmc, admit=admit or Admit(), **kwargs))


def test_delegated_tools_are_the_four_adr_0196_names():
    assert DELEGATED_TOOLS == (
        SYSTEMS_TOOL,
        PARTITIONS_TOOL,
        CAPACITY_TOOL,
        OWNERSHIP_TOOL,
    )


def test_permitting_read_returns_scoped_systems_and_partitions():
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1, owner="agent-a")]})
    page = _read(hmc)
    (system,) = page.systems
    assert system.id == f"lab/{_uuid(1)}"
    assert (system.name, system.state) == ("sys1", "operating")
    assert (system.total_memory_mib, system.free_memory_mib) == (1048576, 524288)
    assert (system.total_proc_units, system.free_proc_units) == (32.0, 12.5)
    assert {s.status for s in vars(system.sources).values()} == {"ok"}
    assert page.systems_source is not None and page.systems_source.status == "ok"
    (partition,) = page.partitions
    assert partition.id == f"lab/{_uuid(1)}/{_uuid(1, 1)}"
    assert partition.system_id == system.id
    assert (partition.name, partition.state, partition.rmc_state) == (
        "lpar1",
        "running",
        "active",
    )
    assert (partition.partition_id, partition.current_memory_mib) == (1, 4096)
    assert (partition.current_proc_units, partition.has_dedicated_processors) == (
        0.5,
        False,
    )
    assert (partition.owned, partition.owner) == (True, "agent-a")
    assert (page.limit, page.truncated, page.next_cursor) == (50, False, None)
    assert (page.systems_limit, page.systems_truncated) == (16, False)


def test_duplicate_partition_names_on_two_systems_get_distinct_ids():
    hmc = FakeHMC(
        [_system(1), _system(2)],
        {_uuid(1): [_lpar(1, 1, "web")], _uuid(2): [_lpar(2, 1, "web")]},
    )
    ids = {p.id for p in _read(hmc).partitions}
    assert len(ids) == 2


def test_same_system_through_two_connections_gets_distinct_ids():
    def ids(connection: str) -> set[str | None]:
        hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1)]})
        page = _read(hmc, connection=connection)
        return {page.systems[0].id, page.partitions[0].id}

    assert ids("lab").isdisjoint(ids("prod"))


def test_missing_capacity_reads_null_and_unavailable():
    hmc = FakeHMC([_system(1, capacity=False)])
    (system,) = _read(hmc).systems
    assert system.total_memory_mib is None and system.free_proc_units is None
    assert system.sources.capacity.status == "unavailable"
    assert "ConfigurableSystemMemory" in (system.sources.capacity.detail or "")


def test_missing_partition_figures_read_null():
    entry = _lpar(1, 1, memory=None)
    del entry["Resource"]["PartitionID"]
    hmc = FakeHMC([_system(1)], {_uuid(1): [entry]})
    (partition,) = _read(hmc).partitions
    assert partition.current_memory_mib is None
    assert partition.partition_id is None


def test_empty_system_is_ok_with_no_partitions():
    page = _read(FakeHMC([_system(1)], {_uuid(1): []}))
    assert page.partitions == []
    assert page.systems[0].sources.partitions.status == "ok"


def test_partition_feed_error_is_unavailable_for_that_system_only():
    hmc = FakeHMC(
        [_system(1), _system(2)],
        {_uuid(1): HMCError("feed refused", 500), _uuid(2): [_lpar(2, 1)]},
    )
    page = _read(hmc)
    first, second = page.systems
    assert first.sources.partitions.status == "unavailable"
    assert "feed refused" in (first.sources.partitions.detail or "")
    assert first.sources.ownership.status == "unavailable"
    assert second.sources.partitions.status == "ok"
    assert [p.uuid for p in page.partitions] == [_uuid(2, 1)]


def test_systems_feed_error_is_unavailable():
    page = _read(FakeHMC([], systems_error=HMCError("timeout", 504)))
    assert page.systems_source is not None
    assert page.systems_source.status == "unavailable"
    assert page.systems == [] and page.partitions == []


def test_denied_enumeration_reads_nothing():
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1)]})
    admit = Admit((SYSTEMS_TOOL, None))
    page = _read(hmc, admit)
    assert hmc.calls == []
    assert page.systems_source is not None
    assert page.systems_source.status == "denied"
    assert "Pass systems selectors" in (page.systems_source.detail or "")
    assert page.systems == [] and page.partitions == []
    assert admit.calls == [(SYSTEMS_TOOL, None)]


def test_denied_selector_is_reported_without_a_read():
    hmc = FakeHMC([_system(1), _system(2)], {_uuid(2): [_lpar(2, 1)]})
    admit = Admit((PARTITIONS_TOOL, "sys1"))
    page = _read(hmc, admit, systems=["sys1", "sys2"])
    assert page.systems_source is None
    denied = next(s for s in page.systems if s.selector == "sys1")
    assert (denied.id, denied.uuid, denied.name, denied.state) == (
        None,
        None,
        None,
        None,
    )
    assert denied.sources.partitions.status == "denied"
    assert hmc.calls.count(("list_system_summaries", None)) == 1
    assert [p.uuid for p in page.partitions] == [_uuid(2, 1)]


@pytest.mark.parametrize(
    "extra",
    [(), tuple({"UUID": _uuid(n), "SystemName": "dup"} for n in (8, 9))],
    ids=["not-found", "ambiguous"],
)
def test_unmatched_selector_is_unavailable(extra):
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1)]}, extra=extra)
    page = _read(hmc, systems=["dup", "sys1"])
    unmatched = next(s for s in page.systems if s.selector == "dup")
    assert unmatched.uuid is None
    assert unmatched.sources.partitions.status == "unavailable"
    assert [p.uuid for p in page.partitions] == [_uuid(1, 1)]


def test_selectors_collapse_and_uuid_selectors_read_by_uuid():
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1)]})
    page = _read(hmc, systems=["sys1", _uuid(1), "sys1"])
    assert len(page.systems) == 1
    assert page.systems[0].selector == "sys1"
    assert hmc.calls.count(("get_managed_system", _uuid(1))) == 1
    assert len(page.partitions) == 1


def test_capacity_denied_nulls_figures():
    admit = Admit((CAPACITY_TOOL, None))
    (system,) = _read(FakeHMC([_system(1)]), admit).systems
    assert system.total_memory_mib is None
    assert system.sources.capacity.status == "denied"
    assert admit.calls.count((CAPACITY_TOOL, None)) == 1


def test_ownership_denied_nulls_owner_and_drops_owner_filtered_partitions():
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1, owner="agent-a")]})
    admit = Admit((OWNERSHIP_TOOL, "*"))
    page = _read(hmc, admit)
    assert page.systems[0].sources.ownership.status == "denied"
    assert (page.partitions[0].owned, page.partitions[0].owner) == (None, None)
    assert _read(hmc, admit, owner="agent-a").partitions == []


def test_ownership_follows_partitions_when_partitions_not_ok():
    hmc = FakeHMC([_system(1)], {_uuid(1): [_lpar(1, 1)]})
    page = _read(hmc, Admit((PARTITIONS_TOOL, "*")))
    sources = page.systems[0].sources
    assert sources.partitions.status == "denied"
    assert sources.ownership.status == "denied"
    assert PARTITIONS_TOOL in (sources.ownership.detail or "")
    assert ("list_logical_partitions", _uuid(1)) not in hmc.calls


def test_state_and_owner_filters_apply_before_paging():
    lpars = [
        _lpar(1, 1, state="running", owner="a"),
        _lpar(1, 2, state="not activated", owner="a"),
        _lpar(1, 3, state="running", owner="b"),
        _lpar(1, 4, state="running"),
    ]
    hmc = FakeHMC([_system(1)], {_uuid(1): lpars})
    page = _read(hmc, lpar_state="running", owner="a", limit=1)
    assert [p.uuid for p in page.partitions] == [_uuid(1, 1)]
    assert page.truncated is False


def test_cursor_walk_returns_each_partition_once():
    systems = [_system(n) for n in (3, 1, 2)]
    hmc = FakeHMC(
        systems,
        {_uuid(n): [_lpar(n, m) for m in range(150, 0, -1)] for n in (1, 2, 3)},
    )
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = _read(hmc, limit=200, cursor=cursor)
        pages += 1
        seen.extend(p.uuid for p in page.partitions)
        cursor = page.next_cursor
        assert page.truncated is (cursor is not None)
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == 450 and len(set(seen)) == 450
    assert seen == sorted(seen)


def test_twenty_systems_page_as_sixteen_then_four():
    hmc = FakeHMC([_system(n) for n in range(1, 21)])
    first = _read(hmc)
    assert len(first.systems) == 16
    assert first.systems_truncated is True and first.truncated is True
    second = _read(hmc, cursor=first.next_cursor)
    assert len(second.systems) == 4
    assert second.systems_truncated is False and second.next_cursor is None


def test_transport_error_ends_the_page_with_a_cursor():
    hmc = FakeHMC(
        [_system(n) for n in (1, 2, 3)],
        {_uuid(2): HMCTransportError("timed out")},
    )
    page = _read(hmc)
    assert [s.uuid for s in page.systems] == [_uuid(1), _uuid(2)]
    assert page.systems[1].sources.partitions.status == "unavailable"
    assert ("list_logical_partitions", _uuid(3)) not in hmc.calls
    assert page.next_cursor == encode_cursor(_uuid(3), None)


def test_a_partition_stall_skips_that_systems_entry_read():
    hmc = FakeHMC(
        [_system(n) for n in (1, 2)], {_uuid(1): HMCTransportError("timed out")}
    )
    page = _read(hmc)
    assert ("get_managed_system", _uuid(1)) not in hmc.calls
    assert page.systems[0].sources.capacity.status == "unavailable"


def test_an_entry_stall_ends_the_page_with_a_cursor():
    hmc = FakeHMC(
        [_system(n) for n in (1, 2, 3)],
        entry_errors={_uuid(2): HMCTransportError("timed out")},
    )
    page = _read(hmc)
    assert [s.uuid for s in page.systems] == [_uuid(1), _uuid(2)]
    assert page.systems[1].sources.capacity.status == "unavailable"
    assert ("list_logical_partitions", _uuid(3)) not in hmc.calls
    assert page.next_cursor == encode_cursor(_uuid(3), None)


def test_a_non_operating_system_names_its_state_for_capacity():
    standby = _system(2)
    standby["Resource"]["State"] = "standby"
    hmc = FakeHMC([_system(1), standby])
    page = _read(hmc)
    capacity = page.systems[1].sources.capacity
    assert capacity.status == "unavailable"
    assert capacity.detail == f"managed system {_uuid(2)} is in State 'standby'"
    assert ("get_managed_system", _uuid(2)) not in hmc.calls


def test_transport_error_stops_selector_resolution():
    hmc = FakeHMC(
        [_system(1), _system(2)],
        failures={1: HMCTransportError("timed out")},
    )
    page = _read(hmc, systems=["sys1", "sys2"])
    assert hmc.calls.count(("list_system_summaries", None)) == 1
    assert {s.sources.partitions.status for s in page.systems} == {"unavailable"}
    assert page.partitions == []


def test_failed_summary_read_is_unavailable_and_reads_nothing_else():
    hmc = FakeHMC([_system(1)], systems_error=HMCError("HTTP 500", status_code=500))
    page = _read(hmc)
    assert page.systems_source is not None
    assert page.systems_source.status == "unavailable"
    assert hmc.calls == [("list_system_summaries", None)]
    assert page.systems == []


def test_stalled_feed_does_not_suggest_selectors():
    hmc = FakeHMC([_system(1)], systems_error=HMCTransportError("timed out"))
    source = _read(hmc).systems_source
    assert source is not None and source.status == "unavailable"
    assert "Pass" not in (source.detail or "")


def test_resolution_stall_reads_no_partitions_on_that_page():
    hmc = FakeHMC(
        [_system(1), _system(2)],
        failures={2: HMCTransportError("timed out")},
    )
    page = _read(hmc, systems=["sys1", "sys2"])
    assert not [
        call
        for call in hmc.calls
        if call[0] in {"list_logical_partitions", "get_managed_system"}
    ]
    statuses = {s.selector: s.sources.partitions for s in page.systems}
    assert {status.status for status in statuses.values()} == {"unavailable"}
    assert "timed out" in (statuses["sys1"].detail or "")
    resolved = next(s for s in page.systems if s.selector == "sys1")
    assert resolved.total_memory_mib is None
    assert resolved.sources.capacity.status == "unavailable"
    # Every system on the page is reported unavailable, so the page is final.
    assert (page.truncated, page.next_cursor) == (False, None)


@pytest.mark.parametrize(
    "failure", [HMCError("gone"), HMCTransportError("timed out"), None]
)
def test_selector_failing_on_a_cursor_page_is_reported(failure):
    hmc = FakeHMC(
        [_system(1), _system(2)],
        {_uuid(2): [_lpar(2, 1)]},
        failures={} if failure is None else {2: failure},
        missing=frozenset() if failure is not None else frozenset({"sys2"}),
    )
    later = _read(hmc, systems=["sys1", "sys2"], cursor=encode_cursor(_uuid(1), None))
    statuses = {s.selector: s.sources.partitions.status for s in later.systems}
    assert statuses["sys2"] == "unavailable"


@pytest.mark.parametrize(
    "arguments",
    [
        {"systems": [f"s{n}" for n in range(17)]},
        {"systems": []},
        {"systems": [""]},
        {"limit": 0},
        {"limit": 201},
        {"owner": ""},
        {"owner": "x" * 65},
    ],
)
def test_input_bounds_are_tool_errors(arguments):
    with pytest.raises(ValueError, match=next(iter(arguments))):
        _read(FakeHMC([]), **arguments)


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",
        base64.urlsafe_b64encode(b'["x", null]').decode(),
        base64.urlsafe_b64encode(b'{"a": 1}').decode(),
        encode_cursor(_uuid(1), "not-a-uuid"),
        "A" * 257,
    ],
)
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError, match="invalid_cursor"):
        _read(FakeHMC([]), cursor=cursor)


def test_detail_is_capped():
    hmc = FakeHMC([_system(1)], {_uuid(1): HMCError("x" * 2000, 500)})
    detail = _read(hmc).systems[0].sources.partitions.detail
    assert detail is not None and len(detail) == 500


def test_denied_selectors_appear_on_the_first_page_only():
    hmc = FakeHMC([_system(n) for n in range(1, 4)], {})
    admit = Admit((PARTITIONS_TOOL, "nope"))
    later = _read(
        hmc,
        admit,
        systems=["nope", "sys1", "sys2"],
        cursor=encode_cursor(_uuid(2), None),
    )
    assert [s.selector for s in later.systems] == ["sys2"]


def test_unparseable_figures_read_null_not_zero():
    entry = _lpar(1, 1, memory="lots")
    entry["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]["CurrentProcessingUnits"] = "n/a"
    hmc = FakeHMC([_system(1)], {_uuid(1): [entry]})
    (partition,) = _read(hmc).partitions
    assert partition.current_memory_mib is None
    assert partition.current_proc_units is None


def test_page_filled_at_a_system_boundary_resumes_at_the_next_system():
    hmc = FakeHMC(
        [_system(1), _system(2)],
        {_uuid(1): [_lpar(1, 1)], _uuid(2): [_lpar(2, 1)]},
    )
    first = _read(hmc, limit=1)
    assert first.next_cursor == encode_cursor(_uuid(2), None)
    assert ("list_logical_partitions", _uuid(2)) not in hmc.calls
    second = _read(hmc, limit=1, cursor=first.next_cursor)
    assert [p.uuid for p in second.partitions] == [_uuid(2, 1)]
    assert second.next_cursor is None
