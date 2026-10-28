"""ST67 through real product dispatch, finite evidence and read-only recovery."""

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
from lpar.test_memory_statistics_reset import reset_boundary  # noqa: F401 — boundary fixture

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import evidence, runner
from live_test import memory_statistics as arm
from live_test.config import LiveTestConfig

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.config import HMCConfig
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle


@pytest.fixture
def live_boundary(monkeypatch, reset_boundary):  # noqa: F811 — imported fixture
    root = Path(__file__).parents[1]

    def git(_root, *args):
        text = (
            str(root)
            if args == ("rev-parse", "--show-toplevel")
            else "c" * 40
            if args == ("rev-parse", "HEAD")
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=text)

    monkeypatch.setattr(evidence, "_git", git)
    reset_boundary.hmc.config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "user-R1",
            "password": "test-password",  # pragma: allowlist secret - mocked HMC fixture
        }
    )
    memory = reset_boundary.resource["PartitionMemoryConfiguration"]
    memory.update(
        {
            name: "2048"
            for name in (
                "CurrentMemory",
                "DesiredMemory",
                "RuntimeMemory",
                "CurrentMinimumMemory",
                "CurrentMaximumMemory",
            )
        }
    )

    @asynccontextmanager
    async def hmc_client(config):
        yield reset_boundary.hmc

    monkeypatch.setattr(arm, "HMCClient", hmc_client)
    monkeypatch.setattr(arm, "get_lpar_description", reset_boundary.description)
    monkeypatch.setattr(
        lifecycle, "with_client", lambda fn, **kw: asyncio.run(fn(reset_boundary.hmc))
    )
    state = runner.RunState(
        config=LiveTestConfig(
            system_name="sys-R1",
            lp3_name="guest-R1",
            protected_lpar_names=("protected-R2",),
        ),
        group=arm.GROUP,
        hmc_config=reset_boundary.hmc.config,
        checkpoint=Mock(),
    )
    return reset_boundary, state


async def exercise(live_boundary):
    _boundary, state = live_boundary
    application = create_mcp(
        compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    )
    async with Client(application) as client:
        await arm.exercise_memory_statistics(client, state)
    document = {
        "run": evidence.run_provenance(
            [67],
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
    return json.loads(json.dumps(document))


def test_arm_only_named_group():
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [67]
    assert runner.SUBTASKS[67] is arm.exercise_memory_statistics
    assert 67 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.parametrize("name", ["", "example-guest", "protected-R2"])
def test_configured_refuses_unsafe_fixture(name):
    with pytest.raises(ValueError):
        arm.configured(
            LiveTestConfig(lp3_name=name, protected_lpar_names=("protected-R2",))
        )


@pytest.mark.asyncio
async def test_arm_one_reset(live_boundary):
    boundary, state = live_boundary
    document = await exercise(live_boundary)
    assert boundary.resets == 1
    assert [row["tool"] for row in state.results] == [*arm.PHASES, arm.TOOL]
    assert state.results[-1]["result"] == "passed"
    assert state.checkpoint.call_count >= 2
    baseline = document["results"][0]["data"]
    assert (
        arm.validate_finished(document, baseline)["result"]["reset_verification"]
        == "unavailable"
    )
    await arm.recover_document(state.hmc_config, document)
    assert boundary.resets == 1


@pytest.mark.asyncio
async def test_changed_counters_do_not_claim_reset_causality(live_boundary):
    live_boundary[0].after = "min_io_entitled_mem=0"
    document = await exercise(live_boundary)
    assert document["results"][-1]["result"] == "passed"
    assert document["results"][-1]["data"] == {"reset_verification": "unavailable"}


@pytest.mark.asyncio
async def test_checkpoint_failure_before_intent_prevents_reset(live_boundary):
    boundary, state = live_boundary
    state.checkpoint.side_effect = OSError("private output unavailable")
    await exercise(live_boundary)
    assert boundary.resets == 0
    assert not any(row["result"] == "passed" for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["owner", "protected", "allocation", "shared"])
async def test_live_admission_prevents_reset(live_boundary, case):
    boundary, state = live_boundary
    if case == "owner":
        boundary.foreign = True
    elif case == "protected":
        state.config = LiveTestConfig(
            system_name="sys-R1",
            lp3_name="guest-R1",
            protected_lpar_names=("guest-R1",),
        )
    elif case == "allocation":
        del boundary.resource["PartitionMemoryConfiguration"]["RuntimeMemory"]
    else:
        boundary.resource["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = (
            "false"
        )
    await exercise(live_boundary)
    assert boundary.resets == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "partial",
        "candidate",
        "schema",
        "attempt",
        "request",
        "status",
        "native",
        "result-extra",
        "phase-extra",
        "order",
        "duplicate",
        "bool-id",
        "missing-phase",
        "hmc",
        "fixture",
        "final-allocation",
        "statistics",
        "run-schema",
        "run-subtask",
        "tls-bool",
        "identity-bool",
        "legacy-config",
    ],
)
async def test_recovery_closed_record(live_boundary, case):
    boundary, state = live_boundary
    document = await exercise(live_boundary)
    baseline = document["results"][0]["data"]
    if case == "partial":
        document["run"]["partial"] = True
    elif case == "candidate":
        document["run"]["tested_commit"] = "d" * 40
    elif case == "schema":
        baseline["schema_version"] = True
    elif case == "attempt":
        document["results"][1]["data"]["attempt"] = True
    elif case == "request":
        document["results"][1]["data"]["request"] = {"wait_minutes": 0}
    elif case == "status":
        document["results"][2]["data"]["status"] = "FAIL"
    elif case == "native":
        document["results"][2]["data"]["result"]["native_completion"] = "unknown"
    elif case == "result-extra":
        document["results"][2]["data"]["result"]["unexpected"] = None
    elif case == "phase-extra":
        document["results"][1]["data"]["unexpected"] = None
    elif case == "order":
        document["results"][1:3] = document["results"][1:3][::-1]
    elif case == "duplicate":
        document["results"].insert(2, deepcopy(document["results"][1]))
    elif case == "bool-id":
        baseline["nativeID"] = True
    elif case == "missing-phase":
        document["results"].pop(1)
    elif case == "hmc":
        document["hmc"]["host"] = "foreign.test"
    elif case == "fixture":
        document["config"]["lp3_name"] = "foreign-R2"
    elif case == "final-allocation":
        document["results"][3]["data"]["allocation"]["CurrentMemory"] = "1024"
    elif case == "statistics":
        document["results"][2]["data"]["result"]["after_statistics"]["usable"] = 1
    elif case == "run-schema":
        document["run"]["schema_version"] = False
    elif case == "run-subtask":
        document["run"]["subtasks"] = [67.0]
    elif case == "tls-bool":
        document["hmc"]["verify_ssl"] = 0
    elif case == "identity-bool":
        document["results"][1]["data"]["baseline_identity"][-1] = 1
    else:
        document["config"]["test_user"] = "retired"
    reads = boundary.reads
    calls = boundary.command.await_count
    with pytest.raises((ValueError, TypeError)):
        await arm.recover_document(state.hmc_config, document)
    assert boundary.resets == 1
    assert boundary.reads == reads and boundary.command.await_count == calls


@pytest.mark.asyncio
async def test_every_required_phase_field_is_checked_before_recovery_reads(
    live_boundary,
):
    boundary, state = live_boundary
    original = await exercise(live_boundary)
    paths = [
        ("run",),
        ("hmc",),
        ("results", 0, "data"),
        ("results", 0, "data", "context"),
        ("results", 0, "data", "destination"),
        ("results", 1, "data"),
        ("results", 2, "data"),
        ("results", 2, "data", "result"),
        ("results", 3, "data"),
    ]
    reads = boundary.reads
    calls = boundary.command.await_count
    for path in paths:
        source = original
        for key in path:
            source = source[key]
        for missing in source:
            document = deepcopy(original)
            target = document
            for key in path:
                target = target[key]
            del target[missing]
            with pytest.raises((ValueError, TypeError)):
                await arm.recover_document(state.hmc_config, document)
            assert boundary.reads == reads and boundary.command.await_count == calls


@pytest.mark.asyncio
async def test_record_size_refuses_before_reset(live_boundary):
    boundary, state = live_boundary
    state.config = LiveTestConfig(
        system_name="sys-R1",
        lp3_name="guest-R1",
        protected_lpar_names=("opaque" * 220000,),
    )
    await exercise(live_boundary)
    assert boundary.resets == 0
    assert not any(row["result"] == "passed" for row in state.results)


@pytest.mark.asyncio
async def test_recovery_rejects_oversized_record_before_reads(live_boundary):
    boundary, state = live_boundary
    document = await exercise(live_boundary)
    document["artifacts"]["oversized"] = "x" * 1048576
    calls = boundary.command.await_count
    with pytest.raises(ValueError, match="1 MiB"):
        await arm.recover_document(state.hmc_config, document)
    assert boundary.command.await_count == calls and boundary.resets == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["nonzero", "unknown", "after-read", "allocation-drift"]
)
async def test_arm_incomplete_attempt_never_replays_or_promotes(live_boundary, failure):
    from hmcpctl.errors import HMCError
    from hmcpctl.ssh.transport import HMCCLIError

    boundary, state = live_boundary
    if failure == "nonzero":
        boundary.native_error = HMCCLIError("nonzero", remote_nonzero_completed=True)
    elif failure == "unknown":
        boundary.native_error = TimeoutError()
    elif failure == "after-read":
        boundary.after_error = HMCError("unavailable")
    else:
        original = boundary.command.side_effect

        async def drift(config, text, **kwargs):
            result = await original(config, text, **kwargs)
            if text.startswith("chhwres"):
                boundary.resource["PartitionMemoryConfiguration"]["CurrentMemory"] = (
                    "1024"
                )
            return result

        boundary.command.side_effect = drift
    document = await exercise(live_boundary)
    assert boundary.resets == 1
    assert not any(row["result"] == "passed" for row in state.results)
    calls = boundary.command.await_count
    with pytest.raises((ValueError, TypeError)):
        await arm.recover_document(state.hmc_config, document)
    assert boundary.command.await_count == calls and boundary.resets == 1


@pytest.mark.asyncio
async def test_observation_ids_and_matrix_preserve_bounded_claim(live_boundary):
    import live_test_evidence

    _boundary, state = live_boundary
    document = await exercise(live_boundary)
    observation = state.observations[-1]
    assert observation["operation"] == "lpar.reset_memory_statistics"
    assert observation["observation"]["scenario"] == "st67-lpar-memory-statistics-reset"
    assert observation["observation"]["assertions"] == [
        "native-command-completed",
        "usable-before",
        "usable-independent-after",
        "contained-shared-identity",
        "allocation-observed-unchanged",
        "reset-verification-unavailable",
    ]
    document["hmc"]["host"] = "PRIVATE-SENTINEL"
    document["results"][0]["data"]["private"] = "PRIVATE-SENTINEL"
    rendered = live_test_evidence.render(document)
    assert rendered is not None and arm.TOOL in rendered
    assert "PRIVATE-SENTINEL" not in rendered
    assert document["results"][-1]["data"] == {"reset_verification": "unavailable"}


@pytest.mark.asyncio
async def test_run_schema_mismatch_refuses_before_recovery_reads(live_boundary):
    boundary, state = live_boundary
    document = await exercise(live_boundary)
    document["run"]["schema_version"] = "V99_99"
    reads, calls = boundary.reads, boundary.command.await_count
    with pytest.raises(ValueError, match="schema"):
        await arm.recover_document(state.hmc_config, document)
    assert (boundary.reads, boundary.command.await_count) == (reads, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ["", "(not set)"])
async def test_unset_schema_recovery_preserves_sentinel(live_boundary, schema):
    boundary, state = live_boundary
    boundary.hmc.config = state.hmc_config.model_copy(update={"schema_version": ""})
    state.hmc_config = boundary.hmc.config
    document = await exercise(live_boundary)
    document["run"]["schema_version"] = schema
    await arm.recover_document(state.hmc_config, document)


@pytest.mark.parametrize(
    "fault", ["completion", "attempt", "root", "schema", "oversize", "utf8"]
)
def test_raw_recovery_refuses_before_bootstrap(
    live_boundary, tmp_path, monkeypatch, fault
):
    import live_test_recovery as recovery

    document = asyncio.run(exercise(live_boundary))
    raw = json.dumps(document)
    tokens = {
        "completion": (
            '"native_completion": "completed"',
            '"native_completion": "unknown", "native_completion": "completed"',
        ),
        "attempt": ('"attempt": 1', '"attempt": 2, "attempt": 1'),
        "root": ('"run": {', '"run": {}, "run": {'),
        "schema": ('"schema_version": 1', '"schema_version": 2, "schema_version": 1'),
    }
    if fault in tokens:
        old, new = tokens[fault]
        assert old in raw
        raw = raw.replace(old, new, 1)
    if fault == "oversize":
        raw += " " * (1024 * 1024)
    path = tmp_path / "results.json"
    path.write_bytes(raw.encode() + (b"\xff" if fault == "utf8" else b""))
    bootstrap = Mock(side_effect=AssertionError("malformed raw reached bootstrap"))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", bootstrap)
    boundary, _state = live_boundary
    calls = boundary.command.await_count
    assert recovery.main(["--memory-statistics", "--results", str(path)]) == 2
    bootstrap.assert_not_called()
    assert boundary.command.await_count == calls


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("schema", ["", "V1_0"])
def test_actual_recovery_route_requires_named_flag(
    live_boundary, tmp_path, monkeypatch, capsys, named, schema
):
    import live_test_recovery as recovery

    import hmcpctl.config as config_owner

    boundary, state = live_boundary
    state.hmc_config = boundary.hmc.config.model_copy(update={"schema_version": schema})
    boundary.hmc.config = state.hmc_config
    monkeypatch.setenv("HMC_SCHEMA_VERSION", schema)
    document = asyncio.run(exercise(live_boundary))
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document))
    for key in ("HOST", "PORT", "USER", "PASSWORD", "VERIFY_SSL", "SCHEMA_VERSION"):
        monkeypatch.delenv("HMC_" + key, raising=False)
    monkeypatch.setattr(config_owner, "load_profile", lambda: state.hmc_config)
    calls = boundary.command.await_count
    args = ["--results", str(path)] + (["--memory-statistics"] if named else [])
    assert recovery.main(args) == (0 if named else 2)
    output = capsys.readouterr()
    assert "CLEAN" not in output.out
    if named:
        assert "OBSERVED" in output.out
    else:
        assert "--memory-statistics" in output.err
        assert boundary.command.await_count == calls
