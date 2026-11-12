"""Tests for the presentation-neutral fleet health operation."""

from __future__ import annotations

import asyncio
from collections import Counter
from unittest.mock import AsyncMock

import pytest
from conftest import live_fixture

from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.systems import health as operations_health
from hmcpctl.operations.systems.health import FleetHealthResult
from hmcpctl.operations.systems.health import fetch_fleet_health as fleet_health
from hmcpctl.xmlutil import parse_feed


def _entry(uuid: object, **resource: object) -> dict:
    return {"UUID": uuid, "Resource": resource}


def _summary(uuid: object, **fields: object) -> dict:
    """One ``ManagedSystem/quick/All`` item."""
    return {"UUID": uuid, **fields}


def _healthy_client() -> AsyncMock:
    client = AsyncMock()
    client.list_system_summaries.return_value = [
        _summary("sys-1", SystemName="system-a", State="operating")
    ]
    client.list_logical_partitions.return_value = [
        _entry(
            "lpar-1",
            PartitionName="aix-a",
            PartitionState="running",
            ResourceMonitoringControlState="active",
        )
    ]
    client.list_vios.return_value = [
        _entry("vios-1", PartitionName="vios-a", PartitionState="running")
    ]
    return client


@pytest.mark.asyncio
async def test_healthy_estate_returns_empty_collections() -> None:
    result = await fleet_health(_healthy_client())

    assert result == FleetHealthResult((), (), (), ())


@pytest.mark.asyncio
async def test_degraded_estate_returns_curated_sorted_issues() -> None:
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("sys-b", SystemName="system-b", State="standby"),
        _summary("sys-a", SystemName="system-a", State="OPERATING"),
    ]

    async def lpars(system_uuid: str) -> list[dict]:
        if system_uuid == "sys-b":
            return [
                _entry(
                    "lpar-z",
                    PartitionName="zeta",
                    PartitionState="running",
                    ResourceMonitoringControlState="inactive",
                )
            ]
        return []

    async def vios(system_uuid: str) -> list[dict]:
        if system_uuid == "sys-b":
            return [
                _entry(
                    "vios-z", PartitionName="zeta-vios", PartitionState="not activated"
                )
            ]
        return []

    client.list_logical_partitions.side_effect = lpars
    client.list_vios.side_effect = vios

    result = await fleet_health(client)

    assert result.systems == (
        {"uuid": "sys-b", "name": "system-b", "state": "standby"},
    )
    assert result.vios == (
        {
            "uuid": "vios-z",
            "name": "zeta-vios",
            "state": "not activated",
            "system_uuid": "sys-b",
            "system_name": "system-b",
        },
    )
    assert result.lpars == (
        {
            "uuid": "lpar-z",
            "name": "zeta",
            "state": "running",
            "rmc_state": "inactive",
            "system_uuid": "sys-b",
            "system_name": "system-b",
        },
    )
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_malformed_child_identities_remain_visible_as_unknown() -> None:
    client = _healthy_client()
    client.list_logical_partitions.return_value = [
        _entry(
            None,
            PartitionName=7,
            PartitionState=None,
            ResourceMonitoringControlState=None,
        )
    ]
    client.list_vios.return_value = [_entry(None, PartitionName=7, PartitionState=None)]

    result = await fleet_health(client)

    assert result.lpars[0]["uuid"] == result.lpars[0]["name"] == "unknown"
    assert result.vios[0]["uuid"] == result.vios[0]["name"] == "unknown"


@pytest.mark.asyncio
async def test_core_inventory_error_propagates_without_partial_result() -> None:
    client = _healthy_client()
    error = HMCError("LPAR inventory failed", 500, "failure")
    client.list_logical_partitions.side_effect = error

    with pytest.raises(HMCError) as exc_info:
        await fleet_health(client)

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_core_inventory_error_cancels_sibling_reads() -> None:
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("sys-fail", SystemName="failing", State="operating"),
        _summary("sys-block", SystemName="blocked", State="operating"),
    ]
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    error = HMCError("LPAR inventory failed", 500, "failure")

    async def lpars(system_uuid: str) -> list[dict]:
        if system_uuid == "sys-fail":
            async with asyncio.timeout(5):
                await sibling_started.wait()
            raise error
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return []

    client.list_logical_partitions.side_effect = lpars

    with pytest.raises(HMCError) as exc_info:
        await fleet_health(client)

    assert exc_info.value is error
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_core_inventory_error_cancels_same_system_sibling_read() -> None:
    client = _healthy_client()
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    error = HMCError("LPAR inventory failed", 500, "failure")

    async def lpars(_system_uuid: str) -> list[dict]:
        async with asyncio.timeout(5):
            await sibling_started.wait()
        raise error

    async def vios(_system_uuid: str) -> list[dict]:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return []

    client.list_logical_partitions.side_effect = lpars
    client.list_vios.side_effect = vios

    with pytest.raises(HMCError) as exc_info:
        await fleet_health(client)

    assert exc_info.value is error
    assert sibling_cancelled.is_set()


@pytest.mark.parametrize("uuid", [None, "", "  ", 42])
@pytest.mark.asyncio
async def test_malformed_system_identity_fails_before_child_reads(uuid: object) -> None:
    client = _healthy_client()
    client.list_system_summaries.return_value = [_summary(uuid, SystemName="broken")]

    with pytest.raises(ValueError, match="Managed system.*valid UUID"):
        await fleet_health(client)

    client.list_logical_partitions.assert_not_awaited()
    client.list_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_estate_fails_before_child_reads(monkeypatch) -> None:
    monkeypatch.setattr(operations_health, "_MAX_SYSTEMS", 1)
    client = _healthy_client()
    client.list_system_summaries.return_value *= 2

    with pytest.raises(ValueError, match="safe limit of 1 managed systems"):
        await fleet_health(client)

    client.list_logical_partitions.assert_not_awaited()
    client.list_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_system_inventory_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(operations_health, "_MAX_RESOURCES_PER_SYSTEM", 1)
    client = _healthy_client()
    client.list_logical_partitions.return_value *= 2

    with pytest.raises(ValueError, match="safe limit of 1 resources per category"):
        await fleet_health(client)


@pytest.mark.asyncio
async def test_aggregate_issue_budget_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(operations_health, "_MAX_ISSUES", 1)
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("sys-1", SystemName="one", State="error"),
        _summary("sys-2", SystemName="two", State="standby"),
    ]

    with pytest.raises(ValueError, match="safe limit of 1 issues"):
        await fleet_health(client)

    client.list_logical_partitions.assert_not_awaited()
    client.list_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_scalar_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(operations_health, "_MAX_SCALAR_LENGTH", 5)
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("sys-1", SystemName="too-long", State="operating")
    ]

    with pytest.raises(ValueError, match="safe limit of 5 characters"):
        await fleet_health(client)


@pytest.mark.asyncio
async def test_oversized_system_uuid_fails_before_child_reads(monkeypatch) -> None:
    monkeypatch.setattr(operations_health, "_MAX_SCALAR_LENGTH", 5)
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("uuid-too-long", SystemName="sys", State="operating")
    ]

    with pytest.raises(ValueError, match="safe limit of 5 characters"):
        await fleet_health(client)

    client.list_logical_partitions.assert_not_awaited()
    client.list_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_workers_and_active_inspections_are_bounded(monkeypatch) -> None:
    active = 0
    maximum = 0
    release = asyncio.Event()
    calls: list[str] = []
    scheduled_coroutines: list[str] = []
    create_task = asyncio.create_task

    def recording_create_task(coro):
        scheduled_coroutines.append(coro.cr_code.co_name)
        return create_task(coro)

    monkeypatch.setattr(operations_health.asyncio, "create_task", recording_create_task)

    class RecordingClient:
        async def list_system_summaries(self) -> list[dict]:
            calls.append("list_system_summaries")
            return [
                _summary(
                    f"sys-{index}", SystemName=f"system-{index}", State="operating"
                )
                for index in range(30)
            ]

        async def list_logical_partitions(self, _uuid: str) -> list[dict]:
            nonlocal active, maximum
            calls.append("list_logical_partitions")
            active += 1
            maximum = max(maximum, active)
            if maximum == 8:
                release.set()
            await release.wait()
            active -= 1
            return []

        async def list_vios(self, _uuid: str) -> list[dict]:
            calls.append("list_vios")
            await release.wait()
            return []

    result = await fleet_health(RecordingClient())  # type: ignore[arg-type]

    assert result == FleetHealthResult((), (), (), ())
    assert maximum == 8
    assert scheduled_coroutines.count("inspect_systems") == 8
    assert Counter(calls) == Counter(
        {
            "list_system_summaries": 1,
            "list_logical_partitions": 30,
            "list_vios": 30,
        }
    )


@pytest.mark.asyncio
async def test_not_activated_partition_is_not_an_rmc_issue() -> None:
    """A powered-off partition has no RMC connection by definition (#1202)."""
    client = _healthy_client()
    client.list_logical_partitions.return_value = parse_feed(
        live_fixture("rest-lpar-entry-lp3")["body"]
    )
    lpar = client.list_logical_partitions.return_value[0]["Resource"]
    assert lpar["PartitionState"] == "not activated"
    assert lpar["ResourceMonitoringControlState"] == "inactive"

    assert (await fleet_health(client)).lpars == ()


@pytest.mark.asyncio
async def test_refused_vios_feed_becomes_a_warning_not_a_failure() -> None:
    """A V11R2 HMC answers a system's VIOS feed with HTTP 500 (#1202)."""
    refused = live_fixture("rest-vios-feed-500-v11r2")
    client = _healthy_client()
    client.list_vios.side_effect = HMCError(
        "GET VirtualIOServer failed", refused["status"], refused["body"]
    )

    result = await fleet_health(client)

    assert result.lpars == () and result.vios == ()
    (warning,) = result.warnings
    assert warning.startswith("VIOS inventory for system system-a is unavailable")
    assert "HTTP 500" in warning


@pytest.mark.asyncio
async def test_non_operating_system_lpar_feed_becomes_a_warning() -> None:
    """A system not operating answers its feeds with 204 (#1289, #1301)."""
    client = _healthy_client()
    client.list_system_summaries.return_value = [
        _summary("sys-1", SystemName="sys-R1", State="no connection"),
        _summary("sys-2", SystemName="sys-R2", State="operating"),
    ]
    refused = HMCError("Cannot list LPARs on managed system 'sys-R1'")

    async def lpars(system_uuid: str) -> list[dict]:
        if system_uuid == "sys-1":
            raise refused
        return [
            _entry(
                "lpar-2",
                PartitionName="aix-b",
                PartitionState="running",
                ResourceMonitoringControlState="inactive",
            )
        ]

    client.list_logical_partitions.side_effect = lpars

    result = await fleet_health(client)

    assert [system["name"] for system in result.systems] == ["sys-R1"]
    assert [lpar["system_name"] for lpar in result.lpars] == ["sys-R2"]
    assert result.warnings == (
        f"LPAR inventory for system sys-R1 is unavailable: {refused}",
    )


@pytest.mark.asyncio
async def test_fleet_health_does_not_read_the_job_feed() -> None:
    """No captured HMC serves GET /rest/api/uom/Job (#1202); nothing asks for it."""
    client = _healthy_client()

    await fleet_health(client)

    client.list_uom.assert_not_awaited()


@pytest.mark.parametrize(
    "feed,state", [("vios", "operating"), ("vios", "standby"), ("lpars", "standby")]
)
@pytest.mark.asyncio
async def test_pin_mismatch_aborts_health_instead_of_returning_partial_result(
    feed, state
):
    failure = HMCTLSFingerprintError("pin mismatch")
    hmc = _healthy_client()
    hmc.list_system_summaries.return_value = [
        _summary("first", SystemName="first", State=state),
    ]
    method = hmc.list_vios if feed == "vios" else hmc.list_logical_partitions
    method.side_effect = failure
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await fleet_health(hmc)
    assert caught.value is failure
    for read in (hmc.list_vios, hmc.list_logical_partitions):
        assert all(call.args == ("first",) for call in read.await_args_list)
