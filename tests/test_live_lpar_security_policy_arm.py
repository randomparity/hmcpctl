"""Bounded C04 arm admission, identity, failure and cleanup contracts."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_security_policy as arm


@pytest.fixture
def setup_arm(monkeypatch):
    @asynccontextmanager
    async def session(config):
        yield object()

    monkeypatch.setattr(arm, "HMCClient", session)
    capabilities = AsyncMock(return_value=SimpleNamespace(secure_boot=True))
    monkeypatch.setattr(arm, "read_security_policy_capabilities", capabilities)
    pools = AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    monkeypatch.setattr(arm, "_read_pools", pools)
    token = AsyncMock(side_effect=lambda client, state, run, target: run.token)
    monkeypatch.setattr(arm, "_token_of", token)
    cleaned = []

    async def teardown(client, state, run, **kwargs):
        assert kwargs == {"inactive_only": True}
        cleaned.append(run)
        run.deleted = True

    cleanup = AsyncMock(side_effect=teardown)
    monkeypatch.setattr(arm, "_teardown", cleanup)
    compare = AsyncMock(return_value=True)
    monkeypatch.setattr(arm, "_compare", compare)
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )
    before = {
        "environment": "AIX/Linux",
        "state": "not activated",
        "settings": {"secure_boot": 0},
        "current_secure_boot": 0,
    }
    after = {**before, "settings": {"secure_boot": 1}}
    responses = [
        (
            "PASS",
            {
                "lpar": {"UUID": "fixture"},
                "ownership_stamped": True,
                "workflow_completed": True,
                "steps": [{"step": "security_policy", "status": "ok"}],
            },
        ),
        ("PASS", before),
        ("PASS", {"status": "verified", "observed": after}),
        ("PASS", after),
    ]
    state.call = AsyncMock(side_effect=responses)
    return SimpleNamespace(
        state=state,
        responses=responses,
        capabilities=capabilities,
        pools=pools,
        token=token,
        cleanup=cleanup,
        compare=compare,
        cleaned=cleaned,
    )


@pytest.mark.asyncio
async def test_opt_in_precedes_all_reads(setup_arm):
    env = setup_arm
    env.state.group = "all"
    await arm.exercise_lpar_security_policy(object(), env.state)
    env.state.skip.assert_called_once()
    env.capabilities.assert_not_awaited()
    env.pools.assert_not_awaited()
    env.state.call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "baseline", [None, (SimpleNamespace(names=["hmcpctl-live-lpar-stranded"]), 256)]
)
async def test_baseline_refusal_has_no_effects(setup_arm, baseline):
    env = setup_arm
    env.pools.return_value = baseline
    await arm.exercise_lpar_security_policy(object(), env.state)
    env.state.skip.assert_called_once()
    env.state.call.assert_not_awaited()
    env.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_positive_capability_required_before_baseline(setup_arm):
    env = setup_arm
    env.capabilities.return_value.secure_boot = False
    await arm.exercise_lpar_security_policy(object(), env.state)
    env.state.skip.assert_called_once()
    env.pools.assert_not_awaited()
    env.state.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_update_exact_owned_fixture_and_independent_reads(setup_arm):
    env = setup_arm
    await arm.exercise_lpar_security_policy(object(), env.state)
    calls = env.state.call.await_args_list
    assert [call.args[1] for call in calls] == [
        "hmc_create_lpar",
        "hmc_get_lpar_security_policy",
        "hmc_configure_lpar_security_policy",
        "hmc_get_lpar_security_policy",
    ]
    run = env.cleaned[0]
    assert calls[0].kwargs == {
        "system_name_or_uuid": "system",
        "name": run.name,
        "partition_type": "AIX/Linux",
        "resources": arm.RESOURCES,
        "caller_token": run.token,
        "security_policy": {"secure_boot": 0},
        "apply_partition_profile": False,
    }
    assert run.name.removeprefix(arm.NAME_PREFIX) == run.token.removeprefix(
        arm.TOKEN_PREFIX
    )
    assert len(run.name.removeprefix(arm.NAME_PREFIX)) == 8
    assert run.uuid == "fixture" and run.create_attempted
    for call in calls[1:]:
        assert call.kwargs["lpar_name_or_uuid"] == run.uuid
        assert call.kwargs["system_name_or_uuid"] == run.system
    assert calls[2].kwargs["settings"] == {"secure_boot": 1}
    env.token.assert_awaited_once()
    assert env.token.await_args.args[-1] == run.uuid
    env.cleanup.assert_awaited_once()
    env.compare.assert_awaited_once()
    assert all(
        c.kwargs["cleanup"] == "passed"
        for c in env.state.record_verified.call_args_list
    )
    assert all(
        c.kwargs["assertions"][0].holds
        for c in env.state.record_verified.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "create-error",
        "uuid",
        "stamp",
        "workflow",
        "steps",
        "step-error",
        "read-error",
        "pending",
        "current",
        "active",
        "environment",
        "token",
        "mismatch",
        "readback-unavailable",
        "write-outcome-unknown",
        "observed",
        "current-drift",
        "postread-drift",
        "postread-error",
    ],
)
async def test_failures_stop_dependent_effects_and_cleanup(setup_arm, failure):
    env = setup_arm
    create = env.responses[0][1]
    expected_calls = 1
    if failure == "create-error":
        env.responses[0] = ("FAIL", "refused")
    elif failure == "uuid":
        create["lpar"] = {}
    elif failure in {"stamp", "workflow"}:
        create[
            {"stamp": "ownership_stamped", "workflow": "workflow_completed"}[failure]
        ] = False
    elif failure == "steps":
        create["steps"] = None
    elif failure == "step-error":
        create["steps"][0]["status"] = "error"
    elif failure == "token":
        env.token.side_effect = None
        env.token.return_value = "foreign"
        expected_calls = 2
    elif failure in {"read-error", "pending", "current", "active", "environment"}:
        before = env.responses[1][1]
        expected_calls = 2
        if failure == "read-error":
            env.responses[1] = ("FAIL", "unavailable")
        elif failure == "pending":
            before["settings"]["secure_boot"] = 1
        elif failure == "current":
            before["current_secure_boot"] = None
        elif failure == "active":
            before["state"] = "Running"
        else:
            before["environment"] = "os400"
    else:
        expected_calls = 3
        result = env.responses[2][1]
        if failure in {"mismatch", "readback-unavailable", "write-outcome-unknown"}:
            result["status"] = failure
        elif failure == "observed":
            result["observed"] = None
        elif failure == "current-drift":
            result["observed"]["current_secure_boot"] = 1
        else:
            expected_calls = 4
            if failure == "postread-drift":
                env.responses[3] = ("PASS", {})
            else:
                env.responses[3] = ("FAIL", "unavailable")
    env.state.call.side_effect = env.responses
    await arm.exercise_lpar_security_policy(object(), env.state)
    assert env.state.call.await_count == expected_calls
    env.state.record.assert_called_once()
    env.cleanup.assert_awaited_once()
    env.compare.assert_awaited_once()
    assert not env.state.record_verified.call_args_list[1].kwargs["assertions"][0].holds


def test_registration_and_independent_recovery():
    import live_test_preflight as preflight
    import live_test_recovery as recovery
    from live_test import runner

    assert runner.SUBTASK_GROUPS[arm.GROUP] == [52]
    assert runner.SUBTASKS[52] is arm.exercise_lpar_security_policy
    assert 52 not in runner.SUBTASK_GROUPS["all"]
    assert arm.GROUP in preflight._ARM_VERDICTS
    assert 52 in recovery._WITNESSED_SUBTASKS
    inputs = recovery.lpar_config_inputs_from_document(
        {"config": {"system_name": "system"}}, [52]
    )
    assert inputs is not None and inputs.system_name == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("invalid"), PermissionError("denied")])
async def test_creation_exception_still_cleans_and_compares(setup_arm, error):
    env = setup_arm
    env.state.call.side_effect = error
    await arm.exercise_lpar_security_policy(object(), env.state)
    env.cleanup.assert_awaited_once()
    env.compare.assert_awaited_once()
    env.state.record.assert_called_once()


@pytest.mark.asyncio
async def test_baseline_difference_prevents_clean_evidence(setup_arm):
    env = setup_arm
    env.compare.return_value = False
    await arm.exercise_lpar_security_policy(object(), env.state)
    assert all(
        c.kwargs["cleanup"] == "failed"
        for c in env.state.record_verified.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["verified", "current-drift", "omitted-drift", "postread-drift"]
)
async def test_real_served_typed_policy_results(setup_arm, variant):
    import json
    from dataclasses import replace

    from fastmcp import Client, FastMCP
    from live_test.runner import RunState

    from hmcpctl.client.core import HMCClient
    from hmcpctl.documents.lpar_security_policy import (
        LparSecurityPolicy,
        SecurityPolicyResult,
        SecurityPolicyState,
    )
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.operations.lpar.security_policy import parse_security_policy
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    env = setup_arm
    app = FastMCP("policy-arm-test")
    body = json.loads(
        (Path(__file__).parent / "fixtures/live/rest-lpar-entry.json").read_text()
    )["body"]
    entry = HMCClient._first_entry(body, "LogicalPartition")
    initial = parse_security_policy(entry["Resource"])
    assert initial.environment == "AIX/Linux"
    assert initial.state == "not activated"
    observed = initial
    writes = []
    reads = []

    @app.tool(name="hmc_create_lpar")
    async def create(
        system_name_or_uuid: str,
        name: str,
        partition_type: str,
        resources: dict,
        caller_token: str,
        security_policy: LparSecurityPolicy,
        apply_partition_profile: bool,
    ) -> LparPcieWorkflowResult:
        assert security_policy == LparSecurityPolicy(0)
        assert not apply_partition_profile
        return LparPcieWorkflowResult(
            True,
            True,
            {"UUID": "fixture"},
            True,
            (WorkflowStep("security_policy", "ok"),),
            (),
        )

    @app.tool(name="hmc_get_lpar_security_policy")
    async def read(
        system_name_or_uuid: str, lpar_name_or_uuid: str
    ) -> SecurityPolicyState:
        reads.append(observed)
        if len(reads) == 2 and variant == "postread-drift":
            return initial
        return observed

    @app.tool(name="hmc_configure_lpar_security_policy")
    async def configure(
        system_name_or_uuid: str, lpar_name_or_uuid: str, settings: LparSecurityPolicy
    ) -> SecurityPolicyResult:
        nonlocal observed
        writes.append(settings)
        observed = replace(
            initial,
            settings=replace(
                initial.settings,
                secure_boot=1,
                keystore_kbytes=1
                if variant == "omitted-drift"
                else initial.settings.keystore_kbytes,
            ),
            current_secure_boot=1
            if variant == "current-drift"
            else initial.current_secure_boot,
        )
        return SecurityPolicyResult(
            "verified", settings, observed, ("secure_boot",), ()
        )

    state = RunState(config=env.state.config, group=arm.GROUP, hmc_config=object())
    state.record = Mock()
    state.record_verified = Mock()
    async with Client(app) as client:
        await arm.exercise_lpar_security_policy(client, state)
    assert writes == [LparSecurityPolicy(1)]
    assert len(reads) == (2 if variant in {"verified", "postread-drift"} else 1)
    assert state.record_verified.call_args_list[1].kwargs["assertions"][0].holds is (
        variant == "verified"
    )
    env.cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign,active", [(True, False), (False, True), (False, False)]
)
async def test_real_guarded_cleanup_exact_token_and_inactive_only(
    monkeypatch, foreign, active
):
    from live_test import lpar_config

    run = lpar_config._Run(
        "system",
        "hmcpctl-live-lpar-a1b2c3d4",
        "lparcfg-a1b2c3d4",
        256,
        uuid="fixture",
        created=True,
        create_attempted=True,
        subtask=52,
    )
    monkeypatch.setattr(
        lpar_config,
        "_lpar_state",
        AsyncMock(return_value="running" if active else "not activated"),
    )
    monkeypatch.setattr(
        lpar_config,
        "_token_of",
        AsyncMock(return_value="foreign" if foreign else run.token),
    )
    monkeypatch.setattr(lpar_config, "_gone", AsyncMock(return_value=True))
    state = SimpleNamespace(call=AsyncMock(return_value=("PASS", {})), record=Mock())
    await lpar_config._teardown(object(), state, run, inactive_only=True)
    if foreign or active:
        state.call.assert_not_awaited()
        assert not run.deleted
        assert state.record.call_args.args[2] == "FAIL"
    else:
        state.call.assert_awaited_once()
        assert state.call.await_args.args[1] == "hmc_delete_lpar"
        assert state.call.await_args.kwargs == {
            "system_name_or_uuid": "system",
            "lpar_name_or_uuid": "fixture",
        }
        assert run.deleted


@pytest.mark.asyncio
async def test_default_runner_does_not_dispatch_new_arm(monkeypatch, tmp_path):
    from live_test import config, runner

    from hmcpctl.config import HMCConfig

    @asynccontextmanager
    async def session():
        yield object()

    ordinary = AsyncMock()
    policy = AsyncMock()
    monkeypatch.setattr(runner, "SUBTASKS", {0: ordinary, 52: policy})
    monkeypatch.setattr(runner, "served_client", session)
    monkeypatch.setattr(runner, "served_schemas", AsyncMock(return_value={}))
    monkeypatch.setattr(runner.evidence, "repository_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    result = await runner.main(
        None,
        str(tmp_path / "results.json"),
        config=config.LiveTestConfig(system_name="system"),
        hmc_config=HMCConfig.from_mapping({}),
    )
    assert result == 0
    ordinary.assert_awaited_once()
    policy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing,clean",
    [
        ("unrelated,Running\n", True),
        ("hmcpctl-live-lpar-a1b2c3d4,Not Activated\n", False),
        (None, False),
    ],
)
async def test_independent_recovery_confirms_absence_or_refuses_clean(listing, clean):
    import live_test_recovery as recovery

    inputs = recovery.lpar_config_inputs_from_document(
        {"config": {"system_name": "system"}}, [52]
    )
    calls = []

    async def call(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        calls.append((tool, arguments))
        return ("PASS", listing) if listing is not None else ("FAIL", None)

    if listing is None:
        with pytest.raises(recovery.StateUnreadable):
            await recovery.check_lpar_config(call, inputs)
    else:
        findings = await recovery.check_lpar_config(call, inputs)
        assert (not findings) is clean
    assert calls == [
        ("hmc_run_command", {"cmd": "lssyscfg -r lpar -m system -F name,state"})
    ]
