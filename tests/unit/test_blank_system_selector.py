"""A blank managed-system selector reads as absent on every path (#945, ADR 0094).

ADR 0094 already reads ``""`` and whitespace as an omitted selector on the
guarded-mutation path (``test_a_blank_system_selector_is_read_as_absent``).
These tests hold the read and resolve paths to the same rule, so ``--system ""``
and an MCP client's serialised unset optional scope every command family alike
instead of resolving a managed system named ``""``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from hmcpctl.documents.lpar import LparResources
from hmcpctl.operations.affinity.rest import ProvisionAffinityAssessment
from hmcpctl.operations.lpar.activation import power_on_lpar
from hmcpctl.operations.lpar.assignments import LparPcieAssignments
from hmcpctl.operations.lpar.boot_order import read_lpar_boot_order
from hmcpctl.operations.lpar.dlpar import modify_lpar
from hmcpctl.operations.lpar.inventory import get_lpar, list_lpars
from hmcpctl.operations.lpar.ownership import list_lpar_ownership
from hmcpctl.operations.metrics.pcm import (
    resolve_pcm_resource,
    validate_pcm_metric_target,
)
from hmcpctl.operations.vios.core import backup_vios, list_vios, restore_vios
from hmcpctl.operations.virtualization.adapters import list_adapters
from hmcpctl.resource_identity import (
    optional_system_selector,
    resolve_lpar_uuid,
    resolve_vios_uuid,
)

BLANKS = pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
VIOS_UUID = "33333333-3333-4333-8333-333333333333"


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.find_partition_by_name.return_value = {"UUID": LPAR_UUID}
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    return hmc


@BLANKS
def test_the_shared_rule_reads_a_blank_selector_as_absent(blank):
    assert optional_system_selector(blank) is None
    assert optional_system_selector(None) is None


def test_the_shared_rule_keeps_a_named_selector_without_padding():
    assert optional_system_selector("  frame-1 ") == "frame-1"


@BLANKS
@pytest.mark.asyncio
async def test_lpar_name_resolution_ignores_a_blank_system(blank):
    hmc = _hmc()

    assert await resolve_lpar_uuid(hmc, "lp1", system_name_or_uuid=blank) == LPAR_UUID

    hmc.list_system_summaries.assert_not_awaited()
    hmc.find_partition_by_name.assert_awaited_once_with("lp1")


@BLANKS
@pytest.mark.asyncio
async def test_vios_name_resolution_ignores_a_blank_system(blank):
    hmc = _hmc()

    assert await resolve_vios_uuid(hmc, "vios1", system_name_or_uuid=blank) == VIOS_UUID

    hmc.list_system_summaries.assert_not_awaited()
    hmc.find_vios_by_name.assert_awaited_once_with("vios1")


@BLANKS
@pytest.mark.asyncio
async def test_adapter_listing_ignores_a_blank_system(blank):
    """``adapters list --system ""`` resolves the partition fleet-wide."""
    hmc = _hmc()
    hmc.list_adapters.return_value = []

    await list_adapters(hmc, "lp1", "ClientNetworkAdapter", system_name_or_uuid=blank)

    hmc.list_system_summaries.assert_not_awaited()
    hmc.list_adapters.assert_awaited_once_with(LPAR_UUID, "ClientNetworkAdapter")


@BLANKS
@pytest.mark.asyncio
async def test_lpar_and_vios_inventory_ignore_a_blank_system(blank):
    hmc = _hmc()
    hmc.list_system_summaries.return_value = []

    await list_lpars(hmc, blank)
    await list_vios(hmc, blank)
    await get_lpar(hmc, "lp1", system_name_or_uuid=blank)

    assert hmc.list_system_summaries.await_count == 2
    hmc.list_logical_partitions.assert_not_awaited()
    hmc.list_vios.assert_not_awaited()
    hmc.find_partition_by_name.assert_awaited_once_with("lp1", system_uuid=None)


@BLANKS
@pytest.mark.asyncio
async def test_ownership_listing_walks_the_fleet_for_a_blank_system(blank):
    hmc = _hmc()
    hmc.list_system_summaries.return_value = []

    assert (await list_lpar_ownership(hmc, blank)).entries == []

    hmc.list_system_summaries.assert_awaited_once_with()


@BLANKS
@pytest.mark.asyncio
async def test_a_rename_with_a_blank_system_reports_the_missing_selector(blank):
    hmc = _hmc()

    with pytest.raises(ValueError, match="system_name_or_uuid is required"):
        await modify_lpar(
            hmc,
            "lp1",
            LparResources(),
            LparPcieAssignments(),
            new_name="lp2",
            system_name_or_uuid=blank,
        )

    hmc.list_system_summaries.assert_not_awaited()


@BLANKS
@pytest.mark.asyncio
async def test_pcm_targets_read_a_blank_system_as_absent(blank):
    hmc = _hmc()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "frame-1"}
    ]

    validate_pcm_metric_target("ManagedSystem", blank)
    target = await resolve_pcm_resource(
        hmc, "ManagedSystem", "frame-1", system_name_or_uuid=blank
    )
    assert target.resource_uuid == "system-uuid"

    with pytest.raises(ValueError, match="require the owning system_name_or_uuid"):
        validate_pcm_metric_target("LogicalPartition", blank)
    with pytest.raises(ValueError, match="require the owning system_name_or_uuid"):
        await resolve_pcm_resource(
            hmc, "LogicalPartition", "lp1", system_name_or_uuid=blank
        )


def _affinity(system: str) -> ProvisionAffinityAssessment:
    return ProvisionAffinityAssessment(
        system_name_or_uuid=system,
        lpar_name="lp1",
        captured_score=80,
        captured_policy_state="absent",
        captured_minimum=None,
        captured_at=datetime.now(UTC),
        stale_after_seconds=300,
        response="warn",
        regression_threshold=5,
        optimization_threshold=5,
    )


@BLANKS
@pytest.mark.asyncio
async def test_an_affinity_power_on_with_a_blank_system_submits_nothing(blank):
    """Affinity assessment needs a named system; a blank one is a missing one."""
    hmc = _hmc()
    hmc.config.authorize_power_operations = False

    with pytest.raises(ValueError, match="required for post-activation affinity"):
        await power_on_lpar(
            hmc, "lp1", system_name_or_uuid=blank, affinity_assessment=_affinity(blank)
        )

    hmc.submit_job.assert_not_awaited()
    hmc.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_affinity_power_on_strips_the_target_selector():
    hmc = _hmc()
    hmc.config.authorize_power_operations = False
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "frame-1"}
    ]
    hmc.get_quick_property.return_value = "running"

    await power_on_lpar(
        hmc,
        "lp1",
        system_name_or_uuid="frame-1  ",
        affinity_assessment=_affinity("frame-1"),
    )

    hmc.list_system_summaries.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_an_affinity_power_on_refuses_a_padded_captured_identity():
    """The assessment measures its captured identity, so it must match exactly."""
    hmc = _hmc()
    hmc.config.authorize_power_operations = False

    with pytest.raises(ValueError, match="identity must match target"):
        await power_on_lpar(
            hmc,
            "lp1",
            system_name_or_uuid="frame-1",
            affinity_assessment=replace(
                _affinity("frame-1"), system_name_or_uuid=" frame-1 "
            ),
        )

    hmc.submit_job.assert_not_awaited()


def _vios_writes(hmc: AsyncMock, system: str) -> list:
    return [
        backup_vios(hmc, "vios1", system_name_or_uuid=system, backup_name="b1"),
        restore_vios(
            hmc,
            "vios1",
            "b1",
            system_name_or_uuid=system,
            backup_type="viosioconfig",
        ),
    ]


@BLANKS
@pytest.mark.asyncio
async def test_vios_backup_and_restore_refuse_a_blank_required_system(blank):
    """A blank ``-m`` must not reach the HMC beside a fleet-resolved VIOS."""
    hmc = _hmc()
    cli = AsyncMock(return_value="ok")

    with patch("hmcpctl.operations.vios.core.run_hmc_cli", new=cli):
        for write in _vios_writes(hmc, blank):
            with pytest.raises(ValueError, match="required for VIOS backup"):
                await write

    cli.assert_not_awaited()
    hmc.find_vios_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_vios_backup_and_restore_send_the_stripped_system():
    hmc = _hmc()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "frame-1"}
    ]
    cli = AsyncMock(return_value="ok")

    with patch("hmcpctl.operations.vios.core.run_hmc_cli", new=cli):
        for write in _vios_writes(hmc, " frame-1 "):
            await write

    assert all(" -m frame-1 " in call.args[0] for call in cli.await_args_list)
    assert cli.await_count == 2


@BLANKS
@pytest.mark.asyncio
async def test_a_boot_order_read_refuses_a_blank_required_system(blank):
    hmc = _hmc()

    with pytest.raises(ValueError, match="required to read a boot order"):
        await read_lpar_boot_order(hmc, "lp1", system_name_or_uuid=blank)

    hmc.find_partition_by_name.assert_not_awaited()
