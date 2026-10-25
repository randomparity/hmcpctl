"""Partial-state contracts for LPAR modification workflows."""

from unittest.mock import AsyncMock

import pytest

from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
)
from hmcpctl.operations.lpar.dlpar import modify_lpar
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

SYSTEM_UUID = "00000000-0000-4000-8000-0000000000c1"


def _hmc_with_system(configurable_memory_mib: int = 16384) -> AsyncMock:
    """An HMC double whose system reads return the dict documents production gets."""
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "system-1", "State": "operating"}
    ]
    hmc.get_managed_system.return_value = {
        "Resource": {
            "AssociatedSystemMemoryConfiguration": {
                "ConfigurableSystemMemory": str(configurable_memory_mib)
            }
        }
    }
    return hmc


@pytest.mark.asyncio
async def test_modify_lpar_returns_rename_when_resource_update_fails(monkeypatch):
    hmc = _hmc_with_system()
    hmc.update_logical_partition.side_effect = [
        {"UUID": "lpar-1", "PartitionName": "renamed"},
        HMCError("resource update failed", 500),
    ]
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value="lpar-1"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )

    result = await modify_lpar(
        hmc,
        "lpar-1",
        LparResources(desired_memory=8192),
        LparPcieAssignments(),
        new_name="renamed",
        system_name_or_uuid="system-1",
    )

    assert result.workflow_completed is False
    assert result.lpar == {"UUID": "lpar-1", "PartitionName": "renamed"}
    assert [(step.step, step.status) for step in result.steps] == [
        ("rename", "ok"),
        ("resources", "error"),
    ]
    assert result.warnings == ("resource update failed (HTTP 500)",)
    hmc.get_managed_system.assert_awaited_once_with(SYSTEM_UUID)


@pytest.mark.asyncio
async def test_modify_lpar_propagates_resource_failure_without_partial_state(
    monkeypatch,
):
    hmc = _hmc_with_system()
    hmc.update_logical_partition.side_effect = HMCError("resource update failed", 500)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value="lpar-1"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )

    with pytest.raises(HMCError, match="resource update failed"):
        await modify_lpar(
            hmc,
            "lpar-1",
            LparResources(desired_memory=8192),
            LparPcieAssignments(),
            system_name_or_uuid="system-1",
        )


@pytest.mark.asyncio
async def test_modify_lpar_refuses_memory_above_the_system_bound(monkeypatch):
    hmc = _hmc_with_system(configurable_memory_mib=4096)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value="lpar-1"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )

    with pytest.raises(ValueError, match="exceeds the managed system's configurable"):
        await modify_lpar(
            hmc,
            "lpar-1",
            LparResources(desired_memory=8192),
            LparPcieAssignments(),
            system_name_or_uuid="system-1",
        )

    hmc.update_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_lpar_preserves_steps_when_final_readback_fails(monkeypatch):
    hmc = AsyncMock()
    hmc.get_logical_partition.side_effect = HMCError("readback unavailable", 503)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value="lpar-1"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.apply_validated_lpar_pcie_assignments",
        AsyncMock(
            return_value=LparPcieWorkflowResult(
                False,
                True,
                None,
                None,
                (WorkflowStep("assign", "ok"),),
                (),
            )
        ),
    )

    result = await modify_lpar(
        hmc,
        "lpar-1",
        LparResources(),
        LparPcieAssignments(),
        system_name_or_uuid="system-1",
    )

    assert result.workflow_completed is True
    assert [(step.step, step.status) for step in result.steps] == [("assign", "ok")]
    assert result.warnings == (
        "final LPAR readback failed: readback unavailable (HTTP 503)",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sync", "warned"), [("Disabled", True), ("On", False), (None, True)]
)
async def test_modify_lpar_warns_when_the_profile_did_not_change(
    monkeypatch, sync, warned
):
    resource = {"Resource": {"CurrentProfileSync": sync} if sync else {}}
    hmc = AsyncMock()
    hmc.update_logical_partition.return_value = resource
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value="lpar-1"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )

    result = await modify_lpar(
        hmc,
        "lpar-1",
        LparResources(desired_memory=3072),
        LparPcieAssignments(),
        system_name_or_uuid=None,
    )

    assert bool(result.warnings) is warned
    if warned:
        assert "CurrentProfileSync" in result.warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resources",
    [LparResources(desired_memory=-1), LparResources(desired_procs=float("nan"))],
)
async def test_sharing_modify_barrier_pure_validation_precedes_client_use(resources):
    hmc = AsyncMock()
    with pytest.raises(ValueError, match="finite, non-negative"):
        await modify_lpar(
            hmc,
            "lpar-1",
            resources,
            LparPcieAssignments(),
            new_name="renamed",
            system_name_or_uuid="system-1",
        )
    assert hmc.mock_calls == []
