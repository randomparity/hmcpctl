"""ST61 exercises real domain logic against mocked HMC transport boundaries."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import make_dataclass, replace
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lpar.test_processor_move import SOURCE, SYSTEM, TARGET, boundary, set_current  # noqa: F401 — pytest fixture

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_processors import LparProcessorMove
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import processor_move as operation
from hmcpctl.ssh.processors import _FIELDS

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import memory_move, runner
from live_test import processor_move as arm
from live_test.config import LiveTestConfig

CONFIG = HMCConfig.from_mapping({})


@pytest.fixture
def live_boundary(monkeypatch, boundary):  # noqa: F811 — fixture imported above
    b = boundary
    initial = deepcopy(b.before), deepcopy(b.rows)

    @asynccontextmanager
    async def client(config):
        yield b.hmc

    monkeypatch.setattr(arm, "HMCClient", client)
    monkeypatch.setattr(arm, "get_lpar_description", b.owner)
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=CONFIG,
        config=LiveTestConfig(
            system_name="sys-R1", lp3_name="source-R1", scratch_name="target-R2"
        ),
        results=[],
        checkpoint_count=0,
        checkpoint_failure=None,
        result_changes={},
        checkpoint_action=None,
        skip=lambda *args: None,
        fault=None,
        oracle_fault=None,
        missing_submission=False,
        generated=False,
    )

    async def native(config, system, source, target, move):
        for number, sign in ((source, -1), (target, 1)):
            uuid = SOURCE if number == 7 else TARGET
            with localcontext(prec=512):
                units = (
                    Decimal(b.rows[uuid]["curr_proc_units"])
                    + sign * move.processing_units
                )
            set_current(b.before[uuid], b.rows[uuid], 3, f"{units:.2f}")
        if state.fault == "lost-ack":
            raise HMCError("completion unknown")
        return ""

    b.submit.side_effect = native

    async def cli(config, command, **kwargs):
        if "--level lpar" in command:
            uuid = SOURCE if "lpar_ids=7 " in command else TARGET
            if state.oracle_fault == uuid:
                raise HMCError("independent read unavailable")
            if "-r proc" in command:
                return ",".join(b.rows[uuid][key] for key in _FIELDS)
            resource = b.before[uuid]["Resource"]
            memory = resource["PartitionMemoryConfiguration"]
            return ",".join(
                [
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
                    "ded",
                ]
            )
        if "-r lpar" in command:
            return "source-R1\ntarget-R2"
        if "-r proc" in command:
            return "0.00"
        assert "-r mem" in command and "--level sys" in command
        return "0,256"

    command = AsyncMock(side_effect=cli)
    monkeypatch.setattr(arm, "run_hmc_command", command)
    monkeypatch.setattr(memory_move, "run_hmc_command", command)
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

    def record(subtask, tool, status, data, *args, **kwargs):
        state.results.append(
            {
                "subtask": subtask,
                "tool": tool,
                "status": status,
                "data": deepcopy(data),
                "result": "observed",
                "timestamp": "2026-10-26T00:00:00+00:00",
                "note": "",
            }
        )

    def checkpoint():
        state.checkpoint_count += 1
        if state.checkpoint_count == state.checkpoint_failure:
            raise OSError("checkpoint failed")
        if state.checkpoint_action:
            state.checkpoint_action(state.checkpoint_count)

    async def call(client, tool, **kwargs):
        assert tool == "hmc_move_lpar_processors"
        assert kwargs["move"] == {"processing_units": "0.10"}
        assert state.checkpoint_count == 2 + 2 * b.submit.await_count
        result = await operation.move_lpar_processors(
            b.hmc,
            kwargs["lpar_name_or_uuid"],
            kwargs["target_lpar_name_or_uuid"],
            LparProcessorMove(**kwargs["move"]),
            system_name_or_uuid=kwargs["system_name_or_uuid"],
        )
        result.update(state.result_changes)
        if state.missing_submission:
            del result["submission_error"]
        if state.generated:
            model = make_dataclass("GeneratedResult", [(key, object) for key in result])
            return "PASS", model(**result)
        return "PASS", result

    state.record, state.checkpoint, state.call = record, checkpoint, call
    state.record_verified = lambda *args, **kwargs: setattr(state, "verified", kwargs)
    return SimpleNamespace(state=state, boundary=b, initial=initial, command=command)


def document(state):
    return {
        "run": {
            "group": arm.GROUP,
            "subtasks": [61],
            "tree_clean": True,
            "tested_commit": "c" * 40,
            "partial": False,
        },
        "config": {},
        "artifacts": {},
        "hmc": {
            "host": CONFIG.host,
            "port": CONFIG.port,
            "user": CONFIG.user,
            "verify_ssl": CONFIG.verify_ssl,
        },
        "results": deepcopy(state.results),
    }


def completed(fixture):
    asyncio.run(arm.exercise_processor_move(None, fixture.state))
    assert fixture.boundary.submit.await_count == 2
    assert (fixture.boundary.before, fixture.boundary.rows) == fixture.initial
    return document(fixture.state)


def test_only_named_group_registers_arm():
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [61]
    assert runner.SUBTASKS[61] is arm.exercise_processor_move
    assert 61 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.parametrize(
    "source,target",
    [
        ("", "target-R2"),
        ("same", "same"),
        ("example-source", "target-R2"),
        ("source-R1", "example-target"),
    ],
)
def test_configured_refuses_missing_or_default(source, target):
    with pytest.raises(ValueError):
        arm.configured(LiveTestConfig(lp3_name=source, scratch_name=target))


def test_successful_sequence_and_read_only_recovery(live_boundary):
    f = live_boundary
    doc = completed(f)
    baseline = doc["results"][0]["data"]
    arm.validate_finished(doc, baseline)
    f.command.reset_mock()
    asyncio.run(arm.recover_document(CONFIG, doc))
    assert f.boundary.submit.await_count == 2
    assert f.command.await_count == 7
    assert all(
        call.args[1].startswith(("lshwres ", "lssyscfg "))
        for call in f.command.await_args_list
    )
    assert f.state.verified["cleanup"] == "passed"
    assert all(a.holds for a in f.state.verified["assertions"])
    assert f.state.verified["data"] == {
        k: "not-run"
        for k in ("dedicated", "mixed", "inactive", "force", "IBM_i", "VIOS")
    }
    assert len(doc["results"]) == 6
    assert json.loads(json.dumps(doc)) == doc


def test_canonical_baseline_retains_all_snapshot_fields(live_boundary):
    doc = completed(live_boundary)
    peer = doc["results"][0]["data"]["peers"][0]
    assert peer.keys() == arm._SNAPSHOT_FIELDS
    assert peer["current_processing_units"] == "2.00"
    assert peer["processors"]["CurrentProcessingUnits"] == "2.00"
    assert type(peer["current_processors"]) is int
    assert peer["entitled_mib"] is None
    assert len(doc["results"][0]["data"]["oracle"][0]["processors"]) == 18


@pytest.mark.parametrize("generated", [False, True])
def test_explicit_null_result_allows_both_cases(live_boundary, generated):
    live_boundary.state.generated = generated
    completed(live_boundary)


@pytest.mark.parametrize("checkpoint,effects", [(1, 0), (2, 0), (3, 1), (4, 1)])
def test_checkpoint_failure_stops_effects(live_boundary, checkpoint, effects):
    f = live_boundary
    f.state.checkpoint_failure = checkpoint
    asyncio.run(arm.exercise_processor_move(None, f.state))
    assert f.boundary.submit.await_count == effects
    assert not all(a.holds for a in f.state.verified["assertions"])


@pytest.mark.parametrize(
    "fault",
    [
        "lost-ack",
        "missing-submission",
        "unverified",
        "partial",
        "submission",
        "source-missing",
        "destination-missing",
    ],
)
def test_uncertain_forward_never_authorizes_reverse(live_boundary, fault):
    f = live_boundary
    if fault == "lost-ack":
        f.state.fault = fault
    elif fault == "missing-submission":
        f.state.missing_submission = True
    elif fault == "submission":
        f.state.result_changes = {"submission_error": "unknown"}
    elif fault in ("unverified", "partial"):
        f.state.result_changes = {"outcome": fault, "workflow_completed": False}
    else:
        f.state.result_changes = {
            "source" if fault.startswith("source") else "destination": None
        }
    asyncio.run(arm.exercise_processor_move(None, f.state))
    assert f.boundary.submit.await_count == 1
    assert not any(
        row["tool"] == "processor-move reverse intent" for row in f.state.results
    )
    assert not all(a.holds for a in f.state.verified["assertions"])


@pytest.mark.parametrize(
    "drift", ["identity", "state", "quantity", "ownership", "profile"]
)
def test_reverse_rechecks_every_fixture(live_boundary, monkeypatch, drift):
    f = live_boundary

    def change(number):
        if number != 3:
            return
        resource = f.boundary.before[TARGET]["Resource"]
        if drift == "identity":
            resource["PartitionName"] = "changed-R2"
        elif drift == "state":
            resource["ResourceMonitoringControlState"] = "inactive"
        elif drift == "quantity":
            set_current(f.boundary.before[TARGET], f.boundary.rows[TARGET], 3, "2.20")
        elif drift == "ownership":
            monkeypatch.setattr(arm, "get_lpar_description", AsyncMock(return_value=""))
        else:
            resource["CurrentProfileSync"] = "Enabled"

    f.state.checkpoint_action = change
    asyncio.run(arm.exercise_processor_move(None, f.state))
    assert f.boundary.submit.await_count == 1
    assert not any(
        row["tool"] == "processor-move reverse intent" for row in f.state.results
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "not activated"),
        ("PartitionType", "OS400"),
        ("ResourceMonitoringControlState", "inactive"),
        ("CurrentProfileSync", "On"),
        ("IsOperationInProgress", "true"),
    ],
)
def test_ineligible_fixture_never_mutates(live_boundary, field, value):
    f = live_boundary
    f.boundary.before[SOURCE]["Resource"][field] = value
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "field",
    ["source-minimum", "destination-maximum", "pending-minimum", "pending-maximum"],
)
def test_both_directions_need_headroom(live_boundary, field):
    f = live_boundary
    uuid = SOURCE if field.endswith("minimum") else TARGET
    cpu = f.boundary.before[uuid]["Resource"]["PartitionProcessorConfiguration"]
    pending = field.startswith("pending")
    container = cpu[
        "SharedProcessorConfiguration"
        if pending
        else "CurrentSharedProcessorConfiguration"
    ]
    minimum = field.endswith("minimum")
    key = ("" if pending else "Current") + (
        "MinimumProcessingUnits" if minimum else "MaximumProcessingUnits"
    )
    container[key] = "2.00"
    f.boundary.rows[uuid][
        ("pend" if pending else "curr")
        + ("_min_proc_units" if minimum else "_max_proc_units")
    ] = "2.00"
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("uuid,name", [(SOURCE, "source-R1"), (TARGET, "target-R2")])
def test_uuid_selector_cannot_bypass_protected_fixture(live_boundary, uuid, name):
    f = live_boundary
    f.state.config = replace(
        f.state.config,
        lp3_name=SOURCE,
        scratch_name=TARGET,
        protected_lpar_names=(name,),
    )
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()


def test_requires_positive_matching_ownership(live_boundary, monkeypatch):
    f = live_boundary
    monkeypatch.setattr(
        arm,
        "get_lpar_description",
        AsyncMock(return_value="ordinary unowned description"),
    )
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("missing", ["group", "credentials", "checkpoint"])
def test_unarmed_or_missing_checkpoint_never_mutates(live_boundary, missing):
    f = live_boundary
    if missing == "group":
        f.state.group = "all"
    elif missing == "credentials":
        f.state.hmc_config = None
    else:
        f.state.checkpoint = None
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()
    assert not hasattr(f.state, "verified")


@pytest.mark.parametrize("number", [True, 0, -1, "7", None, 1.0])
def test_oracle_invalid_id_never_dispatches(monkeypatch, number):
    command = AsyncMock()
    monkeypatch.setattr(arm, "run_hmc_command", command)
    result = asyncio.run(
        arm.oracle(
            SimpleNamespace(config=CONFIG),
            "sys-R1",
            [{"uuid": SOURCE, "name": "source-R1", "partition_id": number}],
        )
    )
    assert result[0]["error"] and result[0]["processors"] is None
    command.assert_not_awaited()


@pytest.mark.parametrize("first", [SOURCE, TARGET])
def test_oracle_attempts_peer_after_read_failure(live_boundary, first):
    f = live_boundary
    peers = asyncio.run(
        arm.admit(f.boundary.hmc, SYSTEM, "sys-R1", [SOURCE, TARGET], [])
    )
    f.state.oracle_fault = first
    result = asyncio.run(arm.oracle(f.boundary.hmc, "sys-R1", peers))
    assert result[0 if first == SOURCE else 1]["error"]
    other = result[1 if first == SOURCE else 0]
    assert other["error"] is None and len(other["processors"]) == 18


@pytest.mark.parametrize("raw", ["", "bad", "1,2", '"unclosed', "\n\n"])
def test_oracle_rejects_malformed_cpu_row(live_boundary, monkeypatch, raw):
    peers = asyncio.run(
        arm.admit(live_boundary.boundary.hmc, SYSTEM, "sys-R1", [SOURCE, TARGET], [])
    )
    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(return_value=raw))
    observed = asyncio.run(arm.oracle(live_boundary.boundary.hmc, "sys-R1", peers))
    assert all(
        o["error"] and o["processors"] is None and o["memory"] is None for o in observed
    )


def corrupt(doc, fault):
    rows = doc["results"]
    if fault.startswith("partial-"):
        value = {
            "true": True,
            "missing": None,
            "null": None,
            "zero": 0,
            "string": "false",
        }[fault.removeprefix("partial-")]
        doc["run"]["partial"] = value
        if fault == "partial-missing":
            del doc["run"]["partial"]
    elif fault.startswith("missing-"):
        del rows[int(fault.removeprefix("missing-"))]
    elif fault.startswith("duplicate-"):
        index = int(fault.removeprefix("duplicate-"))
        rows.insert(index, deepcopy(rows[index]))
    elif fault == "malformed-row":
        rows.append(None)
    elif fault == "extra-row":
        rows.append({**rows[-1], "tool": "unknown-proof"})
    elif fault == "reordered":
        rows[2], rows[3] = rows[3], rows[2]
    elif fault.startswith("refused-"):
        rows[int(fault.removeprefix("refused-"))]["status"] = "FAIL"
    elif fault.startswith("intent-"):
        index, key = fault.removeprefix("intent-").split(":")
        intent = rows[int(index)]["data"]
        if key == "before":
            intent[key][0]["current_processors"] = 7
        else:
            intent[key] = {
                "source": TARGET if index == "1" else SOURCE,
                "destination": SOURCE if index == "1" else TARGET,
                "processing_units": 0.1,
            }[key]
    elif fault.startswith("observed-"):
        index, key = fault.removeprefix("observed-").split(":")
        observed = rows[int(index)]["data"]
        if key == "independent":
            del observed[key][1]
        elif key == "missing-submission":
            del observed["result"]["submission_error"]
        elif key == "runtime":
            del observed["result"]["destination"]["after"]["runtime_processing_units"]
        elif key == "count-bool":
            observed["result"]["source"]["after"]["current_processors"] = True
        elif key == "endpoint":
            del observed["result"]["source"]
        else:
            observed["result"][key] = {
                "submission_error": "unknown",
                "workflow_completed": False,
                "outcome": "partial",
                "move": {"processing_units": "0.20"},
            }[key]
    elif fault == "final-mismatch":
        rows[-1]["data"][0]["processors"]["curr_proc_units"] = "9.00"
    elif fault == "row-type":
        rows[1]["subtask"] = 61.0
    elif fault == "timestamp":
        del rows[1]["timestamp"]
    else:
        raise AssertionError(fault)


PROOF_FAULTS = [
    *[f"partial-{v}" for v in ("true", "missing", "null", "zero", "string")],
    *[f"missing-{i}" for i in range(6)],
    *[f"duplicate-{i}" for i in range(6)],
    "malformed-row",
    "extra-row",
    "reordered",
    "final-mismatch",
    "row-type",
    "timestamp",
    *[f"refused-{i}" for i in range(6)],
    *[
        f"intent-{i}:{key}"
        for i in (1, 3)
        for key in ("source", "destination", "processing_units", "before")
    ],
    *[
        f"observed-{i}:{key}"
        for i in (2, 4)
        for key in (
            "independent",
            "missing-submission",
            "runtime",
            "count-bool",
            "endpoint",
            "submission_error",
            "workflow_completed",
            "outcome",
            "move",
        )
    ],
]


@pytest.mark.parametrize("fault", PROOF_FAULTS)
def test_incomplete_saved_proof_cannot_clean_matching_live_baselines(
    live_boundary, monkeypatch, tmp_path, capsys, fault
):
    import live_test_recovery as recovery

    f = live_boundary
    doc = completed(f)
    corrupt(doc, fault)
    with pytest.raises(ValueError):
        asyncio.run(arm.recover_document(CONFIG, doc))
    path = tmp_path / "result.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)
    assert recovery.main(["--results", str(path)]) == 2
    captured = capsys.readouterr()
    assert "CLEAN:" not in captured.out and "unconfirmed" in captured.err
    assert f.boundary.submit.await_count == 2
    assert (f.boundary.before, f.boundary.rows) == f.initial


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_processing_units", None),
        ("current_processing_units", "2.0"),
        ("current_processing_units", "2e0"),
        ("current_processing_units", 2.0),
        ("current_processors", True),
        ("partition_id", 0),
        ("shared", 0),
        ("uuid", "bad"),
        ("automatic_entitlement", "false"),
        ("dynamic_processor", 1),
        ("change_location", {}),
        ("processors", {}),
        ("pending_minimum_processing_units", "3.00"),
    ],
)
def test_snapshot_refuses_missing_malformed_or_noncanonical_fields(
    live_boundary, field, value
):
    doc = completed(live_boundary)
    baseline = doc["results"][0]["data"]
    baseline["peers"][0][field] = value
    with pytest.raises(ValueError):
        arm.validate_finished(doc, baseline)


@pytest.mark.parametrize(
    "field",
    [
        "runtime_processing_units",
        "dynamic_memory",
        "entitled_mib",
        "current_processors",
        "processors",
    ],
)
def test_snapshot_requires_every_field(live_boundary, field):
    doc = completed(live_boundary)
    baseline = doc["results"][0]["data"]
    del baseline["peers"][0][field]
    with pytest.raises(ValueError):
        arm.validate_finished(doc, baseline)


@pytest.mark.parametrize("field", ["run", "config", "hmc", "artifacts"])
def test_proof_requires_runner_envelope(live_boundary, field):
    doc = completed(live_boundary)
    baseline = doc["results"][0]["data"]
    doc[field] = []
    with pytest.raises(ValueError):
        arm.validate_finished(doc, baseline)


@pytest.mark.parametrize("fault", ["revision", "group", "hmc", "subtasks", "dirty"])
def test_recovery_requires_same_candidate_and_destination(live_boundary, fault):
    doc = completed(live_boundary)
    if fault == "revision":
        doc["run"]["tested_commit"] = "a" * 40
    elif fault == "group":
        doc["run"]["group"] = "all"
    elif fault == "hmc":
        doc["hmc"]["user"] = "other-R1"
    elif fault == "subtasks":
        doc["run"]["subtasks"] = [61.0]
    else:
        doc["run"]["tree_clean"] = False
    with pytest.raises(ValueError):
        asyncio.run(arm.recover_document(CONFIG, doc))


@pytest.mark.parametrize(
    "drift", ["quantity", "memory", "profile", "ownership", "system"]
)
def test_valid_saved_proof_does_not_replace_fresh_baselines(
    live_boundary, monkeypatch, drift
):
    f = live_boundary
    doc = completed(f)
    resource = f.boundary.before[TARGET]["Resource"]
    if drift == "quantity":
        set_current(f.boundary.before[TARGET], f.boundary.rows[TARGET], 3, "2.20")
    elif drift == "memory":
        resource["PartitionMemoryConfiguration"]["CurrentMemory"] = "2304"
        resource["PartitionMemoryConfiguration"]["RuntimeMemory"] = "2304"
    elif drift == "profile":
        resource["CurrentProfileSync"] = "On"
    elif drift == "ownership":
        monkeypatch.setattr(arm, "get_lpar_description", AsyncMock(return_value=""))
    else:
        monkeypatch.setattr(
            arm, "system_oracle", AsyncMock(return_value=["changed", "0.00", "0,256"])
        )
    with pytest.raises(ValueError):
        asyncio.run(arm.recover_document(CONFIG, doc))
    assert f.boundary.submit.await_count == 2


def test_uuid_case_is_identity_not_drift(live_boundary):
    f = live_boundary
    doc = completed(f)

    def upper(value, key=""):
        if isinstance(value, dict):
            return {k: upper(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [upper(v) for v in value]
        return (
            value.upper()
            if key in {"uuid", "UUID", "system_uuid", "source", "destination"}
            and isinstance(value, str)
            else value
        )

    doc = upper(doc)
    asyncio.run(arm.recover_document(CONFIG, doc))
    assert f.boundary.submit.await_count == 2


def test_phase_contract_rejects_nonpass(live_boundary):
    doc = completed(live_boundary)
    data = doc["results"][2]["data"]
    after = doc["results"][3]["data"]["before"]
    assert arm.phase_succeeded("PASS", data["result"], data["independent"], after)
    assert not arm.phase_succeeded("FAIL", data["result"], data["independent"], after)


def test_decimal_encoding_ignores_ambient_precision(live_boundary):
    with localcontext(prec=2):
        doc = completed(live_boundary)
        arm.validate_finished(doc, doc["results"][0]["data"])
    assert arm._encoded(Decimal("0.10")) == "0.10"
    with pytest.raises(TypeError):
        arm._encoded(0.1)


@pytest.mark.parametrize("peer", [SOURCE, TARGET])
def test_baseline_independent_failure_never_mutates(live_boundary, peer):
    f = live_boundary
    f.state.oracle_fault = peer
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()
    assert not hasattr(f.state, "verified")


@pytest.mark.parametrize("checkpoint,effects", [(1, 0), (3, 1)])
def test_fresh_baseline_drift_stops_next_effect(live_boundary, checkpoint, effects):
    f = live_boundary

    def change(number):
        if number == checkpoint:
            set_current(f.boundary.before[TARGET], f.boundary.rows[TARGET], 3, "2.20")

    f.state.checkpoint_action = change
    asyncio.run(arm.exercise_processor_move(None, f.state))
    assert f.boundary.submit.await_count == effects
    assert not all(a.holds for a in f.state.verified["assertions"])


def test_distinct_selectors_cannot_alias_same_fixture(live_boundary):
    f = live_boundary
    f.state.config = replace(f.state.config, scratch_name=SOURCE)
    asyncio.run(arm.exercise_processor_move(None, f.state))
    f.boundary.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "cell,value",
    [
        ("lpar_id", "99"),
        ("curr_proc_mode", "ded"),
        ("pend_proc_mode", "ded"),
        ("lpar_name", "other-R1"),
        ("curr_proc_units", "NaN"),
        ("curr_procs", "-1"),
    ],
)
def test_independent_cpu_identity_and_numeric_failures(
    live_boundary, monkeypatch, cell, value
):
    f = live_boundary
    peers = asyncio.run(
        arm.admit(f.boundary.hmc, SYSTEM, "sys-R1", [SOURCE, TARGET], [])
    )
    row = deepcopy(f.boundary.rows[SOURCE])
    row[cell] = value
    command = AsyncMock(return_value=",".join(row[k] for k in _FIELDS))
    if cell in ("lpar_id", "curr_proc_mode", "pend_proc_mode", "lpar_name"):
        command.side_effect = [
            ",".join(row[k] for k in _FIELDS),
            "7,1024,2048,4096,2048,ded",
        ] * 2
    monkeypatch.setattr(arm, "run_hmc_command", command)
    observed = asyncio.run(arm.oracle(f.boundary.hmc, "sys-R1", peers))
    assert all(o["error"] and o["processors"] is None for o in observed)


@pytest.mark.parametrize(
    "row",
    [
        "7,1024,2048,4096,2048,shared",
        "7,1024,2048,4096,2048",
        "99,1024,2048,4096,2048,ded",
        "7,4096,2048,4096,2048,ded",
        "7,1024,2048,4096,NaN,ded",
    ],
)
def test_independent_memory_row_refusal(live_boundary, monkeypatch, row):
    f = live_boundary
    peers = asyncio.run(
        arm.admit(f.boundary.hmc, SYSTEM, "sys-R1", [SOURCE, TARGET], [])
    )
    command = AsyncMock(
        side_effect=[
            ",".join(f.boundary.rows[SOURCE][k] for k in _FIELDS),
            row,
            ",".join(f.boundary.rows[TARGET][k] for k in _FIELDS),
            row,
        ]
    )
    monkeypatch.setattr(arm, "run_hmc_command", command)
    observed = asyncio.run(arm.oracle(f.boundary.hmc, "sys-R1", peers))
    assert all(o["error"] and o["memory"] is None for o in observed)
