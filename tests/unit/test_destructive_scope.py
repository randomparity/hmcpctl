from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.activation import power_off_lpar
from hmcpctl.operations.lpar.core import delete_lpar, rename_lpar
from hmcpctl.operations.vios import core as operations_vios
from hmcpctl.operations.vios.core import power_vios
from hmcpctl.server_tools.lpar import lifecycle as server_lpars
from hmcpctl.server_tools.vios import core as server_vios


def _client_factory(hmc):
    @asynccontextmanager
    async def factory(_profile=None):
        yield hmc

    return factory


@pytest.mark.asyncio
async def test_delete_lpar_uses_required_system_to_scope_name_resolution():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_partition_by_name.return_value = {"UUID": "lpar-uuid"}
    hmc.get_logical_partition.return_value = {
        "Resource": {"PartitionName": "aix1", "Description": ""}
    }
    hmc.get_managed_system.return_value = {"Resource": {"SystemName": "system-name"}}
    hmc.get_quick_property.return_value = "not activated"

    assert (
        await delete_lpar(
            hmc, "aix1", system_name_or_uuid="system-name", ownership_override=True
        )
        == "lpar-uuid"
    )

    hmc.find_partition_by_name.assert_awaited_once_with(
        "aix1", system_uuid="system-uuid"
    )


@pytest.mark.asyncio
async def test_rename_lpar_uses_required_system_to_scope_name_resolution():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_partition_by_name.return_value = {"UUID": "lpar-uuid"}
    hmc.get_logical_partition.return_value = {
        "Resource": {"PartitionName": "aix1", "Description": ""}
    }
    hmc.get_managed_system.return_value = {"Resource": {"SystemName": "system-name"}}

    await rename_lpar(
        hmc,
        "aix1",
        "renamed",
        system_name_or_uuid="system-name",
        ownership_override=True,
    )

    hmc.find_partition_by_name.assert_awaited_once_with(
        "aix1", system_uuid="system-uuid"
    )


@pytest.mark.parametrize("operation", [delete_lpar, rename_lpar])
@pytest.mark.parametrize("contained", [True, False])
@pytest.mark.asyncio
async def test_required_uuid_scope_is_verified_before_mutation(operation, contained):
    system = "cccc0000-0000-0000-0000-000000000001"
    partition = "aaaa0000-0000-0000-0000-000000000001"
    hmc = AsyncMock()
    hmc.list_logical_partitions.return_value = (
        [{"UUID": partition}] if contained else []
    )
    hmc.get_logical_partition.return_value = {"Resource": {"PartitionName": "aix1"}}
    hmc.get_quick_property.return_value = "not activated"
    args = (partition, "renamed") if operation is rename_lpar else (partition,)
    if contained:
        result = await operation(
            hmc, *args, system_name_or_uuid=system, ownership_override=True
        )
        assert (result[0] if operation is rename_lpar else result) == partition
        if operation is rename_lpar:
            assert hmc.update_logical_partition.await_args.args[0] == partition
        else:
            hmc.delete_logical_partition.assert_awaited_once_with(partition)
    else:
        with pytest.raises(ValueError, match="does not belong"):
            await operation(
                hmc, *args, system_name_or_uuid=system, ownership_override=True
            )
        hmc.update_logical_partition.assert_not_awaited()
        hmc.delete_logical_partition.assert_not_awaited()
        hmc.get_logical_partition.assert_not_awaited()
    hmc.list_logical_partitions.assert_awaited_once_with(system)
    hmc.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_power_off_lpar_forwards_optional_system_scope():
    hmc = AsyncMock()
    # A real config: power_off_lpar reads authorize_power_operations off it, and a
    # child mock would be truthy — silently enabling the ADR 0092 §4 guard.
    # from_mapping so an exported HMC_AUTHORIZE_POWER_OPERATIONS cannot enable
    # it either (ADR 0096).
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p"}
    )
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_partition_by_name.return_value = {"UUID": "lpar-uuid"}
    hmc.submit_job.return_value = {"UUID": "job-uuid"}

    await power_off_lpar(hmc, "aix1", system_name_or_uuid="system-name")

    hmc.find_partition_by_name.assert_awaited_once_with(
        "aix1", system_uuid="system-uuid"
    )


@pytest.mark.asyncio
async def test_power_vios_forwards_optional_system_scope():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_vios_by_name.return_value = {"UUID": "vios-uuid"}
    hmc.power_off_vios.return_value = {"UUID": "job-uuid"}

    await power_vios(
        hmc,
        "vios1",
        system_name_or_uuid="system-name",
        power_on=False,
    )

    hmc.find_vios_by_name.assert_awaited_once_with("vios1", system_uuid="system-uuid")


def test_power_off_lpar_tool_forwards_system_scope(monkeypatch):
    hmc = AsyncMock()
    operation = AsyncMock(return_value=AsyncMock(job={"UUID": "job-uuid"}))
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    monkeypatch.setattr(server_lpars, "power_off_lpar", operation)

    server_lpars.hmc_power_off_lpar("aix1", system_name_or_uuid="system-name")

    assert operation.await_args.kwargs["system_name_or_uuid"] == "system-name"


def test_delete_vios_tool_scopes_name_before_mutation(monkeypatch):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_vios_by_name.return_value = {"UUID": "vios-uuid"}
    hmc.get_quick_property.return_value = "not activated"
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))

    server_vios.hmc_delete_vios("vios1", system_name_or_uuid="system-name")

    hmc.find_vios_by_name.assert_awaited_once_with("vios1", system_uuid="system-uuid")
    hmc.get_quick_property.assert_awaited_once_with(
        "VirtualIOServer", "vios-uuid", "PartitionState"
    )
    hmc.delete_vios.assert_awaited_once_with("vios-uuid")


def test_restore_vios_tool_forwards_system_scope(monkeypatch):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    hmc.find_vios_by_name.return_value = {"UUID": "vios-uuid"}
    command = AsyncMock(return_value="restored")
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    monkeypatch.setattr(operations_vios, "run_hmc_cli", command)

    assert (
        server_vios.hmc_restore_vios(
            "system-name",
            "vios1",
            "backup",
            backup_type="ssp",
            restart_if_required=False,
        )
        == "restored"
    )

    hmc.find_vios_by_name.assert_awaited_once_with("vios1", system_uuid="system-uuid")


def test_power_off_vios_tool_forwards_system_scope(monkeypatch):
    hmc = AsyncMock()
    operation = AsyncMock(return_value={"UUID": "job-uuid"})
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    monkeypatch.setattr(server_vios, "power_vios", operation)

    server_vios.hmc_power_off_vios("vios1", system_name_or_uuid="system-name")

    assert operation.await_args.args[1] == "vios1"
    assert operation.await_args.kwargs["system_name_or_uuid"] == "system-name"
