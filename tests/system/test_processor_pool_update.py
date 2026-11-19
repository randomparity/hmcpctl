"""Shared processor pool update preconditions and independent readback."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from hmcpctl.operations.systems.processor_pool_update import (
    PoolCapacity,
    capacity_evidence,
)

SYSTEM = "00000000-0000-0000-0000-000000000011"
LPAR = "00000000-0000-0000-0000-000000000012"
VIOS = "00000000-0000-0000-0000-000000000013"


def member(uuid, pool, units):
    return {
        "UUID": uuid,
        "Resource": {
            "PartitionProcessorConfiguration": {
                "CurrentHasDedicatedProcessors": "false",
                "CurrentSharedProcessorConfiguration": {
                    "CurrentSharedProcessorPoolID": str(pool),
                    "CurrentProcessingUnits": units,
                },
            }
        },
    }


def hardware():
    hmc = AsyncMock()
    hmc.get_managed_system.return_value = {
        "UUID": SYSTEM,
        "Resource": {
            "State": "operating",
            "MachineTypeModelAndSerialNumber": {
                "MachineType": "9999",
                "Model": "001",
                "SerialNumber": "ABC123",
            },
            "AssociatedSystemProcessorConfiguration": {
                "CurrentAvailableSystemProcessorUnits": "2.00"
            },
        },
    }
    hmc.list_pool_partition_entries.return_value = [
        member(LPAR, 2, "0.55"),
        member(VIOS, 2, "0.20"),
    ]
    return hmc


@pytest.mark.asyncio
async def test_capacity_includes_lpar_and_vios_and_distinguishes_empty_slot():
    hmc = hardware()
    assert await capacity_evidence(hmc, SYSTEM, 2, (LPAR, VIOS)) == PoolCapacity(
        2, Decimal("0.75"), Decimal("2.00")
    )
    assert await capacity_evidence(hmc, SYSTEM, 3, None) == PoolCapacity(
        0, Decimal(0), Decimal(2)
    )


@pytest.mark.asyncio
async def test_missing_membership_or_unreadable_source_refuses():
    hmc = hardware()
    with pytest.raises(ValueError, match="membership"):
        await capacity_evidence(hmc, SYSTEM, 2, ())
    hmc.list_pool_partition_entries.return_value = [member(VIOS, 2, "NaN")]
    with pytest.raises(ValueError):
        await capacity_evidence(hmc, SYSTEM, 2, None)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"maximum": "1.25"},
        {"reserved": "0.001"},
        {"reserved": True},
        {"name": " "},
        {"name": " Ops "},
        {"name": "A,B"},
        {"name": "A=B"},
        {"name": 'A"B'},
        {"name": "x" * 15},
        {"name": "a\nb"},
    ],
)
def test_refuse_invalid_updates(args):
    from hmcpctl.operations.systems.processor_pool_update import PoolChange

    with pytest.raises(ValueError):
        PoolChange.from_values(**args)


@pytest.mark.asyncio
async def test_native_command_keeps_values_in_one_csv_attribute(monkeypatch):
    import csv
    import shlex

    from hmcpctl.operations.systems import processor_pool_update as update

    dispatch = AsyncMock()
    monkeypatch.setattr(update, "run_hmc_command", dispatch)
    change = update.PoolChange.from_values(name="A B", maximum="2.00", reserved="0.05")
    await update.submit_native(None, "Power Box", 2, change)
    tokens = shlex.split(dispatch.await_args.args[1])
    assert tokens[:-1] == [
        "chhwres",
        "-r",
        "procpool",
        "-m",
        "Power Box",
        "-o",
        "s",
        "--poolid",
        "2",
        "-a",
    ]
    assert next(csv.reader([tokens[-1]])) == [
        "new_name=A B",
        "max_pool_proc_units=2",
        "reserved_pool_proc_units=0.05",
    ]
    with pytest.raises(ValueError, match="default"):
        await update.submit_native(None, "Power Box", 0, change)
    assert dispatch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [0, "DefaultPool"])
async def test_default_refused_by_id_or_name(monkeypatch, selector):
    from hmcpctl.operations.systems import processor_pool_update as update
    from hmcpctl.operations.systems.processor_pools import SharedProcessorPool

    default = SharedProcessorPool(
        SYSTEM, 0, "DefaultPool", True, None, None, None, None, None
    )
    hmc = AsyncMock()
    monkeypatch.setattr(
        update,
        "read_selected_pool",
        AsyncMock(return_value=(SYSTEM, default, (default,))),
    )
    with pytest.raises(ValueError, match="default"):
        await update.update_shared_processor_pool(hmc, "sys", selector, new_name="new")
    hmc.get_managed_system.assert_not_awaited()


def make_pool(pool_id=2, name="test pool"):
    from hmcpctl.operations.systems.processor_pools import SharedProcessorPool

    return SharedProcessorPool(LPAR, pool_id, name, True, 2.0, 0.0, 0.0, 2.0, None)


@pytest.fixture
def update_boundary(monkeypatch):
    from hmcpctl.operations.systems import processor_pool_update as update
    from hmcpctl.operations.systems.processor_pools import SharedProcessorPoolResult

    target = make_pool()
    hmc = hardware()
    monkeypatch.setattr(
        update,
        "read_selected_pool",
        AsyncMock(return_value=(SYSTEM, target, (target, make_pool(3, "other")))),
    )
    monkeypatch.setattr(update, "submit_native", AsyncMock())
    monkeypatch.setattr(
        update,
        "get_shared_processor_pool",
        AsyncMock(return_value=SharedProcessorPoolResult(SYSTEM, "", target)),
    )
    return update, hmc, target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"new_name": "other"}, "already exists"),
        ({"reserved_units": 3}, "pending reservation"),
        ({"maximum_units": 0}, "entitlements"),
        ({"maximum_units": 4, "reserved_units": "2.50"}, "available"),
    ],
)
async def test_prewrite_refusals(update_boundary, kwargs, message):
    update, hmc, _ = update_boundary
    with pytest.raises(ValueError, match=message):
        await update.update_shared_processor_pool(hmc, "sys", 2, **kwargs)
    update.submit_native.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("matched", "verified"),
        ("partial", "partial"),
        ("unreadable", "unverified"),
        ("wrong-id", "unverified"),
        ("unchanged", "unverified"),
        ("uncertain", "partial"),
    ],
)
async def test_independent_configuration_outcomes(update_boundary, kind, expected):
    from dataclasses import replace

    from hmcpctl.errors import HMCError
    from hmcpctl.operations.systems.processor_pools import SharedProcessorPoolResult

    update, hmc, before = update_boundary
    after = replace(before, name="renamed", pending_reserved_processing_units=0.05)
    if kind == "partial":
        after = replace(after, pending_reserved_processing_units=0)
    if kind == "uncertain":
        update.submit_native.side_effect = HMCError("write response lost")
    if kind == "unreadable":
        update.get_shared_processor_pool.side_effect = HMCError("read unavailable")
    if kind == "wrong-id":
        after = replace(after, uuid=VIOS)
    if kind == "unchanged":
        after = before
    update.get_shared_processor_pool.return_value = SharedProcessorPoolResult(
        SYSTEM, "", after
    )
    result = await update.update_shared_processor_pool(
        hmc, "sys", 2, new_name="renamed", reserved_units="0.05"
    )
    assert result.outcome == expected
    assert (result.route, result.if_match) == ("chhwres", "not-applicable")
    assert bool(result.submission_error) == (kind == "uncertain")
    assert bool(result.readback_error) == (kind in {"wrong-id", "unreadable"})
    if result.after is not None:
        assert result.after.reserved_processing_units == 0
    update.submit_native.assert_awaited_once()
    update.get_shared_processor_pool.assert_awaited_once_with(hmc, SYSTEM, 2)


def test_mcp_forwards_update_to_domain(update_boundary, monkeypatch):
    import asyncio

    from hmcpctl.server_tools.systems import processor_pools as tools

    _, hmc, _ = update_boundary
    monkeypatch.setattr(tools, "with_client", lambda fn, profile: asyncio.run(fn(hmc)))
    result = tools.hmc_update_shared_processor_pool("sys", 2, new_name="test pool")
    assert result["outcome"] == "verified"
    assert result["route"] == "chhwres"


@pytest.mark.parametrize(
    "kind", ["SharedProcessorPool", "LogicalPartition", "VirtualIOServer"]
)
def test_complete_pool_inventory_refuses_paged_feed(kind):
    from hmcpctl.xmlutil import parse_complete_feed

    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="?page=2"/></feed>'
    with pytest.raises(ValueError, match="incomplete"):
        parse_complete_feed(xml, kind)


@pytest.mark.asyncio
async def test_unrelated_available_units_do_not_make_failed_rename_partial(
    update_boundary,
):
    from dataclasses import replace

    from hmcpctl.operations.systems.processor_pools import SharedProcessorPoolResult

    update, hmc, target = update_boundary
    update.get_shared_processor_pool.return_value = SharedProcessorPoolResult(
        SYSTEM, "", replace(target, available_processing_units=1)
    )
    result = await update.update_shared_processor_pool(hmc, "sys", 2, new_name="new")
    assert result.outcome == "unverified"
    assert update.submit_native.await_args.args[1] == "9999-001*ABC123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "continued", ["SharedProcessorPool", "LogicalPartition", "VirtualIOServer"]
)
async def test_paged_child_feed_never_reaches_native_update(continued, monkeypatch):
    from unittest.mock import patch

    from conftest import live_fixture, make_config

    from hmcpctl.client.core import HMCClient
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.systems import processor_pool_update as update

    recorded = live_fixture("rest-shared-processor-pool-feed")["body"]
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    page = '<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="?page=2"/></feed>'

    async def get(path, kind, **kwargs):
        if kind == continued:
            return page
        return recorded if kind == "SharedProcessorPool" else empty

    hmc = HMCClient(make_config())
    submit = AsyncMock()
    monkeypatch.setattr(update, "submit_native", submit)
    with (
        patch.object(hmc, "_get", side_effect=get),
        patch.object(
            hmc,
            "list_system_summaries",
            return_value=[{"UUID": SYSTEM, "State": "operating"}],
        ),
        patch.object(
            hmc,
            "get_managed_system",
            return_value=hardware().get_managed_system.return_value,
        ),
        pytest.raises(HMCError, match="incomplete"),
    ):
        await update.update_shared_processor_pool(hmc, SYSTEM, 2, maximum_units=0)
    submit.assert_not_awaited()


@pytest.mark.parametrize(
    "xml,message",
    [
        ("<entry/>", "complete Atom"),
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>2</totalResults></feed>',
            "incomplete",
        ),
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry xmlns=""/></feed>',
            "namespace",
        ),
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content><Other/></content></entry></feed>',
            "resource type",
        ),
    ],
)
def test_refuses_unusable_envelopes(xml, message):
    from hmcpctl.xmlutil import parse_complete_feed

    with pytest.raises(ValueError, match=message):
        parse_complete_feed(xml, "LogicalPartition")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,state,accepted",
    [
        (204, "Operating", True),
        (204, "No Connection", False),
        (200, "Operating", False),
    ],
)
async def test_empty_pool_member_feed_needs_positive_system_state(
    mock_hmc, status, state, accepted
):
    from unittest.mock import patch

    import httpx
    from conftest import make_config

    from hmcpctl.client.core import HMCClient
    from hmcpctl.errors import HMCError

    base = f"/rest/api/uom/ManagedSystem/{SYSTEM}"
    mock_hmc.get(f"{base}/LogicalPartition").mock(
        return_value=httpx.Response(
            200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>'
        )
    )
    mock_hmc.get(f"{base}/VirtualIOServer").mock(return_value=httpx.Response(status))
    async with HMCClient(make_config()) as hmc:
        with patch.object(
            hmc,
            "list_system_summaries",
            return_value=[{"UUID": SYSTEM, "State": state}],
        ):
            if accepted:
                assert await hmc.list_pool_partition_entries(SYSTEM) == []
            else:
                with pytest.raises(HMCError):
                    await hmc.list_pool_partition_entries(SYSTEM)


@pytest.mark.asyncio
async def test_native_target_admits_the_shared_operating_state_spellings(
    update_boundary,
):
    update, hmc, _ = update_boundary
    resource = hmc.get_managed_system.return_value["Resource"]
    resource["State"] = "Operating"
    await update.update_shared_processor_pool(hmc, "sys", 2, new_name="test pool")
    update.submit_native.assert_awaited_once()
