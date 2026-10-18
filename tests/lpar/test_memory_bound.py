"""Partition memory beyond the managed system's configurable memory (#1166).

The system entry mirrors the live V10R3 read (POWER9): ``ConfigurableSystemMemory`` is
131072 MiB, nested under ``AssociatedSystemMemoryConfiguration``. The live create that
motivated the check asked for 8388608 MiB and ``mksyscfg`` accepted it.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.dlpar import set_lpar_memory

SYSTEM_UUID = "cccc0000-0000-0000-0000-000000000001"
LPAR_UUID = "aaaa0000-0000-0000-0000-000000000001"
CONFIGURABLE_MIB = 131072
OVERSIZE_MIB = 8388608


def _system(configurable: Any = str(CONFIGURABLE_MIB)) -> dict[str, Any]:
    return {
        "UUID": SYSTEM_UUID,
        "Resource": {
            "AssociatedSystemMemoryConfiguration": {
                "ConfigurableSystemMemory": configurable,
                "CurrentAvailableSystemMemory": "112448",
                "PermanentSystemMemory": {
                    "@attrs": {"ksv": "V1_10_0"},
                    "text": "262144",
                },
            }
        },
    }


def _hmc(system: dict[str, Any] | None) -> AsyncMock:
    hmc = AsyncMock(spec=HMCClient)
    hmc.find_partition_by_name.return_value = None
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "sys-a", "State": "operating"}
    ]
    hmc.get_managed_system.return_value = system
    return hmc


def _creation(desired: int | None, maximum: int | None = None) -> LparCreation:
    return LparCreation(
        name="probe-lpar",
        partition_type="AIX/Linux",
        resources=LparResources(desired_memory=desired, max_memory=maximum),
    )


@pytest.mark.asyncio
async def test_create_refuses_desired_memory_above_configurable_before_any_write():
    hmc = _hmc(_system())

    with pytest.raises(ValueError, match=r"8388608 MiB.*131072 MiB"):
        await create_and_stamp_lpar(
            hmc, SYSTEM_UUID, _creation(OVERSIZE_MIB, OVERSIZE_MIB)
        )

    hmc.create_logical_partition.assert_not_awaited()
    hmc.get_managed_system.assert_awaited_once_with(SYSTEM_UUID)


@pytest.mark.asyncio
async def test_create_allows_desired_memory_equal_to_configurable():
    hmc = _hmc(_system())
    hmc.create_logical_partition.return_value = None

    result = await create_and_stamp_lpar(
        hmc, SYSTEM_UUID, _creation(CONFIGURABLE_MIB, CONFIGURABLE_MIB)
    )

    assert result.resource_created is True
    hmc.create_logical_partition.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_reads_configurable_memory_from_a_ksv_wrapped_leaf():
    hmc = _hmc(_system({"@attrs": {"ksv": "V1_10_0"}, "text": "131072"}))

    with pytest.raises(ValueError, match="131072 MiB"):
        await create_and_stamp_lpar(hmc, SYSTEM_UUID, _creation(OVERSIZE_MIB))

    hmc.create_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_without_desired_memory_reads_no_system():
    hmc = _hmc(_system())
    hmc.create_logical_partition.return_value = None
    # A processor value keeps the create on REST: with no resource value at all it
    # goes straight to mksyscfg (#1164).
    creation = LparCreation(
        name="probe-lpar",
        partition_type="AIX/Linux",
        resources=LparResources(desired_vcpus=1),
    )

    await create_and_stamp_lpar(hmc, SYSTEM_UUID, creation)

    hmc.get_managed_system.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("system", [None, {"Resource": {}}, _system("unreadable")])
async def test_create_is_unchecked_when_the_system_reports_no_configurable_memory(
    system,
):
    hmc = _hmc(system)
    hmc.create_logical_partition.return_value = None

    result = await create_and_stamp_lpar(hmc, SYSTEM_UUID, _creation(OVERSIZE_MIB))

    assert result.resource_created is True


@pytest.mark.asyncio
async def test_dlpar_memory_refuses_desired_memory_above_configurable_before_the_write(
    monkeypatch,
):
    hmc = _hmc(_system())
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value=LPAR_UUID),
    )

    with pytest.raises(ValueError, match=r"8388608 MiB.*131072 MiB"):
        await set_lpar_memory(
            hmc,
            LPAR_UUID,
            LparResources(desired_memory=OVERSIZE_MIB),
            system_name_or_uuid=SYSTEM_UUID,
        )

    hmc.update_logical_partition.assert_not_awaited()
