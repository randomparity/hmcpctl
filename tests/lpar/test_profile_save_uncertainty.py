"""Profile-save uncertainty and legacy-journal recovery through the real engine."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lpar.test_partition_profiles import field, profile
from lpar.test_provision_tool import (
    LPAR_UUID,
    PROFILE_UUID,
    SYSTEM_UUID,
    _hmc_env,
    _mock_execution_steps,
    _mock_preconditions,
    _patch_stamp_ownership,
    _provision_args,
    hmc_provision_lpar,
    persistence,
)

from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.jobs import JobOutcome
from hmcpctl.operations.logical import store
from hmcpctl.operations.lpar.profiles.core import PartitionProfileOutcome
from hmcpctl.operations.lpar.provisioning import operation
from hmcpctl.operations.lpar.provisioning.core import ProvisionRequest

__all__ = ["_patch_stamp_ownership", "persistence"]


@pytest.mark.parametrize("legacy_record", [False, True])
@pytest.mark.parametrize("has_handle", [False, True])
@pytest.mark.parametrize("observation", ["absent", "matching", "mismatched"])
def test_pending_profile_is_reconciled_without_another_save(
    monkeypatch, mock_hmc, persistence, has_handle, observation, legacy_record
):
    _hmc_env(monkeypatch)
    _mock_preconditions(mock_hmc)
    _mock_execution_steps(mock_hmc)
    mock_hmc.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn")
    found = None

    async def pending(hmc, lpar_name_or_uuid, name, *, system_name_or_uuid, **kwargs):
        persistence.saved.append(name)
        job = (
            JobOutcome(
                "pending-save",
                "RUNNING",
                True,
                None,
                None,
                True,
                "/rest/api/uom/jobs/pending-save",
            )
            if has_handle
            else None
        )
        return PartitionProfileOutcome(
            "save",
            system_name_or_uuid,
            lpar_name_or_uuid,
            "LogicalPartition",
            name,
            None,
            job=job,
        )

    async def by_name(hmc, lpar_uuid, system_uuid, name):
        if found is None:
            return None
        return SimpleNamespace(uuid=PROFILE_UUID, name=name, element=found)

    monkeypatch.setattr(operation, "save_current_partition_profile", pending)
    monkeypatch.setattr(operation.profile_write, "profile_by_name", by_name)
    request = _provision_args(request_id="pending-profile")
    first = hmc_provision_lpar(**request)
    effect = next(e for e in first.operation.effects if e.key == "profile")
    initial_status = effect.status
    assert first.operation.outcome == "needs_attention"
    assert persistence.defaults == []

    if legacy_record:
        # Historical fixtures carry the pre-upgrade uncertainty misclassification.
        with store.session() as conn, store.write_transaction(conn):
            store.record_outcome(
                conn, first.operation.operation_id, "profile", "not_applied", None
            )

    if observation != "absent":
        found = profile()
        resources = request["resources"]
        for path, value in {
            "ProfileMemory/MinimumMemory": resources.min_memory,
            "ProfileMemory/DesiredMemory": resources.desired_memory
            + (1 if observation == "mismatched" else 0),
            "ProfileMemory/MaximumMemory": resources.max_memory,
            "ProcessorAttributes/SharedProcessorConfiguration/DesiredVirtualProcessors": resources.desired_vcpus,
            "ProcessorAttributes/SharedProcessorConfiguration/DesiredProcessingUnits": resources.desired_procs,
        }.items():
            if value is not None:
                field(found, path).text = str(value)

    resumed = hmc_provision_lpar(
        request_id="pending-profile",
        continuation="resume",
        exclusive_writer_window=True,
    )
    effect = next(e for e in resumed.operation.effects if e.key == "profile")
    assert len(persistence.saved) == 1
    assert initial_status == "uncertain"
    if observation == "matching":
        assert effect.status == "applied"
        assert len(persistence.defaults) == 1
    else:
        assert effect.status == "uncertain"
        assert resumed.operation.outcome == "needs_attention"
        assert persistence.defaults == []


@pytest.mark.parametrize("stage", ["save", "readback", "legacy-readback"])
@pytest.mark.parametrize("error_type", [HMCError, ValueError, HMCTLSFingerprintError])
def test_profile_provider_failures_do_not_enter_public_or_stored_results(
    monkeypatch, mock_hmc, persistence, stage, error_type
):
    _hmc_env(monkeypatch)
    _mock_preconditions(mock_hmc)
    _mock_execution_steps(mock_hmc)
    marker = "session-marker-profile-save"
    failure = (
        error_type("profile read refused", 503, "<reply>" + marker + "</reply>")
        if issubclass(error_type, HMCError)
        else error_type(marker)
    )
    persistence.profile_present = False
    if stage == "legacy-readback":
        first = hmc_provision_lpar(**_provision_args(request_id="profile-privacy"))
        with store.session() as conn, store.write_transaction(conn):
            store.record_outcome(
                conn, first.operation.operation_id, "profile", "not_applied", None
            )
        monkeypatch.setattr(
            operation.profile_write, "profile_by_name", AsyncMock(side_effect=failure)
        )
        result = hmc_provision_lpar(
            request_id="profile-privacy",
            continuation="resume",
            exclusive_writer_window=True,
        )
        assert len(persistence.saved) == 1
    else:
        if stage == "save":
            monkeypatch.setattr(
                operation,
                "save_current_partition_profile",
                AsyncMock(side_effect=failure),
            )
        else:
            monkeypatch.setattr(
                operation.profile_write,
                "profile_by_name",
                AsyncMock(side_effect=failure),
            )
        result = hmc_provision_lpar(**_provision_args(request_id="profile-privacy"))
    assert marker not in repr(result)
    with store.session() as conn:
        recorded = store.read_record(conn, result.operation.operation_id)
    assert marker not in repr(recorded)
    assert result.operation.outcome == "needs_attention"
    assert persistence.defaults == []
    if error_type is HMCTLSFingerprintError:
        assert any(
            "TLS certificate identity" in str(step.result) for step in result.steps
        )
    elif error_type is HMCError:
        assert any("HTTP 503" in str(step.result) for step in result.steps)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["save", "readback"])
async def test_profile_uncertainty_preserves_cause_without_rendering_it(
    monkeypatch, stage
):
    failure = HMCError("provider read failed", 503, "session-marker-profile-cause")
    outcome = PartitionProfileOutcome(
        "save", SYSTEM_UUID, LPAR_UUID, "LogicalPartition", "profile-R1", None
    )
    save = (
        AsyncMock(side_effect=failure)
        if stage == "save"
        else AsyncMock(return_value=outcome)
    )
    read = (
        AsyncMock(side_effect=failure)
        if stage == "readback"
        else AsyncMock(return_value=None)
    )
    monkeypatch.setattr(operation, "save_current_partition_profile", save)
    monkeypatch.setattr(operation.profile_write, "profile_by_name", read)
    args = _provision_args()
    request = ProvisionRequest(
        **{key: value for key, value in args.items() if key != "system_name_or_uuid"}
    )
    run = operation._Run(SimpleNamespace(), SimpleNamespace(), SYSTEM_UUID, request)
    with pytest.raises(HMCError) as caught:
        await run._write_profile(SYSTEM_UUID, LPAR_UUID, "profile-R1")
    assert caught.value.__cause__ is failure
    assert "session-marker-profile-cause" not in str(caught.value)


@pytest.mark.parametrize(
    ("has_handle", "expected_status"), [(False, "not_applied"), (True, "uncertain")]
)
def test_only_a_consistent_explicit_save_rejection_proves_non_application(
    monkeypatch, mock_hmc, persistence, has_handle, expected_status
):
    _hmc_env(monkeypatch)
    _mock_preconditions(mock_hmc)
    _mock_execution_steps(mock_hmc)
    persistence.profile_present = False
    outcome = PartitionProfileOutcome(
        "save",
        SYSTEM_UUID,
        LPAR_UUID,
        "LogicalPartition",
        "profile-R1",
        None,
        execution="rejected",
        job=JobOutcome(
            "pending-save",
            "RUNNING",
            True,
            None,
            None,
            True,
            "/rest/api/uom/jobs/pending-save",
        )
        if has_handle
        else None,
    )
    save = AsyncMock(return_value=outcome)
    monkeypatch.setattr(operation, "save_current_partition_profile", save)
    result = hmc_provision_lpar(**_provision_args())
    profile_effect = next(e for e in result.operation.effects if e.key == "profile")
    assert profile_effect.status == expected_status
    assert result.operation.outcome == "needs_attention"
    save.assert_awaited_once()
    assert persistence.defaults == []
