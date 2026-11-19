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
            "AssociatedSystemProcessorConfiguration": {
                "CurrentAvailableSystemProcessorUnits": "2.00"
            },
        },
    }
    hmc.list_logical_partitions.return_value = [member(LPAR, 2, "0.55")]
    hmc.list_vios.return_value = [member(VIOS, 2, "0.20")]
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
    hmc.list_vios.return_value = [member(VIOS, 2, "NaN")]
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
    change = update.PoolChange.from_values(name="A,B", maximum="2.00", reserved="0.05")
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
        "new_name=A,B",
        "max_pool_proc_units=2",
        "reserved_pool_proc_units=0.05",
    ]
    with pytest.raises(ValueError, match="default"):
        await update.submit_native(None, "Power Box", 0, change)
    assert dispatch.await_count == 1
