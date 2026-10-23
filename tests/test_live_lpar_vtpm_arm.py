"""C03 opt-in arm bounds and cleanup without hardware."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_vtpm as arm


@pytest.mark.asyncio
@pytest.mark.parametrize("creation", ["ok", "missing", "error", "malformed"])
async def test_creation_barrier_and_bounded_updates_always_cleanup(
    monkeypatch, creation
):
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )
    steps = [] if creation == "missing" else [{"step": "vtpm", "status": creation}]
    state.call = AsyncMock(
        side_effect=[
            (
                "PASS",
                {
                    "Resource": {
                        "SystemCapabilities": {
                            "VirtualTrustedPlatformModuleCapable": "true"
                        }
                    }
                },
            ),
            (
                "PASS",
                {
                    "lpar": {"UUID": "fixture"},
                    "ownership_stamped": True,
                    "workflow_completed": True,
                    "steps": None if creation == "malformed" else steps,
                },
            ),
            (
                "PASS",
                {
                    "state": "Not Activated",
                    "settings": {"vtpm_enabled": True, "vtpm_version": "1.2"},
                },
            ),
            ("PASS", {"status": "verified"}),
            ("PASS", {"status": "verified"}),
        ]
    )
    monkeypatch.setattr(
        arm, "get_vtpm_advertisement", AsyncMock(return_value=(("1.2",), ()))
    )
    monkeypatch.setattr(
        arm, "_read_pools", AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    )

    async def teardown(client, state, run, **kwargs):
        assert kwargs == {"inactive_only": True}
        run.deleted = True

    cleanup = AsyncMock(side_effect=teardown)
    compare = AsyncMock(return_value=True)
    monkeypatch.setattr(arm, "_teardown", cleanup)
    monkeypatch.setattr(arm, "_compare", compare)
    await arm.exercise_lpar_vtpm(object(), state)
    cleanup.assert_awaited_once()
    compare.assert_awaited_once()
    calls = state.call.await_args_list
    assert len(calls) == (5 if creation == "ok" else 2)
    assert calls[1].kwargs["apply_partition_profile"] is False
    assert calls[1].kwargs["vtpm_settings"] == {
        "vtpm_enabled": True,
        "vtpm_version": "1.2",
    }
    if creation == "ok":
        assert calls[3].kwargs["acknowledge_data_loss"] is True
        assert calls[3].kwargs["settings"] == {"vtpm_enabled": False}
        assert calls[4].kwargs["settings"] == {
            "vtpm_enabled": True,
            "vtpm_version": "1.2",
        }
    assert state.record_verified.call_args.kwargs["assertions"][0].holds is (
        creation == "ok"
    )


@pytest.mark.asyncio
async def test_group_opt_in_precedes_all_reads():
    state = SimpleNamespace(group="round2", hmc_config=object(), skip=Mock())
    await arm.exercise_lpar_vtpm(object(), state)
    state.skip.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "versions,encryptions", [((), ()), (("3.0",), ()), (("2.0",), ())]
)
async def test_unsupported_variant_skips_before_baseline_or_effects(
    monkeypatch, versions, encryptions
):
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
    )
    monkeypatch.setattr(
        arm, "get_vtpm_advertisement", AsyncMock(return_value=(versions, encryptions))
    )
    pools = AsyncMock()
    monkeypatch.setattr(arm, "_read_pools", pools)
    await arm.exercise_lpar_vtpm(object(), state)
    state.skip.assert_called_once()
    pools.assert_not_awaited()


def test_registration_preflight_and_independent_recovery():
    import live_test_preflight as preflight
    import live_test_recovery as recovery
    from live_test import runner

    assert runner.SUBTASK_GROUPS[arm.GROUP] == [51]
    assert runner.SUBTASKS[51] is arm.exercise_lpar_vtpm
    assert arm.GROUP in preflight._ARM_VERDICTS
    assert 51 in recovery._WITNESSED_SUBTASKS
    inputs = recovery.lpar_config_inputs_from_document(
        {"config": {"system_name": "system"}}, [51]
    )
    assert inputs is not None and inputs.system_name == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("update_status", ["verified", "mismatch"])
async def test_real_served_typed_results_and_update_failure_stop(
    monkeypatch, update_status
):
    from fastmcp import Client, FastMCP
    from live_test.runner import RunState

    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings, VtpmState
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.operations.lpar.vtpm import VtpmResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    app = FastMCP("vtpm-arm-test")
    patches = []

    @app.tool(name="hmc_get_system")
    async def system(system_name_or_uuid: str) -> dict:
        return {
            "Resource": {
                "SystemCapabilities": {
                    "VirtualTrustedPlatformModuleCapable": "true",
                    "VirtualTrustedPlatformModule20Capable": "true",
                }
            }
        }

    @app.tool(name="hmc_create_lpar")
    async def create(
        system_name_or_uuid: str,
        name: str,
        partition_type: str,
        resources: dict,
        caller_token: str,
        vtpm_settings: dict,
        apply_partition_profile: bool,
    ) -> LparPcieWorkflowResult:
        return LparPcieWorkflowResult(
            True, True, {"UUID": "fixture"}, True, (WorkflowStep("vtpm", "ok"),), ()
        )

    @app.tool(name="hmc_get_lpar_vtpm_settings")
    async def read(system_name_or_uuid: str, lpar_name_or_uuid: str) -> VtpmState:
        return VtpmState(
            "aixlinux", "Not Activated", LparVtpmSettings(True, "2.0", "level")
        )

    @app.tool(name="hmc_configure_lpar_vtpm")
    async def update(
        system_name_or_uuid: str,
        lpar_name_or_uuid: str,
        settings: LparVtpmSettings,
        acknowledge_data_loss: bool,
    ) -> VtpmResult:
        patches.append(settings)
        return VtpmResult(update_status, settings, settings, ())

    state = RunState(
        config=SimpleNamespace(system_name="system"),
        group=arm.GROUP,
        hmc_config=object(),
    )
    state.record = Mock()
    state.record_verified = Mock()
    monkeypatch.setattr(
        arm, "get_vtpm_advertisement", AsyncMock(return_value=(("2.0",), ("level",)))
    )
    monkeypatch.setattr(
        arm, "_read_pools", AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    )

    async def teardown(client, state, run, **kwargs):
        run.deleted = True

    cleanup = AsyncMock(side_effect=teardown)
    monkeypatch.setattr(arm, "_teardown", cleanup)
    monkeypatch.setattr(arm, "_compare", AsyncMock(return_value=False))
    async with Client(app) as client:
        await arm.exercise_lpar_vtpm(client, state)
    assert len(patches) == (2 if update_status == "verified" else 1)
    cleanup.assert_awaited_once()
    observed = state.record_verified.call_args_list[1].kwargs
    assert observed["assertions"][0].holds is (update_status == "verified")
    assert observed["cleanup"] == "failed"
    assert observed["data"]["guest-tpm-objects"] == "not-run"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [None, False, 1, "false"])
async def test_missing_positive_general_capability_skips_before_baseline(
    monkeypatch, capability
):
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        call=AsyncMock(
            return_value=(
                "PASS",
                {
                    "Resource": {
                        "SystemCapabilities": {
                            "VirtualTrustedPlatformModuleCapable": capability
                        }
                    }
                },
            )
        ),
    )
    monkeypatch.setattr(
        arm, "get_vtpm_advertisement", AsyncMock(return_value=(("1.2",), ()))
    )
    pools = AsyncMock(return_value=None)
    monkeypatch.setattr(arm, "_read_pools", pools)
    await arm.exercise_lpar_vtpm(object(), state)
    pools.assert_not_awaited()
    assert state.call.await_args.args[1] == "hmc_get_system"
