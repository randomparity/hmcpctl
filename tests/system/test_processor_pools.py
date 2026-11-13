"""Shared processor pool reads over the captured V10R3 feed (#1975, ADR 0375)."""

from __future__ import annotations

import copy
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from conftest import live_fixture, live_response, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.systems.processor_pools import (
    CONFIGURED_RULE,
    get_shared_processor_pool,
    list_shared_processor_pools,
    project_shared_processor_pool,
)
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.server_tools.systems.processor_pools import (
    hmc_get_shared_processor_pool,
    hmc_list_shared_processor_pools,
)
from hmcpctl.ssh.commands import _parse_lshwres_output
from hmcpctl.xmlutil import parse_feed

FEED_PATH, FEED_RESPONSE = live_response("rest-shared-processor-pool-feed")
SYSTEM_UUID = FEED_PATH.split("/")[-2]
ENTRIES = parse_feed(live_fixture("rest-shared-processor-pool-feed")["body"])
CLI_ROWS = _parse_lshwres_output(live_fixture("cli-procpool")["stdout"])
RESOLVE = "hmcpctl.operations.systems.processor_pools.resolve_system_uuid"


def _entry(pool_id: int) -> dict:
    return copy.deepcopy(
        next(e for e in ENTRIES if e["Resource"]["PoolID"] == str(pool_id))
    )


def _hmc(entries=ENTRIES) -> AsyncMock:
    hmc = AsyncMock()
    hmc.list_shared_processor_pools.return_value = entries
    return hmc


async def _listing(entries=ENTRIES, **kwargs):
    with patch(RESOLVE, AsyncMock(return_value=SYSTEM_UUID)):
        return await list_shared_processor_pools(_hmc(entries), "sys-R1", **kwargs)


async def _get(pool, entries=ENTRIES):
    with patch(RESOLVE, AsyncMock(return_value=SYSTEM_UUID)):
        return await get_shared_processor_pool(_hmc(entries), "sys-R1", pool)


@pytest.mark.asyncio
async def test_client_reads_the_typed_child_feed(mock_hmc):
    route = mock_hmc.get(FEED_PATH).mock(return_value=FEED_RESPONSE)

    async with HMCClient(make_config()) as hmc:
        entries = await hmc.list_shared_processor_pools(SYSTEM_UUID)

    assert len(entries) == 64
    accept = route.calls.last.request.headers["Accept"]
    assert "type=SharedProcessorPool" in accept


@pytest.mark.asyncio
async def test_client_reads_no_content_as_no_slots(mock_hmc):
    mock_hmc.get(FEED_PATH).mock(return_value=httpx.Response(204))

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_shared_processor_pools(SYSTEM_UUID) == []


@pytest.mark.asyncio
async def test_client_refuses_a_non_uuid_system(mock_hmc):
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="system_uuid"):
            await hmc.list_shared_processor_pools("sys-R1/../x")


@pytest.mark.asyncio
async def test_rule_selects_exactly_the_cli_pools_from_the_64_slot_feed():
    listing = await _listing()

    cli_ids = {int(row["shared_proc_pool_id"]) for row in CLI_ROWS}
    assert {pool.pool_id for pool in listing.pools} == cli_ids == {0, 1}
    assert listing.slot_count == 64
    assert listing.unconfigured_omitted == 62
    assert listing.configured_rule == CONFIGURED_RULE
    assert listing.system_uuid == SYSTEM_UUID


@pytest.mark.asyncio
async def test_full_view_keeps_every_slot_with_its_flag():
    listing = await _listing(include_unconfigured=True)

    assert [pool.pool_id for pool in listing.pools] == list(range(64))
    assert listing.unconfigured_omitted == 0
    assert sum(pool.configured for pool in listing.pools) == 2


def test_default_pool_reads_absent_units_as_not_applicable_and_keeps_links():
    pool = project_shared_processor_pool(_entry(0))

    assert pool.name == "DefaultPool"
    assert pool.configured is True
    assert pool.maximum_processing_units is None
    assert pool.reserved_processing_units is None
    assert pool.pending_reserved_processing_units is None
    assert pool.available_processing_units == 0.0
    assert pool.assigned_partition_uuids is not None
    assert len(pool.assigned_partition_uuids) == 1


def test_configured_pool_carries_units_and_reports_no_membership():
    pool = project_shared_processor_pool(_entry(1))

    assert (pool.name, pool.configured) == ("SharedPool01", True)
    assert pool.maximum_processing_units == 20.0
    assert pool.reserved_processing_units == 1.0
    assert pool.pending_reserved_processing_units == 1.0
    assert pool.available_processing_units == 19.0
    assert pool.assigned_partition_uuids is None


def test_unconfigured_placeholder_slot_is_flagged():
    pool = project_shared_processor_pool(_entry(7))

    assert (pool.name, pool.configured) == ("SharedPool07", False)
    assert pool.maximum_processing_units == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MaximumProcessingUnits", "1"),
        ("CurrentReservedProcessingUnits", "0.5"),
        ("PendingReservedProcessingUnits", "0.5"),
        ("PoolName", "batch"),
    ],
)
def test_each_rule_clause_marks_a_slot_configured(field, value):
    entry = _entry(7)
    entry["Resource"][field] = value

    assert project_shared_processor_pool(entry).configured is True


def test_default_pool_is_configured_whatever_its_name_or_units():
    entry = _entry(0)
    entry["Resource"]["PoolName"] = "SharedPool00"

    assert project_shared_processor_pool(entry).configured is True


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", ""])
def test_malformed_unit_refuses_the_pool(value):
    entry = _entry(1)
    entry["Resource"]["MaximumProcessingUnits"] = value

    with pytest.raises(HMCError, match="ID 1 has a malformed MaximumProcessingUnits"):
        project_shared_processor_pool(entry)


@pytest.mark.parametrize("value", ["x", "-1", "1.5", None])
def test_malformed_pool_id_refuses_the_pool(value):
    entry = _entry(1)
    entry["Resource"]["PoolID"] = value

    with pytest.raises(HMCError, match="malformed PoolID"):
        project_shared_processor_pool(entry)


def test_missing_name_or_uuid_refuses_the_pool():
    entry = _entry(1)
    del entry["Resource"]["PoolName"]
    with pytest.raises(HMCError, match="no PoolName"):
        project_shared_processor_pool(entry)
    with pytest.raises(HMCError, match="no UUID"):
        project_shared_processor_pool({"Resource": {}})


@pytest.mark.parametrize("assigned", ["", {}])
def test_present_empty_membership_is_an_empty_tuple(assigned):
    entry = _entry(1)
    entry["Resource"]["AssignedPartitions"] = assigned

    assert project_shared_processor_pool(entry).assigned_partition_uuids == ()


def test_membership_accepts_partition_and_vios_links_in_order():
    entry = _entry(1)
    entry["Resource"]["AssignedPartitions"] = {
        "link": [
            {
                "href": "https://hmc.test:443/rest/api/uom/ManagedSystem/s/LogicalPartition/A1"
            },
            {
                "href": "https://hmc.test:443/rest/api/uom/ManagedSystem/s/VirtualIOServer/B2"
            },
        ]
    }

    assert project_shared_processor_pool(entry).assigned_partition_uuids == ("A1", "B2")


@pytest.mark.parametrize(
    "assigned",
    [
        {"link": {"href": "https://hmc.test:443/rest/api/uom/ManagedSystem/s"}},
        {"link": {"rel": "related"}},
        "text",
    ],
)
def test_membership_refuses_a_link_that_names_no_partition(assigned):
    entry = _entry(1)
    entry["Resource"]["AssignedPartitions"] = assigned

    with pytest.raises(HMCError, match="ID 1"):
        project_shared_processor_pool(entry)


@pytest.mark.asyncio
async def test_repeated_pool_id_refuses_list_and_get():
    entries = [*ENTRIES, _entry(1)]

    with pytest.raises(HMCError, match="repeats PoolID 1"):
        await _listing(entries)
    with pytest.raises(HMCError, match="repeats PoolID 1"):
        await _get(5, entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "pool_id", "configured"),
    [(0, 0, True), (1, 1, True), ("SharedPool01", 1, True), (7, 7, False)],
)
async def test_get_by_id_or_exact_name_reads_every_slot(selector, pool_id, configured):
    result = await _get(selector)

    assert (result.pool.pool_id, result.pool.configured) == (pool_id, configured)
    assert result.configured_rule == CONFIGURED_RULE
    assert result.system_uuid == SYSTEM_UUID


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [99, "sharedpool01", "NoSuchPool"])
async def test_get_unknown_id_or_name_is_not_found(selector):
    with pytest.raises(ResourceNotFoundError, match="List the system's shared"):
        await _get(selector)


@pytest.mark.asyncio
async def test_a_digit_string_selector_names_the_integer_id_route():
    with pytest.raises(ResourceNotFoundError) as caught:
        await _get("0")

    assert "with name '0'" in str(caught.value)
    assert "pass an integer to select by pool ID" in str(caught.value)


@pytest.mark.asyncio
async def test_a_plain_unknown_name_carries_no_id_hint():
    with pytest.raises(ResourceNotFoundError) as caught:
        await _get("NoSuchPool")

    assert "pass an integer" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "error"),
    [
        ("", ValueError),
        (-1, ValueError),
        (True, TypeError),
        (1.0, TypeError),
        (None, TypeError),
    ],
)
async def test_get_refuses_an_invalid_selector_before_reading(selector, error):
    hmc = _hmc()

    with pytest.raises(error, match="pool"):
        await get_shared_processor_pool(hmc, "sys-R1", selector)
    hmc.list_shared_processor_pools.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_refuses_an_ambiguous_name():
    twin = _entry(2)
    twin["Resource"]["PoolName"] = "SharedPool01"
    entries = [e for e in ENTRIES if e["Resource"]["PoolID"] != "2"] + [twin]

    with pytest.raises(ValueError, match="Ambiguous .* pool IDs 1, 2"):
        await _get("SharedPool01", entries)


def _hmc_env(monkeypatch):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def test_list_tool_serializes_the_listing(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get(FEED_PATH).mock(return_value=FEED_RESPONSE)
    resolver = AsyncMock(return_value=SYSTEM_UUID)

    with patch(RESOLVE, new=resolver):
        result = hmc_list_shared_processor_pools("sys-R1")

    resolver.assert_awaited_once_with(ANY, "sys-R1")
    assert result["configured_rule"] == CONFIGURED_RULE
    assert [pool["pool_id"] for pool in result["pools"]] == [0, 1]
    assert result["pools"][0]["maximum_processing_units"] is None


def test_get_tool_serializes_one_pool(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get(FEED_PATH).mock(return_value=FEED_RESPONSE)

    with patch(RESOLVE, AsyncMock(return_value=SYSTEM_UUID)):
        result = hmc_get_shared_processor_pool("sys-R1", "SharedPool01")

    assert result["pool"]["pool_id"] == 1
    assert result["pool"]["assigned_partition_uuids"] is None
