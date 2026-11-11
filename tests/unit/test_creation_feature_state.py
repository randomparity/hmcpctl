"""Private creation inputs and point-in-time observation handoff."""

from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCTLSFingerprintError
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.partition_creation import (
    _CreationFeatureCoordinator,
    _CreationFeatureInputs,
)


@pytest.fixture
def coordinator():
    return _CreationFeatureCoordinator(
        AsyncMock(spec=HMCClient),
        "11111111-1111-4111-8111-111111111111",
        "original-selector",
        _CreationFeatureInputs("partition", "AIX/Linux", LparResources()),
    )


@pytest.mark.parametrize(
    ("options", "hardware", "profile"),
    [
        ({}, False, False),
        ({"hardware_mem_encryption": False}, True, False),
        ({"affinity_group_id": 1}, False, True),
    ],
)
def test_normalized_inputs_preserve_selection_and_cannot_be_reassigned(
    options, hardware, profile
):
    resources = LparResources()
    inputs = _CreationFeatureInputs("partition", "AIX/Linux", resources, **options)
    assert inputs.resources is resources
    assert inputs.hardware_requested is hardware
    assert inputs.profile_features_requested is profile
    assert inputs.native_features_requested is (hardware or profile)
    with pytest.raises(FrozenInstanceError):
        inputs.name = "other"


@pytest.mark.parametrize("environment", ["AIX/Linux", "OS400", "Virtual IO Server"])
def test_input_environment_selects_the_existing_resource_kind(environment):
    inputs = _CreationFeatureInputs("partition", environment, LparResources())
    assert inputs.vios is (environment == "Virtual IO Server")
    assert inputs.kind == ("VirtualIOServer" if inputs.vios else "LogicalPartition")


@pytest.mark.asyncio
async def test_snapshot_distinguishes_absent_from_a_completed_none(coordinator):
    before = coordinator.snapshot()
    await coordinator.observe("serial_step", None)
    after = coordinator.snapshot()

    assert "serial_step" not in before.facts
    assert after.facts == {"serial_step": None}
    assert before.active_step is None and before.attempted_feature is None
    assert after.active_step is None and after.attempted_feature == "serial_step"


@pytest.mark.asyncio
async def test_snapshot_is_independent_readonly_and_preserves_step_identity(
    coordinator,
):
    payload = {"evidence": "retained"}
    step = WorkflowStep("serial", "ok", payload)
    await coordinator.observe("serial_step", step)
    before = coordinator.snapshot()

    with pytest.raises(TypeError):
        cast(Any, before.facts)["serial_step"] = None
    with pytest.raises(FrozenInstanceError):
        before.active_step = "serial_step"

    await coordinator.observe("hardware_memory_step", None)
    assert set(before.facts) == {"serial_step"}
    assert before.facts["serial_step"] is step
    assert before.facts["serial_step"].result is payload
    assert coordinator.snapshot().facts["hardware_memory_step"] is None


@pytest.mark.asyncio
async def test_failed_observation_snapshot_retains_prior_facts_and_failing_phase(
    coordinator,
):
    step = WorkflowStep("serial", "ok")
    await coordinator.observe("serial_step", step)
    before = coordinator.snapshot()
    failure = HMCTLSFingerprintError("certificate identity changed")

    async def reject():
        raise failure

    with pytest.raises(HMCTLSFingerprintError) as caught:
        await coordinator.observe("hardware_memory_step", reject())

    after = coordinator.snapshot()
    assert caught.value is failure
    assert after.facts == {"serial_step": step}
    assert "hardware_memory_step" not in after.facts
    assert after.active_step == after.attempted_feature == "hardware_memory_step"
    assert before.active_step is None and before.attempted_feature == "serial_step"


@pytest.mark.asyncio
async def test_cli_context_is_bound_by_the_existing_phase_without_caller_field_writes(
    coordinator,
):
    assert coordinator.system == "original-selector"
    await coordinator.discover_mode(cli_system="resolved-cli-name")
    assert coordinator.system == "resolved-cli-name"
    with pytest.raises(AttributeError):
        coordinator.system = "foreign"
    assert coordinator.hmc.mock_calls == []
