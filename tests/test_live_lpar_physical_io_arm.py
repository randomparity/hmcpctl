"""C12 opt-in live arm, with no hardware in unit tests."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP

from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_physical_io as arm


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True, "malformed"])
async def test_arm_always_tears_down_and_compares(monkeypatch, failure):
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )
    state.call = AsyncMock(
        return_value=(
            "FAIL" if failure is True else "PASS",
            {
                "lpar": {"UUID": "new"},
                "ownership_stamped": True,
                "workflow_completed": True,
                "steps": None
                if failure == "malformed"
                else [{"step": "physical_io", "status": "ok"}],
            },
        )
    )
    monkeypatch.setattr(
        arm, "_read_pools", AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    )

    async def teardown(client, state, run, **kwargs):
        run.deleted = True

    cleanup = AsyncMock(side_effect=teardown)
    compare = AsyncMock(return_value=True)
    monkeypatch.setattr(arm, "_teardown", cleanup)
    monkeypatch.setattr(arm, "_compare", compare)
    await arm.exercise_lpar_physical_io(object(), state)
    cleanup.assert_awaited_once()
    compare.assert_awaited_once()
    args = state.call.await_args.kwargs
    assert args["partition_type"] == "OS400"
    assert args["physical_io"] == {"console_slot": "hmc"}
    assert args["apply_partition_profile"] is False
    assert state.record_verified.call_args.kwargs["assertions"][0].holds is (
        not failure
    )


@pytest.mark.asyncio
async def test_arm_requires_explicit_group():
    state = SimpleNamespace(group="round2", hmc_config=object(), skip=Mock())
    await arm.exercise_lpar_physical_io(object(), state)
    state.skip.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_arm_reads_actual_served_typed_result(monkeypatch, verified):
    from live_test.runner import RunState

    app = FastMCP("creation-result-test")

    @app.tool(name="hmc_create_lpar")
    async def create(
        system_name_or_uuid: str,
        name: str,
        partition_type: str,
        resources: dict,
        caller_token: str,
        physical_io: dict,
        apply_partition_profile: bool,
    ) -> LparPcieWorkflowResult:
        return LparPcieWorkflowResult(
            True,
            verified,
            {"UUID": "fixture"},
            True,
            (
                WorkflowStep(
                    "physical_io",
                    "ok" if verified else "error",
                    "console-readback-mismatch" if not verified else None,
                ),
            ),
            (),
        )

    state = RunState(
        config=SimpleNamespace(system_name="system"),
        group=arm.GROUP,
        hmc_config=object(),
    )
    state.record = Mock()
    state.record_verified = Mock()
    monkeypatch.setattr(
        arm, "_read_pools", AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    )

    async def teardown(client, state, run, **kwargs):
        run.deleted = True

    cleanup = AsyncMock(side_effect=teardown)
    compare = AsyncMock(return_value=True)
    monkeypatch.setattr(arm, "_teardown", cleanup)
    monkeypatch.setattr(arm, "_compare", compare)
    async with Client(app) as client:
        await arm.exercise_lpar_physical_io(client, state)
    cleanup.assert_awaited_once()
    compare.assert_awaited_once()
    failures = [
        call.args[3] for call in state.record.call_args_list if call.args[2] == "FAIL"
    ]
    assert state.record_verified.call_args.kwargs["assertions"][0].holds is verified
    assert state.record_verified.call_args.kwargs["cleanup"] == "passed"
    if verified:
        assert failures == []
    else:
        assert any("console-readback-mismatch" in failure for failure in failures)
