"""Tests for presentation-neutral managed-system read operations."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hmcpctl.operations.lpar import inventory as lpar_inventory
from hmcpctl.operations.lpar.inventory import list_lpars
from hmcpctl.operations.systems.core import get_system, list_systems

OPERATING = "12345678-1234-1234-1234-123456789abc"
STANDBY = "12345678-1234-1234-1234-123456789abd"
SUMMARIES = [
    {"UUID": OPERATING, "SystemName": "frame-1", "State": "operating"},
    {"UUID": STANDBY, "SystemName": "frame-2", "State": "standby", "StateDetail": ""},
]


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = SUMMARIES
    hmc.get_managed_system.return_value = {"UUID": OPERATING, "Resource": {}}
    return hmc


@pytest.mark.asyncio
async def test_list_systems_reads_only_operating_entries() -> None:
    hmc = _hmc()

    result = await list_systems(hmc)

    hmc.get_managed_system.assert_awaited_once_with(OPERATING)
    hmc.search_uom.assert_not_awaited()
    assert result == [
        {"UUID": OPERATING, "Resource": {}},
        {
            "UUID": STANDBY,
            "title": None,
            "link": None,
            "ResourceType": "ManagedSystem",
            "Resource": {
                "SystemName": "frame-2",
                "State": "standby",
                "StateDetail": "",
            },
        },
    ]


@pytest.mark.asyncio
async def test_list_systems_filters_state_locally() -> None:
    hmc = _hmc()

    result = await list_systems(hmc, "standby")

    hmc.get_managed_system.assert_not_awaited()
    hmc.search_uom.assert_not_awaited()
    assert [entry["UUID"] for entry in result] == [STANDBY]


@pytest.mark.asyncio
async def test_get_system_selects_by_name_or_uuid_from_quick_all() -> None:
    hmc = _hmc()

    assert await get_system(hmc, "frame-1") == {"UUID": OPERATING, "Resource": {}}
    assert await get_system(hmc, OPERATING.upper()) == {
        "UUID": OPERATING,
        "Resource": {},
    }
    assert (await get_system(hmc, "frame-2"))["Resource"]["State"] == "standby"
    assert await get_system(hmc, "frame-9") is None

    assert hmc.get_managed_system.await_count == 2
    hmc.find_system_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_lpar_inventory_composes_system_and_state_filters(monkeypatch) -> None:
    hmc = AsyncMock()
    hmc.list_logical_partitions.return_value = [
        {"UUID": "one", "Resource": {"PartitionState": "running"}},
        {"UUID": "two", "Resource": {"PartitionState": "not activated"}},
    ]
    resolve_system = AsyncMock(return_value="system-uuid")
    monkeypatch.setattr(lpar_inventory, "resolve_system_uuid", resolve_system)

    result = await list_lpars(hmc, "system-1", "running")

    resolve_system.assert_awaited_once_with(hmc, "system-1")
    hmc.list_logical_partitions.assert_awaited_once_with("system-uuid")
    hmc.search_uom.assert_not_awaited()
    assert result.entries == [
        {"UUID": "one", "Resource": {"PartitionState": "running"}}
    ]
    assert result.unreadable_systems == []
