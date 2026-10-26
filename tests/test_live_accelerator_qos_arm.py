"""Finite arm64 proof with only HMC/SSH and source-control boundaries mocked."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import live_test_preflight as preflight
import live_test_recovery as recovery
from live_test import accelerator_qos as arm
from live_test import runner
from live_test.config import LiveTestConfig, decode_saved_config
from live_test.observation import CallFailure

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError

SYSTEM = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TARGET = "11111111-2222-3333-4444-555555555555"
CONFIG = HMCConfig.from_mapping(
    {"host": "hmc.test", "user": "operator", "agent_id": "arm64"}
)
REQUEST = {"qos": 3, "wait_minutes": 0, "detail_level": 4, "force": False}


def config():
    return LiveTestConfig(
        system_name="sys-R1",
        lp3_name="partition-R1",
        protected_lpar_names=("protected-R1",),
        accelerator_qos_request_json=json.dumps(REQUEST),
    )


def test_live_config_wrapper(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / ".env.example"
    path = tmp_path / ".env"
    path.write_text(
        source.read_text()
        + "\nLIVE_TEST_ACCELERATOR_QOS_REQUEST_JSON='"
        + json.dumps(REQUEST)
        + "'\n"
    )
    monkeypatch.setenv("LIVE_TEST_ACCELERATOR_QOS_REQUEST_JSON", '{"qos":99}')
    loaded = LiveTestConfig.from_env_file(path)
    assert json.loads(loaded.accelerator_qos_request_json) == REQUEST
    path.write_text(source.read_text())
    assert LiveTestConfig.from_env_file(path).accelerator_qos_request_json == ""
    saved = asdict(config())
    saved["protected_lpar_names"] = list(saved["protected_lpar_names"])
    saved.pop("accelerator_qos_request_json")
    old = decode_saved_config(saved)
    assert old.accelerator_qos_request_json == ""
    with pytest.raises(ValueError):
        arm.configured(old)
    saved["accelerator_qos_request_json"] = {}
    with pytest.raises(TypeError):
        decode_saved_config(saved)
    assert preflight._accelerator_qos_verdict(config()).runnable
    assert not preflight._accelerator_qos_verdict(old).runnable
    assert preflight._memory_move_verdict(
        replace(old, scratch_name="partition-R2")
    ).runnable


@pytest.mark.parametrize(
    "changes",
    [
        {"system_name": ""},
        {"system_name": "example-system"},
        {"lp3_name": " "},
        {"lp3_name": "example-partition"},
        {"protected_lpar_names": ("partition-R1",)},
        *(
            {"accelerator_qos_request_json": value}
            for value in ("", "bad", "{}", '{"qos":true}', '{"qos":3,"extra":0}')
        ),
    ],
)
def test_live_config_wrapper_refuses(changes):
    with pytest.raises(ValueError):
        arm.configured(replace(config(), **changes))


@pytest.mark.asyncio
async def test_live_checkpoint_drift_invalid_system_uuid():
    hmc = SimpleNamespace(
        list_logical_partitions=AsyncMock(
            side_effect=AssertionError("unvalidated system queried")
        )
    )
    with pytest.raises(ValueError):
        await arm.admit(
            hmc, "not-a-uuid", "sys-R1", TARGET, (), arm.configured(config())[1]
        )


def test_live_registered_opt_in(monkeypatch, tmp_path):
    assert arm.SUBTASK == 64 and arm.GROUP == "lpar-accelerator-qos"
    assert arm.SCENARIO == "st64-lpar-accelerator-qos"
    assert runner.SUBTASKS[64] is arm.exercise_accelerator_qos
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [64]
    assert 64 not in runner.SUBTASK_GROUPS["all"]
    seen = []
    tasks = {}
    for number in runner.SUBTASKS:

        async def run(client, state, number=number):
            seen.append(number)

        tasks[number] = run

    @asynccontextmanager
    async def served():
        yield None

    monkeypatch.setattr(runner, "SUBTASKS", tasks)
    monkeypatch.setattr(runner, "served_client", served)
    monkeypatch.setattr(runner, "served_schemas", AsyncMock(return_value={}))
    monkeypatch.setattr(runner.evidence, "repository_root", lambda: None)
    for group in (None, "all"):
        seen.clear()
        assert (
            asyncio.run(
                runner.main(
                    group=group,
                    config=LiveTestConfig(),
                    hmc_config=CONFIG,
                    results_path=str(tmp_path / f"{group}.json"),
                )
            )
            == 0
        )
        assert seen and 64 not in seen


@pytest.fixture
def boundary(monkeypatch):
    partition = {
        "UUID": TARGET,
        "Resource": {
            "PartitionName": "partition-R1",
            "PartitionID": "7",
            "PartitionType": "AIX/Linux",
            "PartitionState": "running",
            "ResourceMonitoringControlState": "active",
            "IsOperationInProgress": "false",
        },
    }
    system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            "State": "operating",
            "AssociatedSystemCapabilities": {
                "HardwareAcceleratorCapable": "true",
                "IBMiHardwareAcceleratorCapable": "false",
            },
            "SupportedHardwareAcceleratorTypes": {
                "HardwareAcceleratorType": {"Type": "gzip"}
            },
        },
    }
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=CONFIG,
        config=config(),
        results=[],
        writes=[],
        verified=None,
        native=[1, 1],
        owner="[hmcpctl owner:arm64 created:2026-10-26]",
        checkpoints=0,
        checkpoint_fail=None,
        hook=None,
        fault=None,
        fault_phase=0,
        read_fail=False,
        connections=0,
        skip=lambda *a: None,
    )
    hmc = SimpleNamespace(
        config=CONFIG,
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        get_managed_system=AsyncMock(side_effect=lambda *a: deepcopy(system)),
        list_logical_partitions=AsyncMock(side_effect=lambda *a: [deepcopy(partition)]),
        list_vios=AsyncMock(return_value=[]),
        get_logical_partition=AsyncMock(side_effect=lambda *a: deepcopy(partition)),
    )

    @asynccontextmanager
    async def client(cfg):
        assert cfg is CONFIG
        state.connections += 1
        yield hmc

    monkeypatch.setattr(arm, "HMCClient", client)
    monkeypatch.setattr(
        arm, "get_lpar_description", AsyncMock(side_effect=lambda *a: state.owner)
    )
    provenance = {"tested_commit": "c" * 40, "tree_clean": True}
    monkeypatch.setattr(arm.evidence, "run_provenance", lambda *a: deepcopy(provenance))

    async def native(cfg, command, **kwargs):
        assert cfg is CONFIG
        assert "lshwres -r accel -m sys-R1 --rsubtype gzip --level " in command
        assert not command.startswith("ch")
        if "--level sys" in command:
            return "gzip,128,120,120"
        assert "--filter lpar_ids=7" in command
        if state.read_fail:
            raise HMCError("independent read unavailable")
        return "7,gzip," + ",".join(map(str, state.native))

    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(side_effect=native))

    def record(subtask, tool, status, data, **kwargs):
        state.results.append(
            {"subtask": subtask, "tool": tool, "status": status, "data": deepcopy(data)}
        )

    def checkpoint():
        state.checkpoints += 1
        if state.checkpoint_fail == state.checkpoints:
            raise OSError("checkpoint failed")
        if state.hook:
            state.hook(state, partition, system, provenance)

    async def call(client, tool, **kwargs):
        assert tool == "hmc_set_lpar_accelerator_qos"
        assert kwargs["lpar_name_or_uuid"] == TARGET
        assert kwargs["system_name_or_uuid"] == SYSTEM
        assert "ownership_override" not in kwargs
        assert state.results[-1]["tool"].endswith(" intent")
        state.writes.append(deepcopy(kwargs["change"]))
        fault = state.fault if state.fault_phase == len(state.writes) - 1 else None
        if fault == "call-failure":
            return "FAIL", CallFailure(
                "HMCError",
                "submission unavailable",
                "private-trace-sentinel",
                None,
                False,
            )
        if fault != "disagree":
            state.native[:] = [kwargs["change"]["qos"]] * 2
        result = {
            "requested_qos": kwargs["change"]["qos"],
            "submission": "completed",
            "submission_error": None,
            "workflow_completed": True,
            "outcome": "observed",
        }
        if fault == "read":
            state.read_fail = True
        elif fault == "error":
            result["submission_error"] = "uncertain"
        elif fault == "missing-error":
            result.pop("submission_error")
        elif fault in ("rejected", "unknown"):
            result["submission"] = fault
        elif fault == "incomplete":
            result["workflow_completed"] = False
        elif fault == "partial":
            result["outcome"] = "partial"
        return ("FAIL" if fault == "refused" else "PASS"), result

    state.record, state.checkpoint, state.call = record, checkpoint, call
    state.record_verified = lambda *a, **kw: setattr(state, "verified", kw)
    return state, partition, system, provenance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "disagree",
        "refused",
        "unknown",
        "rejected",
        "missing-error",
        "error",
        "incomplete",
        "partial",
        "read",
    ],
)
async def test_live_phase_gate(boundary, fault):
    state, *_ = boundary
    state.fault = fault
    await arm.exercise_accelerator_qos(None, state)
    assert len(state.writes) == 1
    assert state.verified["cleanup"] == "failed"
    assert not any(
        a.holds
        for a in state.verified["assertions"]
        if a.id in ("forward-current-pending", "exact-two-phase-sequence")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["refused", "unknown", "read", "disagree"])
async def test_live_phase_gate_restore_refusal(boundary, fault):
    state, *_ = boundary
    state.fault, state.fault_phase = fault, 1
    await arm.exercise_accelerator_qos(None, state)
    assert len(state.writes) == 2
    assert state.verified["cleanup"] == "failed"
    assert not all(a.holds for a in state.verified["assertions"])


@pytest.mark.asyncio
async def test_live_phase_gate_call_failure_no_traceback(boundary):
    state, *_ = boundary
    state.fault = "call-failure"
    await arm.exercise_accelerator_qos(None, state)
    assert len(state.writes) == 1
    assert "private-trace-sentinel" not in json.dumps(state.results)
    assert "submission unavailable" in json.dumps(state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1])
@pytest.mark.parametrize("after_intent", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "candidate",
        "target",
        "protected",
        "owner",
        "capability",
        "state",
        "rmc",
        "native",
        "checkpoint",
        "destination",
    ],
)
async def test_live_checkpoint_drift(boundary, phase, after_intent, fault):
    state, *_ = boundary

    def drift(s, partition, system, provenance):
        if s.checkpoints != (1 if phase == 0 else 3) + after_intent:
            return
        if fault == "candidate":
            provenance["tested_commit"] = "d" * 40
        elif fault == "target":
            partition["Resource"]["PartitionID"] = "8"
        elif fault == "protected":
            s.config = replace(s.config, protected_lpar_names=("partition-R1",))
        elif fault == "owner":
            s.owner = "[hmcpctl owner:foreign created:2026-10-26]"
        elif fault == "capability":
            system["Resource"]["AssociatedSystemCapabilities"][
                "HardwareAcceleratorCapable"
            ] = "false"
        elif fault in ("state", "rmc"):
            partition["Resource"][
                "PartitionState"
                if fault == "state"
                else "ResourceMonitoringControlState"
            ] = "Unknown"
        elif fault == "native":
            s.native[:] = [99, 99]
        elif fault == "destination":
            s.hmc_config = HMCConfig.from_mapping({"host": "other.test"})
        else:
            if after_intent:
                raise OSError("checkpoint failed")
            s.checkpoint_fail = s.checkpoints + 1

    state.hook = drift
    await arm.exercise_accelerator_qos(None, state)
    assert len(state.writes) == phase
    assert state.verified is None or state.verified["cleanup"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "failed",
        "credentials",
        "same",
        "pending",
        "capability",
        "config",
        "owner",
    ],
)
async def test_live_checkpoint_drift_initial(boundary, fault):
    state, _partition, system, _ = boundary
    if fault == "absent":
        state.checkpoint = None
    elif fault == "failed":
        state.checkpoint_fail = 1
    elif fault == "credentials":
        state.hmc_config = None
    elif fault == "same":
        state.native[:] = [3, 3]
    elif fault == "capability":
        system["Resource"]["AssociatedSystemCapabilities"][
            "HardwareAcceleratorCapable"
        ] = "false"
    elif fault == "config":
        state.config = replace(state.config, accelerator_qos_request_json="")
    elif fault == "owner":
        state.owner = "owned-looking but unstamped"
    else:
        state.native[:] = [1, 2]
    await arm.exercise_accelerator_qos(None, state)
    assert state.writes == []
    if fault in ("config", "absent", "credentials"):
        assert state.connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "7,gzip,1,1\n7,gzip,1,1",
        "8,gzip,1,1",
        "7,other,1,1",
        "7,gzip,-1,1",
        "7,gzip,١,1",
        "7,gzip,1",
        "7,gzip,1,1,extra",
    ],
)
async def test_live_phase_gate_oracle_rejects_malformed(boundary, monkeypatch, raw):
    state, *_ = boundary
    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(return_value=raw))
    with pytest.raises(ValueError):
        await arm.oracle(SimpleNamespace(config=CONFIG), "sys-R1", 7)
    assert state.writes == []


@pytest.mark.asyncio
async def test_live_restore_baseline(boundary):
    state, *_ = boundary
    await arm.exercise_accelerator_qos(None, state)
    assert state.writes == [REQUEST, {**REQUEST, "qos": 1}]
    assert state.native == [1, 1]
    assert state.verified["cleanup"] == "passed"
    assert [a.id for a in state.verified["assertions"]] == [
        "forward-current-pending",
        "restore-current-pending",
        "original-baseline",
        "exact-two-phase-sequence",
    ]
    assert all(a.holds for a in state.verified["assertions"])
    assert state.results[0]["data"]["native"] == [7, "gzip", 1, 1]


@pytest.mark.asyncio
async def test_live_restore_baseline_ignores_unrelated_metadata(boundary):
    state, *_ = boundary
    state.hook = lambda s, p, system, provenance: system["Resource"].update(
        UnrelatedProviderCounter=s.checkpoints
    )
    await arm.exercise_accelerator_qos(None, state)
    assert len(state.writes) == 2 and state.verified["cleanup"] == "passed"


def document(state):
    return {
        "run": {
            "group": arm.GROUP,
            "subtasks": [64],
            "tree_clean": True,
            "partial": False,
            "tested_commit": "c" * 40,
        },
        "hmc": {
            key: getattr(CONFIG, key) for key in ("host", "port", "user", "verify_ssl")
        },
        "results": deepcopy(state.results),
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "candidate",
        "destination",
        "duplicate",
        "missing",
        "reordered",
        "partial",
        "intent",
        "failed",
        "refused",
        "malformed",
        "final-drift",
        "baseline",
        "read",
        "bool-observation",
        "extra-effect",
        "wrong-group",
        "wrong-subtask",
        "system-drift",
        "foreign-row",
        "bool-intent",
        "bool-destination",
        "float-subtask",
        "protection-shape",
    ],
)
def test_recovery_proof(boundary, monkeypatch, tmp_path, capsys, fault):
    state, *_ = boundary
    asyncio.run(arm.exercise_accelerator_qos(None, state))
    doc = document(state)
    if fault == "candidate":
        doc["run"]["tested_commit"] = "d" * 40
    elif fault == "destination":
        doc["hmc"]["host"] = "other.test"
    elif fault == "duplicate":
        doc["results"].append(deepcopy(doc["results"][0]))
    elif fault == "missing":
        doc["results"].pop(2)
    elif fault == "reordered":
        doc["results"][1:3] = reversed(doc["results"][1:3])
    elif fault == "partial":
        doc["run"]["partial"] = True
    elif fault == "intent":
        doc["results"][3]["data"]["change"]["qos"] = 99
    elif fault == "failed":
        doc["results"][2]["status"] = "FAIL"
    elif fault == "refused":
        doc["results"][2]["data"]["result"]["submission"] = "rejected"
    elif fault == "malformed":
        doc["results"][2] = None
    elif fault == "final-drift":
        state.native[:] = [2, 2]
    elif fault == "baseline":
        doc["results"][0]["data"]["native"] = [7, "gzip", 3, 3]
    elif fault == "read":
        state.read_fail = True
    elif fault == "bool-observation":
        doc["results"][4]["data"]["independent"][2] = True
    elif fault == "extra-effect":
        doc["results"].append({"subtask": 64, "tool": arm.TOOL, "status": "PASS"})
        doc["results"].append({"subtask": 64, "tool": arm.TOOL, "status": "PASS"})
    elif fault == "wrong-group":
        doc["run"]["group"] = "all"
    elif fault == "wrong-subtask":
        doc["run"]["subtasks"] = [54]
    elif fault == "system-drift":
        doc["results"][0]["data"]["system_native"][1] = 129
    elif fault == "foreign-row":
        doc["results"].append({"subtask": 54, "tool": "other", "status": "PASS"})
    elif fault == "bool-intent":
        doc["results"][3]["data"]["change"]["qos"] = True
    elif fault == "bool-destination":
        doc["hmc"]["verify_ssl"] = int(CONFIG.verify_ssl)
    elif fault == "float-subtask":
        doc["run"]["subtasks"] = [64.0]
    elif fault == "protection-shape":
        doc["results"][0]["data"]["protected_names"] = "protected-R1"
    path = tmp_path / "results.json"
    path.write_text(json.dumps(doc))
    original = path.read_bytes()
    if fault == "wrong-group":
        with pytest.raises(ValueError, match="provenance"):
            asyncio.run(arm.recover_document(CONFIG, doc))
        assert state.writes == [REQUEST, {**REQUEST, "qos": 1}]
        assert path.read_bytes() == original
        return
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)
    before = deepcopy(state.writes)
    code = recovery.main(["--results", str(path)])
    captured = capsys.readouterr()
    assert code == (0 if fault is None else 2)
    assert ("CLEAN:" in captured.out) is (fault is None)
    if fault in (None, "failed", "refused", "partial", "read", "final-drift"):
        assert (
            "OBSERVATION:" in captured.out
            and "system native quantities" in captured.out
        )
    assert CONFIG.user not in captured.out and CONFIG.host not in captured.out
    assert state.writes == before
    assert path.read_bytes() == original
