"""Private creation inputs and point-in-time observation handoff."""

from dataclasses import FrozenInstanceError, replace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.errors import HMCTLSFingerprintError
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.partition_creation import (
    _CreationFeatureCoordinator,
    _CreationFeatureInputs,
    _CreationObservations,
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


@pytest.mark.parametrize("status", [None, "error", "skipped", "ok"])
def test_selected_evidence_requires_success_even_for_explicit_false(status):
    inputs = _CreationFeatureInputs(
        "partition", "AIX/Linux", LparResources(), hardware_mem_encryption=False
    )
    observations = _CreationObservations.selected(inputs)
    assert observations.hardware_memory_step.requested is True
    assert observations.verified is False
    step = None if status is None else WorkflowStep("hardware_memory", status)
    retained = observations.record("hardware_memory_step", step)
    assert retained.verified is (status == "ok")
    assert retained.public_steps() == {"hardware_memory_step": step}
    assert observations.public_steps() == {}
    assert not replace(
        retained,
        hardware_memory_step=replace(retained.hardware_memory_step, completed=False),
    ).verified
    with pytest.raises(FrozenInstanceError):
        retained.hardware_memory_step.completed = False
    with pytest.raises(FrozenInstanceError):
        retained.hardware_memory_step = observations.hardware_memory_step


def test_apply_and_raw_start_do_not_gate_their_own_observation():
    inputs = _CreationFeatureInputs(
        "partition",
        "AIX/Linux",
        LparResources(),
        start_settings=LparStartSettings(auto_start=False),
        apply_profile=True,
    )
    observations = _CreationObservations.selected(inputs)
    assert observations.verified is False
    observations = observations.record("settings_step", WorkflowStep("settings", "ok"))
    assert observations.verified is True
    assert not observations.apply_step.completed
    assert not observations.start_step.completed


def test_interruption_preserves_facts_and_distinguishes_apply_from_requested_steps():
    inputs = _CreationFeatureInputs(
        "partition",
        "AIX/Linux",
        LparResources(),
        hardware_mem_encryption=False,
        affinity_group_id=1,
        apply_profile=True,
    )
    previous = WorkflowStep("affinity_group", "ok", {"verified": True})
    observations = _CreationObservations.selected(inputs).record(
        "affinity_group_step", previous
    )
    interrupted = observations.interrupted("hardware_memory_step", "TLS cutoff")
    assert interrupted.affinity_group_step.step is previous
    assert interrupted.hardware_memory_step.step == WorkflowStep(
        "hardware_memory", "error", "TLS cutoff"
    )
    assert interrupted.apply_step.step == WorkflowStep(
        "apply_profile", "skipped", "TLS cutoff"
    )
    assert observations.public_steps() == {"affinity_group_step": previous}
    active_apply = observations.interrupted("apply_step", "TLS cutoff")
    assert active_apply.apply_step.step.status == "error"


def test_interruption_before_requested_observations_keeps_fallback_error():
    inputs = _CreationFeatureInputs("partition", "AIX/Linux", LparResources())
    observations = _CreationObservations.selected(inputs)
    assert observations.interrupted(
        None, "TLS cutoff"
    ).settings_step.step == WorkflowStep("settings", "error", "TLS cutoff")


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["AIX/Linux", "Virtual IO Server"])
@pytest.mark.parametrize("tls_failure", [False, True])
async def test_real_start_observer_retains_raw_evidence_and_existing_failure_phase(
    coordinator, environment, tls_failure
):
    from hmcpctl.operations.lpar.creation.start import admit_start_creation

    inputs = replace(
        coordinator.inputs,
        environment=environment,
        start_settings=LparStartSettings(conn_monitoring=False),
    )
    coordinator = _CreationFeatureCoordinator(
        coordinator.hmc, coordinator.system_uuid, coordinator.system_selector, inputs
    )
    coordinator.controller_mode = True
    entry = {
        "UUID": "22222222-2222-4222-8222-222222222222",
        "ResourceType": inputs.kind,
        "Resource": {
            "PartitionName": inputs.name,
            "PartitionID": "1",
            "PartitionType": environment,
            "IsConnectionMonitoringEnabled": "false",
        },
    }
    hmc = coordinator.hmc
    hmc.list_logical_partitions.return_value = [] if inputs.vios else [entry]
    hmc.list_vios.return_value = [entry] if inputs.vios else []
    hmc.get_uom.return_value = entry
    coordinator.start_admission = await admit_start_creation(
        hmc, coordinator.system_uuid, coordinator.system, inputs.start_settings
    )
    earlier = WorkflowStep("serial", "ok", {"retained": True})
    await coordinator.observe("serial_step", earlier)
    if tls_failure:
        failure = HMCTLSFingerprintError("certificate identity changed")
        hmc.get_uom.side_effect = failure
        with pytest.raises(HMCTLSFingerprintError) as caught:
            await coordinator.observe_start(entry)
        assert caught.value is failure
        snapshot = coordinator.snapshot()
        assert snapshot.active_step == (
            "start_step" if inputs.vios else "settings_step"
        )
        assert not snapshot.observations.start_step.completed
    else:
        step = await coordinator.observe_start(entry)
        assert step.status == "ok"
        snapshot = coordinator.snapshot()
        assert snapshot.observations.start_step.completed
        assert snapshot.observations.start_step.step is step
        assert snapshot.active_step is None
        assert "start_step" not in snapshot.facts
    assert snapshot.observations.serial_step.step is earlier
    hmc.get_uom.assert_awaited_once()
