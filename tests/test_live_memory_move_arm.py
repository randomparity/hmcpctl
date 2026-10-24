import asyncio
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import memory_move as arm
from live_test import runner
from live_test.config import LiveTestConfig
from lpar.test_memory_move import (
    SOURCE,
    TARGET,
    boundary,  # noqa: F401 — HTTP/SSH boundary fixture
    mem,
)

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError

CONFIG = HMCConfig.from_mapping({})


@pytest.mark.parametrize(
    "source,target",
    [("", "target-R2"), ("same", "same"), ("example-source", "target-R2")],
)
def test_configured_refuses_missing_or_default(source, target):
    config = LiveTestConfig(lp3_name=source, scratch_name=target)
    with pytest.raises(ValueError):
        arm.configured(config)


def test_only_named_group_registers_arm():
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [54]
    assert runner.SUBTASKS[54] is arm.exercise_memory_move
    assert 54 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.asyncio
async def test_oracle_attempts_both_and_rejects_malformed(monkeypatch):
    peers = [{"uuid": SOURCE, "partition_id": 7}, {"uuid": TARGET, "partition_id": 8}]
    command = AsyncMock(side_effect=["bad", "8,1024,2048,4096,2048,ded"])
    monkeypatch.setattr(arm, "run_hmc_command", command)
    result = await arm.oracle(SimpleNamespace(config=CONFIG), "sys-R1", peers)
    assert result[0]["error"] and result[1]["values"] == [8, 1024, 2048, 4096, 2048]
    assert command.await_count == 2


@pytest.fixture
def live_boundary(monkeypatch, boundary):  # noqa: F811 — pytest injects the imported fixture
    hmc = boundary.hmc
    initial = deepcopy(boundary.before)

    @asynccontextmanager
    async def client(config):
        yield hmc

    monkeypatch.setattr(arm, "HMCClient", client)
    monkeypatch.setattr(arm, "get_lpar_description", boundary.description)

    async def cli(config, command):
        if "--level lpar" in command:
            uuid = SOURCE if "lpar_ids=7 " in command else TARGET
            resource = boundary.before[uuid]["Resource"]
            memory = mem(boundary.before[uuid])
            values = [
                resource["PartitionID"],
                *[
                    memory[key]
                    for key in (
                        "CurrentMinimumMemory",
                        "CurrentMemory",
                        "CurrentMaximumMemory",
                        "RuntimeMemory",
                    )
                ],
            ]
            return ",".join(str(value) for value in values) + ",ded"
        if "-r lpar" in command:
            return "source-R1\ntarget-R2"
        if "-r proc" in command:
            return "1.0"
        assert "lshwres -r mem" in command and "--level sys" in command
        return "0,256"

    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(side_effect=cli))
    root = Path(arm.__file__).resolve().parents[2]

    def git(repo, *args):
        stdout = (
            str(root)
            if args == ("rev-parse", "--show-toplevel")
            else "c" * 40
            if args == ("rev-parse", "HEAD")
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(arm.evidence, "_git", git)
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=CONFIG,
        config=LiveTestConfig(
            system_name="sys-R1", lp3_name="source-R1", scratch_name="target-R2"
        ),
        results=[],
        effects=0,
        checkpoint_count=0,
        fault=None,
        checkpoint_failure=None,
        skip=lambda *args: None,
    )

    def record(subtask, tool, status, data, *args, **kwargs):
        state.results.append(
            {"subtask": subtask, "tool": tool, "status": status, "data": deepcopy(data)}
        )

    def checkpoint():
        state.checkpoint_count += 1
        if state.checkpoint_count == state.checkpoint_failure:
            raise OSError("checkpoint failed")

    async def call(client, tool, **kwargs):
        assert tool == "hmc_move_lpar_memory"
        assert kwargs["move"] == {"quantity_mib": 256}
        assert kwargs["system_name_or_uuid"] == "aaaaaaaa-0000-0000-0000-000000000001"
        assert state.checkpoint_count == 2 + 2 * state.effects
        state.effects += 1
        source = kwargs["lpar_name_or_uuid"]
        target = kwargs["target_lpar_name_or_uuid"]
        for uuid, delta in ((source, -256), (target, 256)):
            if uuid == target and state.fault == "partial":
                continue
            for field in ("CurrentMemory", "RuntimeMemory"):
                mem(boundary.before[uuid])[field] = str(
                    int(mem(boundary.before[uuid])[field]) + delta
                )
        return "PASS", {
            "outcome": "partial" if state.fault else "applied",
            "workflow_completed": not state.fault,
        }

    state.record = record
    state.checkpoint = checkpoint
    state.call = call
    state.record_verified = lambda *args, **kwargs: setattr(state, "verified", kwargs)
    return state, boundary.before, initial


@pytest.mark.asyncio
async def test_live_move_sequence(live_boundary):
    state, peers, initial = live_boundary
    await arm.exercise_memory_move(None, state)
    assert state.effects == 2 and peers == initial
    assert state.verified["cleanup"] == "passed"
    assert all(a.holds for a in state.verified["assertions"])


@pytest.mark.asyncio
async def test_live_move_stops(live_boundary):
    state, peers, initial = live_boundary
    state.fault = "partial"
    await arm.exercise_memory_move(None, state)
    assert state.effects == 1 and peers != initial
    assert state.verified["cleanup"] == "failed"
    assert not all(a.holds for a in state.verified["assertions"])


@pytest.mark.asyncio
async def test_recovery_compares_both_without_writes(live_boundary):
    state, peers, _ = live_boundary
    await arm.exercise_memory_move(None, state)
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "tested_commit": "c" * 40,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": state.results,
    }
    await arm.recover_document(CONFIG, document)
    before = state.effects
    mem(peers[TARGET])["CurrentMemory"] = "2304"
    mem(peers[TARGET])["RuntimeMemory"] = "2304"
    with pytest.raises(ValueError, match="baselines differ"):
        await arm.recover_document(CONFIG, document)
    assert state.effects == before


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint,effects", [(1, 0), (2, 0), (3, 1), (4, 1)])
async def test_checkpoint_failure_stops_writes(live_boundary, checkpoint, effects):
    state, _, _ = live_boundary
    state.checkpoint_failure = checkpoint
    await arm.exercise_memory_move(None, state)
    assert state.effects == effects
    assert state.verified["cleanup"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revision", "group", "hmc", "baseline"])
async def test_recovery_rejects_provenance(live_boundary, fault):
    state, _, _ = live_boundary
    await arm.exercise_memory_move(None, state)
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "tested_commit": "c" * 40,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": state.results,
    }
    if fault == "revision":
        document["run"]["tested_commit"] = "d" * 40
    if fault == "group":
        document["run"]["group"] = "all"
    if fault == "hmc":
        document["hmc"]["port"] += 1
    if fault == "baseline":
        document["results"] = []
    with pytest.raises(ValueError):
        await arm.recover_document(CONFIG, document)
    assert state.effects == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target,name", [(SOURCE, "source-R1"), (TARGET, "target-R2")])
async def test_uuid_alias_cannot_bypass_protected_fixture(live_boundary, target, name):
    state, _, _ = live_boundary
    state.config = LiveTestConfig(
        system_name="sys-R1",
        lp3_name=SOURCE,
        scratch_name=TARGET,
        protected_lpar_names=(name,),
    )
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any(
        row["status"] == "FAIL" and "protected" in str(row["data"])
        for row in state.results
    )


@pytest.mark.asyncio
async def test_arm_requires_matching_positive_ownership(live_boundary, monkeypatch):
    state, _, _ = live_boundary
    monkeypatch.setattr(arm, "get_lpar_description", AsyncMock(return_value=""))
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [True, 0, -1, "7"])
async def test_oracle_refuses_invalid_id_without_command(monkeypatch, number):
    command = AsyncMock()
    monkeypatch.setattr(arm, "run_hmc_command", command)
    result = await arm.oracle(
        SimpleNamespace(config=CONFIG),
        "sys-R1",
        [{"uuid": SOURCE, "partition_id": number}],
    )
    assert result[0]["error"] and result[0]["values"] is None
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        "",
        "7,1024,2048,4096,2048,shared",
        "7,1024,2048,4096,2048,ded\nextra",
        "8,1024,2048,4096,2048,ded",
        "7,3072,2048,4096,2048,ded",
        "7,1024,8192,4096,2048,ded",
        "7,1024,２０４８,4096,2048,ded",
        HMCError("unavailable"),
    ],
)
async def test_oracle_rejects_bad_independent_row(monkeypatch, row):
    command = AsyncMock(side_effect=[row, "8,1024,2048,4096,2048,ded"])
    monkeypatch.setattr(arm, "run_hmc_command", command)
    peers = [{"uuid": SOURCE, "partition_id": 7}, {"uuid": TARGET, "partition_id": 8}]
    result = await arm.oracle(SimpleNamespace(config=CONFIG), "sys-R1", peers)
    assert result[0]["error"] and result[0]["values"] is None
    assert result[1]["values"] == [8, 1024, 2048, 4096, 2048]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionType", "OS400"),
        ("PartitionState", "not activated"),
        ("ResourceMonitoringControlState", "inactive"),
        ("CurrentProfileSync", "Enabled"),
        ("IsOperationInProgress", "true"),
    ],
)
async def test_ineligible_fixture_never_mutates(live_boundary, field, value):
    state, peers, _ = live_boundary
    peers[TARGET]["Resource"][field] = value
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["shared", "runtime", "capability", "source-min", "target-max"]
)
async def test_ineligible_memory_never_mutates(live_boundary, fault):
    state, peers, _ = live_boundary
    if fault == "shared":
        mem(peers[TARGET])["SharedMemoryEnabled"] = "true"
    elif fault == "runtime":
        mem(peers[TARGET])["RuntimeMemory"] = "1792"
    elif fault == "capability":
        peers[TARGET]["Resource"]["PartitionCapabilities"][
            "DynamicLogicalPartitionMemoryCapable"
        ] = "false"
    elif fault == "source-min":
        mem(peers[SOURCE])["CurrentMinimumMemory"] = "2048"
    else:
        mem(peers[TARGET])["CurrentMaximumMemory"] = "2048"
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
async def test_missing_checkpoint_never_mutates(live_boundary):
    state, _, _ = live_boundary
    state.checkpoint = None
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any("checkpoint" in str(row["data"]) for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["group", "credentials"])
async def test_skip_is_not_positive_evidence(live_boundary, missing):
    state, _, _ = live_boundary
    if missing == "group":
        state.group = None
    else:
        state.hmc_config = None
    skips = []
    state.skip = lambda *args: skips.append(args)
    await arm.exercise_memory_move(None, state)
    assert len(skips) == 1 and state.effects == 0
    assert not state.results and not hasattr(state, "verified")


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint,effects", [(1, 0), (3, 1)])
async def test_drift_before_each_case_stops_writes(live_boundary, checkpoint, effects):
    state, peers, _ = live_boundary
    original = state.checkpoint

    def drift():
        original()
        if state.checkpoint_count == checkpoint:
            peers[TARGET]["Resource"]["CurrentProfileSync"] = "Suspended"

    state.checkpoint = drift
    await arm.exercise_memory_move(None, state)
    assert state.effects == effects
    assert state.verified["cleanup"] == "failed"
    assert any("drift before effect" in str(row["data"]) for row in state.results)


@pytest.mark.asyncio
async def test_baseline_oracle_mismatch_never_mutates(live_boundary, monkeypatch):
    state, _, _ = live_boundary
    monkeypatch.setattr(
        arm, "run_hmc_command", AsyncMock(return_value="7,1024,2048,4096,2048,ded")
    )
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0 and not hasattr(state, "verified")
    assert any("baseline does not match" in str(row["data"]) for row in state.results)


@pytest.mark.asyncio
async def test_distinct_selectors_cannot_alias_same_fixture(live_boundary):
    state, _, _ = live_boundary
    state.config = LiveTestConfig(
        system_name="sys-R1", lp3_name=SOURCE, scratch_name="source-R1"
    )
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert any("distinct fixtures" in str(row["data"]) for row in state.results)


def test_preflight_uses_offline_fixture_admission():
    import live_test_preflight as preflight

    invalid = preflight.arm_verdicts(LiveTestConfig(), arm.GROUP)
    valid = preflight.arm_verdicts(
        LiveTestConfig(
            system_name="sys-R1", lp3_name="source-R1", scratch_name="target-R2"
        ),
        arm.GROUP,
    )
    assert len(invalid) == 1 and not invalid[0].runnable
    assert len(valid) == 1 and valid[0].runnable
    assert valid[0].group == arm.GROUP and valid[0].system_name == "sys-R1"


def test_recovery_route_is_read_only(live_boundary, monkeypatch, tmp_path, capsys):
    import json

    import live_test_recovery as recovery

    state, peers, _ = live_boundary
    asyncio.run(arm.exercise_memory_move(None, state))
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "tested_commit": "c" * 40,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": state.results,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)

    def no_generic(*args):
        raise AssertionError("memory move must not enter deletion-oriented recovery")

    monkeypatch.setattr(recovery, "dispatched_subtasks", no_generic)
    before = state.effects
    assert recovery.main(["--results", str(path)]) == 0
    assert "CLEAN:" in capsys.readouterr().out
    mem(peers[TARGET])["CurrentMemory"] = "2304"
    mem(peers[TARGET])["RuntimeMemory"] = "2304"
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN:" not in capsys.readouterr().out
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: False)
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN:" not in capsys.readouterr().out
    assert state.effects == before
