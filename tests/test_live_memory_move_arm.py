import asyncio
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
        result_changes={},
        missing_submission_error=False,
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
        result = {
            "outcome": "partial" if state.fault else "applied",
            "workflow_completed": not state.fault,
            "quantity_mib": 256,
            "submission_error": None,
            **state.result_changes,
        }
        if state.missing_submission_error:
            del result["submission_error"]
        return "PASS", result

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
            "partial": False,
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
@pytest.mark.parametrize(
    "fault",
    [
        "baseline-shape",
        "peers-shape",
        "peers-mapping",
        "too-few",
        "too-many",
        "peer-shape",
        "missing-uuid",
        "nonstring-uuid",
        "invalid-uuid",
        "duplicate-uuid",
    ],
)
async def test_recovery_bounds_peers_before_client(
    live_boundary,
    boundary,  # noqa: F811 — pytest injects the imported HTTP/SSH fixture
    monkeypatch,
    fault,
):
    state, _, _ = live_boundary
    await arm.exercise_memory_move(None, state)
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "partial": False,
            "tested_commit": "c" * 40,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": deepcopy(state.results),
    }
    baseline = document["results"][0]["data"]
    peers = baseline["peers"]
    if fault == "baseline-shape":
        document["results"][0]["data"] = None
    elif fault == "peers-shape":
        baseline["peers"] = None
    elif fault == "peers-mapping":
        baseline["peers"] = {"source": peers[0], "destination": peers[1]}
    elif fault == "too-few":
        baseline["peers"] = peers[:1]
    elif fault == "too-many":
        baseline["peers"] = peers + peers[:1]
    elif fault == "peer-shape":
        peers[1] = None
    elif fault == "missing-uuid":
        del peers[1]["uuid"]
    elif fault == "nonstring-uuid":
        peers[1]["uuid"] = 8
    elif fault == "invalid-uuid":
        peers[1]["uuid"] = "target-R2"
    else:
        peers[1]["uuid"] = peers[0]["uuid"].upper()
    client = Mock(wraps=arm.HMCClient)
    monkeypatch.setattr(arm, "HMCClient", client)
    boundary.hmc.get_logical_partition.reset_mock()
    boundary.hmc.list_logical_partitions.reset_mock()
    boundary.description.reset_mock()
    arm.run_hmc_command.reset_mock()
    with pytest.raises(ValueError, match="baseline peers"):
        try:
            await arm.recover_document(CONFIG, document)
        finally:
            client.assert_not_called()
    boundary.hmc.get_logical_partition.assert_not_awaited()
    boundary.hmc.list_logical_partitions.assert_not_awaited()
    boundary.description.assert_not_awaited()
    arm.run_hmc_command.assert_not_awaited()
    assert state.effects == 2


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
            "partial": False,
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
            "partial": False,
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


@pytest.fixture
def system_observation_boundary(live_boundary, monkeypatch):
    state, _, _ = live_boundary
    original = arm.run_hmc_command
    fault = {}

    async def observed(config, command):
        if "--level lpar" not in command:
            key = (
                "names"
                if "-r lpar" in command
                else "cpu"
                if "-r proc" in command
                else "memory"
            )
            if key in fault and (not fault.get("after_effect") or state.effects):
                return fault[key]
        return await original(config, command)

    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(side_effect=observed))
    return state, fault


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("names", ""),
        ("names", "source-R1\n\ntarget-R2"),
        ("cpu", ""),
        ("cpu", "NaN"),
        ("cpu", "Infinity"),
        ("cpu", "-1"),
        ("cpu", "1\n1"),
        ("cpu", "unavailable"),
        ("memory", ""),
        ("memory", "0"),
        ("memory", "0,256,1"),
        ("memory", "-1,256"),
        ("memory", "０,256"),
        ("memory", "0,0"),
    ],
)
async def test_system_observation_invalid_baseline_prevents_effects(
    system_observation_boundary, field, value
):
    state, fault = system_observation_boundary
    fault[field] = value
    await arm.exercise_memory_move(None, state)
    assert state.effects == 0
    assert not hasattr(state, "verified")
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.parametrize("field", ["names", "cpu", "memory"])
def test_system_observation_failure_after_effect_cannot_attest_clean(
    system_observation_boundary, field, monkeypatch, tmp_path, capsys
):
    import json

    import live_test_recovery as recovery

    state, fault = system_observation_boundary
    fault.update({field: "", "after_effect": True})
    asyncio.run(arm.exercise_memory_move(None, state))
    assert state.effects == 2
    assert state.verified["cleanup"] == "failed"
    baseline = next(row["data"] for row in state.results if row["tool"] == arm.BASELINE)
    baseline["system_oracle"][["names", "cpu", "memory"].index(field)] = ""
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "partial": False,
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
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN:" not in capsys.readouterr().out
    assert state.effects == 2


@pytest.mark.asyncio
async def test_system_observation_zero_available_capacities_are_valid(
    system_observation_boundary,
):
    state, fault = system_observation_boundary
    fault.update(cpu="0", memory="0,256")
    await arm.exercise_memory_move(None, state)
    assert state.effects == 2
    assert state.verified["cleanup"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["submission", "missing", "unverified", "partial"])
async def test_uncertain_forward_never_authorizes_reverse(live_boundary, fault):
    state, peers, initial = live_boundary
    if fault == "submission":
        state.result_changes = {"submission_error": "transport completion unknown"}
    elif fault == "missing":
        state.missing_submission_error = True
    else:
        state.result_changes = {"outcome": fault, "workflow_completed": False}
    await arm.exercise_memory_move(None, state)
    assert state.effects == 1 and peers != initial
    observed = next(
        r for r in state.results if r["tool"] == "memory-move forward observed"
    )
    assert observed["status"] == "FAIL"
    assert [p["values"][2] for p in observed["data"]["independent"]] == [1792, 2304]
    assert not any(r["tool"] == "memory-move reverse intent" for r in state.results)
    assert not all(a.holds for a in state.verified["assertions"])


@pytest.mark.asyncio
async def test_explicit_null_generated_result_allows_both_phases(live_boundary):
    state, peers, initial = live_boundary
    original = state.call

    async def generated(*args, **kwargs):
        status, data = await original(*args, **kwargs)
        return status, SimpleNamespace(**data)

    state.call = generated
    await arm.exercise_memory_move(None, state)
    assert state.effects == 2 and peers == initial
    assert all(a.holds for a in state.verified["assertions"])


def incomplete_proof(document, fault):
    rows = document["results"]
    if fault.startswith("partial-"):
        value = {
            "true": True,
            "missing": None,
            "null": None,
            "zero": 0,
            "string": "false",
        }[fault.removeprefix("partial-")]
        document["run"]["partial"] = value
        if fault == "partial-missing":
            del document["run"]["partial"]
    elif fault in {"baseline-only", "forward-pending", "reverse-pending"}:
        del rows[
            {"baseline-only": 1, "forward-pending": 2, "reverse-pending": 4}[fault] :
        ]
    elif fault.startswith("missing-"):
        del rows[int(fault.removeprefix("missing-"))]
    elif fault.startswith("duplicate-"):
        index = int(fault.removeprefix("duplicate-"))
        rows.insert(index, deepcopy(rows[index]))
    elif fault == "out-of-bounds":
        rows[0]["data"]["peers"][0]["current_minimum_mib"] = 2048
        rows[0]["data"]["oracle"][0]["values"][1] = 2048
        rows[5]["data"][0]["values"][1] = 2048
        for index in (1, 3):
            rows[index]["data"]["before"][0]["current_minimum_mib"] = 2048
            rows[index + 1]["data"]["independent"][0]["values"][1] = 2048
    elif fault == "malformed-row":
        rows.append(None)
    elif fault == "reordered":
        rows[2], rows[3] = rows[3], rows[2]
    elif fault.startswith("refused-"):
        rows[int(fault.removeprefix("refused-"))]["status"] = "FAIL"
    elif fault.startswith("intent-"):
        index, key = fault.removeprefix("intent-").split(":")
        intent = rows[int(index)]["data"]
        if key == "before":
            intent[key][0]["current_memory_mib"] += 256
        else:
            intent[key] = {
                "source": TARGET,
                "destination": SOURCE,
                "quantity_mib": 512,
            }[key]
            if index == "3" and key in {"source", "destination"}:
                intent[key] = SOURCE if key == "source" else TARGET
    elif fault.startswith("observed-"):
        index, key = fault.removeprefix("observed-").split(":")
        data = rows[int(index)]["data"]
        if key == "independent":
            data[key][0]["values"][2] += 256
        elif key == "missing-submission":
            del data["result"]["submission_error"]
        else:
            data["result"][key] = {
                "submission_error": "unknown completion",
                "workflow_completed": False,
                "outcome": "unverified",
                "quantity_mib": 512,
            }[key]
    else:
        assert fault == "final-mismatch"
        rows[5]["data"][0]["values"][2] += 256


@pytest.mark.parametrize(
    "fault",
    [
        *[f"partial-{flag}" for flag in ("true", "missing", "null", "zero", "string")],
        "baseline-only",
        "forward-pending",
        "reverse-pending",
        *[f"missing-{index}" for index in range(1, 6)],
        *[f"duplicate-{index}" for index in range(1, 6)],
        "reordered",
        "malformed-row",
        "out-of-bounds",
        *[f"refused-{index}" for index in range(1, 6)],
        *[
            f"intent-{index}:{key}"
            for index in (1, 3)
            for key in ("source", "destination", "quantity_mib", "before")
        ],
        *[
            f"observed-{index}:{key}"
            for index in (2, 4)
            for key in (
                "submission_error",
                "missing-submission",
                "outcome",
                "workflow_completed",
                "quantity_mib",
                "independent",
            )
        ],
        "final-mismatch",
    ],
)
def test_incomplete_proof_never_clears_matching_baselines(
    live_boundary, monkeypatch, tmp_path, capsys, fault
):
    import json

    import live_test_recovery as recovery

    state, peers, initial = live_boundary
    asyncio.run(arm.exercise_memory_move(None, state))
    assert state.effects == 2 and peers == initial
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "tested_commit": "c" * 40,
            "partial": False,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": deepcopy(state.results),
    }
    incomplete_proof(document, fault)
    if fault == "out-of-bounds":
        mem(peers[SOURCE])["CurrentMinimumMemory"] = "2048"
        mem(initial[SOURCE])["CurrentMinimumMemory"] = "2048"
    command = arm.run_hmc_command
    command.reset_mock()
    with pytest.raises(ValueError, match="finite|partial"):
        asyncio.run(arm.recover_document(CONFIG, document))
    assert command.await_count == 5
    path = tmp_path / "result.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)
    command.reset_mock()
    assert recovery.main(["--results", str(path)]) == 2
    captured = capsys.readouterr()
    assert "CLEAN:" not in captured.out
    assert "unconfirmed" in captured.err
    assert command.await_count == 5
    assert all(
        call.args[1].startswith(("lshwres ", "lssyscfg "))
        for call in command.await_args_list
    )
    assert state.effects == 2 and peers == initial


@pytest.mark.parametrize(
    "failure", ["baseline-checkpoint", "intent-checkpoint", "refused"]
)
def test_stopped_arm_with_unchanged_state_remains_unconfirmed(live_boundary, failure):
    state, peers, initial = live_boundary
    if failure == "refused":
        state.call = AsyncMock(return_value=("FAIL", {"error": "native refusal"}))
    else:
        state.checkpoint_failure = 1 if failure == "baseline-checkpoint" else 2
    asyncio.run(arm.exercise_memory_move(None, state))
    assert state.effects == 0 and peers == initial
    assert not all(a.holds for a in state.verified["assertions"])
    document = {
        "run": {
            "group": arm.GROUP,
            "subtasks": [54],
            "tree_clean": True,
            "tested_commit": "c" * 40,
            "partial": False,
        },
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": state.results,
    }
    arm.run_hmc_command.reset_mock()
    with pytest.raises(ValueError, match="finite"):
        asyncio.run(arm.recover_document(CONFIG, document))
    assert arm.run_hmc_command.await_count == 5
    assert state.effects == 0 and peers == initial


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["identity", "state", "quantity", "ownership"])
async def test_reverse_requires_fresh_admission(live_boundary, monkeypatch, drift):
    state, peers, _ = live_boundary
    original = state.checkpoint

    def checkpoint():
        original()
        if state.checkpoint_count != 3:
            return
        if drift == "identity":
            peers[TARGET]["Resource"]["PartitionName"] = "changed-R2"
        elif drift == "state":
            peers[TARGET]["Resource"]["ResourceMonitoringControlState"] = "inactive"
        elif drift == "quantity":
            mem(peers[TARGET])["CurrentMemory"] = "2560"
            mem(peers[TARGET])["RuntimeMemory"] = "2560"
        else:
            monkeypatch.setattr(arm, "get_lpar_description", AsyncMock(return_value=""))

    state.checkpoint = checkpoint
    await arm.exercise_memory_move(None, state)
    assert state.effects == 1
    assert not any(r["tool"] == "memory-move reverse intent" for r in state.results)
    assert not all(a.holds for a in state.verified["assertions"])
