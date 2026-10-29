"""ST66 exercises actual dispatch; fixtures never assert runtime availability."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import Client
from lpar.test_paging_observation import (  # noqa: F401 - imported boundary fixture
    PRIMARY,
    SECONDARY,
    SYSTEM,
    TARGET,
    paging_boundary,
)
from lpar.test_paging_switchover import attempt  # noqa: F401 - imported boundary fixture

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import evidence, runner
from live_test import paging_switchover as arm
from live_test.config import LiveTestConfig

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle


@pytest.fixture
def live_boundary(request, monkeypatch):
    boundary = request.getfixturevalue("attempt")
    root = Path(__file__).parents[1]

    def git(_root, *args):
        value = str(root) if args == ("rev-parse", "--show-toplevel") else ""
        if args == ("rev-parse", "HEAD"):
            value = "c" * 40
        elif args == ("rev-parse", "HEAD^{tree}"):
            value = "d" * 40
        return SimpleNamespace(returncode=0, stdout=value)

    monkeypatch.setattr(evidence, "_git", git)
    boundary.description.return_value = None
    boundary.description.side_effect = lambda *a, **kw: (
        "[hmcpctl owner:hmcpctl created:2026-01-01]"
    )
    monkeypatch.setattr(arm, "get_lpar_description", boundary.description)

    @asynccontextmanager
    async def hmc_client(_config):
        yield boundary.hmc

    monkeypatch.setattr(arm, "HMCClient", hmc_client)
    monkeypatch.setattr(
        lifecycle, "with_client", lambda fn, **kw: asyncio.run(fn(boundary.hmc))
    )
    state = runner.RunState(
        config=LiveTestConfig(
            system_name=SYSTEM,
            lp3_name="consumer",
            paging_expected_current_vios=SECONDARY,
            paging_writer_window=True,
        ),
        group=arm.GROUP,
        hmc_config=boundary.hmc.config,
        checkpoint=Mock(),
    )
    return boundary, state


async def exercise(pair):
    _, state = pair
    application = create_mcp(
        compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    )
    async with Client(application) as client:
        await arm.exercise_paging_switchover(client, state)
    return json.loads(
        json.dumps(
            {
                "run": evidence.run_provenance(
                    [66],
                    arm.GROUP,
                    evidence.repository_root(),
                    state.hmc_config.schema_version,
                    False,
                ),
                "config": asdict(state.config),
                "hmc": runner._hmc_identity(state.hmc_config),
                "artifacts": asdict(state.artifacts),
                "results": state.results,
            }
        )
    )


def test_only_named_group():
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [66]
    assert runner.SUBTASKS[66] is arm.exercise_paging_switchover
    assert 66 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.parametrize(
    "case", ["missing", "example", "protected", "expected", "window"]
)
def test_fixture_admission(case):
    args = {
        "lp3_name": "consumer",
        "paging_expected_current_vios": SECONDARY,
        "paging_writer_window": True,
    }
    if case == "missing":
        args["lp3_name"] = ""
    elif case == "example":
        args["lp3_name"] = "example-consumer"
    elif case == "protected":
        args["protected_lpar_names"] = ("consumer",)
    elif case == "expected":
        args["paging_expected_current_vios"] = ""
    else:
        args["paging_writer_window"] = False
    with pytest.raises(ValueError):
        arm.configured_paging_fixture(LiveTestConfig(**args))


@pytest.mark.asyncio
async def test_one_attempt_checkpoint_and_changed_current_held(live_boundary):
    boundary, state = live_boundary
    submit = boundary.submit.side_effect

    async def checkpointed(*args, **kwargs):
        assert state.checkpoint.call_count >= 2
        assert [row["tool"] for row in state.results] == list(arm.PHASES[:2])
        return await submit(*args, **kwargs)

    boundary.submit.side_effect = checkpointed
    document = await exercise(live_boundary)
    boundary.submit.assert_awaited_once()
    assert state.checkpoint.call_count >= 2
    baseline = document["results"][0]["data"]
    assert (
        baseline["candidate_commit"] == "c" * 40
        and baseline["deployed_tree"] == "d" * 40
    )
    assert baseline["before"]["current_native"]["uuid"] == SECONDARY
    assert any(
        row["tool"] == arm.PHASES[2] and row["data"]["workflow_completed"] is True
        for row in state.results
    )
    assert not any(row["result"] == "passed" for row in state.results)
    original = deepcopy(document)
    recovery = await arm.recover_document(state.hmc_config, document)
    assert (
        recovery["status"] == "drift" and "current-provider" in recovery["diagnostics"]
    )
    assert document == original
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_restoration_is_separate_readonly_confirmation(live_boundary):
    boundary, state = live_boundary
    document = await exercise(live_boundary)
    boundary.rows.clear()
    boundary.rows.update(deepcopy(boundary.original))
    boundary.current.return_value = boundary.current.return_value.replace(
        "paging-a", "paging-b"
    )
    recovery = await arm.recover_document(state.hmc_config, document)
    assert recovery["status"] == "clean"
    boundary.submit.assert_awaited_once()
    assert not any(row["result"] == "passed" for row in document["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["checkpoint", "owner", "protected-provider", "stale", "dirty", "wrong-group"],
)
async def test_before_effect_refusals(live_boundary, monkeypatch, case):
    boundary, state = live_boundary
    if case == "checkpoint":
        state.checkpoint.side_effect = OSError("private output unavailable")
    elif case == "owner":
        boundary.description.side_effect = lambda *a, **kw: ""
    elif case == "protected-provider":
        state.config = LiveTestConfig(
            system_name=SYSTEM,
            lp3_name="consumer",
            paging_expected_current_vios=SECONDARY,
            paging_writer_window=True,
            protected_lpar_names=("paging-a",),
        )
    elif case == "stale":
        state.config = LiveTestConfig(
            system_name=SYSTEM,
            lp3_name="consumer",
            paging_expected_current_vios=PRIMARY,
            paging_writer_window=True,
        )
    elif case == "dirty":
        old = evidence._git
        monkeypatch.setattr(
            evidence,
            "_git",
            lambda root, *args: (
                SimpleNamespace(returncode=0, stdout=" M owned.py")
                if args[0] == "status"
                else old(root, *args)
            ),
        )
    else:
        state.group = "all"
    await exercise(live_boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-baseline",
        "duplicate",
        "candidate",
        "tree",
        "destination",
        "config",
        "malformed",
        "unknown-field",
    ],
)
async def test_recovery_rejects_unbound_record(live_boundary, case):
    boundary, state = live_boundary
    doc = await exercise(live_boundary)
    baseline = doc["results"][0]["data"]
    if case == "missing-baseline":
        doc["results"].pop(0)
    elif case == "duplicate":
        doc["results"].insert(0, deepcopy(doc["results"][0]))
    elif case == "candidate":
        doc["run"]["tested_commit"] = "f" * 40
    elif case == "tree":
        baseline["deployed_tree"] = "bad"
    elif case == "destination":
        doc["hmc"]["host"] = "foreign.test"
    elif case == "config":
        doc["config"]["lp3_name"] = "unrelated"
    elif case == "malformed":
        baseline["before"]["target"]["partition_id"] = True
    else:
        baseline["before"]["unexpected"] = True
    before = boundary.hmc._get.await_count
    with pytest.raises((ValueError, TypeError)):
        await arm.recover_document(state.hmc_config, doc)
    assert boundary.hmc._get.await_count == before
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_attempt_can_observe_but_cannot_confirm_clean(live_boundary):
    boundary, state = live_boundary
    doc = await exercise(live_boundary)
    boundary.rows.clear()
    boundary.rows.update(deepcopy(boundary.original))
    boundary.current.return_value = boundary.current.return_value.replace(
        "paging-a", "paging-b"
    )
    doc["run"]["partial"] = True
    result = await arm.recover_document(state.hmc_config, doc)
    assert result["status"] == "unverified"
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_after_effect_still_observes_and_never_replays(live_boundary):
    boundary, state = live_boundary
    boundary.error = OSError("transport lost")
    await exercise(live_boundary)
    assert boundary.current.await_count >= 3
    boundary.submit.assert_awaited_once()
    assert not any(row["result"] == "passed" for row in state.results)


@pytest.mark.asyncio
async def test_surviving_cancellation_keeps_original_and_independent_read(
    live_boundary,
):
    boundary, state = live_boundary
    original_call = state.call

    async def interrupted(*args, **kwargs):
        await original_call(*args, **kwargs)
        raise asyncio.CancelledError

    state.call = interrupted
    with pytest.raises(asyncio.CancelledError):
        await exercise(live_boundary)
    assert [row["tool"] for row in state.results] == [
        arm.PHASES[0],
        arm.PHASES[1],
        arm.PHASES[3],
    ]
    document = {
        "run": evidence.run_provenance(
            [66],
            arm.GROUP,
            evidence.repository_root(),
            state.hmc_config.schema_version,
            True,
        ),
        "config": json.loads(json.dumps(asdict(state.config))),
        "hmc": runner._hmc_identity(state.hmc_config),
        "results": json.loads(json.dumps(state.results)),
    }
    result = await arm.recover_document(state.hmc_config, document)
    assert result["status"] == "drift"
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["shared", "redundant", "dedicated", "pair", "route-id"]
)
async def test_saved_baseline_must_be_positive_before_read(live_boundary, case):
    boundary, state = live_boundary
    doc = await exercise(live_boundary)
    before = doc["results"][0]["data"]["before"]
    if case == "shared":
        before["shared"] = False
    elif case == "redundant":
        before["native_redundant"] = False
    elif case == "dedicated":
        before["provider_dedicated"][0] = False
    elif case == "pair":
        before["configured"]["secondary"] = before["configured"]["primary"]
    else:
        before["target"]["uuid"] = "invalid"
    reads = boundary.hmc._get.await_count
    with pytest.raises(ValueError):
        await arm.recover_document(state.hmc_config, doc)
    assert boundary.hmc._get.await_count == reads


@pytest.mark.asyncio
async def test_unknown_unchanged_attempt_is_not_recovery_completion(live_boundary):
    from hmcpctl.ssh.transport import HMCCLIError

    boundary, state = live_boundary
    boundary.change = False
    boundary.error = HMCCLIError("unknown remote completion")
    document = await exercise(live_boundary)
    result = await arm.recover_document(state.hmc_config, document)
    assert result["status"] == "unverified"
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["pool", "provider"])
@pytest.mark.parametrize("drift", [False, True])
async def test_readonly_recovery_missing_reader_distinguishes_drift(
    live_boundary, missing, drift
):
    from hmcpctl.errors import HMCError
    from hmcpctl.ssh.transport import HMCCLIError

    boundary, state = live_boundary
    boundary.change = False
    boundary.error = HMCCLIError("unknown completion")
    document = await exercise(live_boundary)
    get = boundary.hmc._get.side_effect
    if drift:
        boundary.rows["LogicalPartition"] = boundary.rows["LogicalPartition"].replace(
            "<PartitionState>running", "<PartitionState>not activated"
        )

    async def unavailable(path, *args, **kwargs):
        if (
            path.endswith("/SharedMemoryPool")
            if missing == "pool"
            else path.lower().endswith(PRIMARY)
        ):
            raise HMCError("read unavailable")
        return await get(path, *args, **kwargs)

    boundary.hmc._get.side_effect = unavailable
    before = deepcopy(document)
    result = await arm.recover_document(state.hmc_config, document)
    assert result["status"] == ("drift" if drift else "unverified")
    assert result["after"]["unavailable"]
    assert set(result["after"]["invalid"]) == (
        {"consumer-not-running"} if drift else set()
    )
    assert document == before
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["link", "containment"])
@pytest.mark.parametrize("change", ["unchanged", "redundancy", "device"])
async def test_partial_pool_readonly_recovery_retains_drift(
    live_boundary, missing, change
):
    from hmcpctl.ssh.transport import HMCCLIError

    boundary, state = live_boundary
    boundary.change = False
    boundary.error = HMCCLIError("unknown completion")
    document = await exercise(live_boundary)
    boundary.partial_pool_response(missing, change)
    original = deepcopy(document)
    result = await arm.recover_document(state.hmc_config, document)
    assert result["status"] == ("unverified" if change == "unchanged" else "drift")
    assert result["after"]["unavailable"]
    assert result["after"]["uom_redundant"] is (change != "redundancy")
    assert result["after"]["unique_device_id"] == (
        "device-two" if change == "device" else "device-one"
    )
    assert document == original
    boundary.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("missing", [False, True])
async def test_known_member_readonly_recovery_precedes_completeness(
    live_boundary, changed, missing
):
    from hmcpctl.ssh.transport import HMCCLIError

    boundary, state = live_boundary
    boundary.change = False
    boundary.error = HMCCLIError("unknown completion")
    document = await exercise(live_boundary)
    replacement = boundary.pool_member_response(changed, missing)
    original = deepcopy(document)
    result = await arm.recover_document(state.hmc_config, document)
    assert result["status"] == ("drift" if changed else "unverified")
    assert (replacement in {p["uuid"] for p in result["after"]["providers"]}) is changed
    assert bool(result["after"]["unavailable"]) is missing
    assert document == original
    boundary.submit.assert_awaited_once()
