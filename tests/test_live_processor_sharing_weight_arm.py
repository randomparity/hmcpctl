"""Offline ST59 custody and independent state proof; no hardware boundary is live."""

import importlib
import sys
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import ownership

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config, lpar_config, runner
from live_test.observation import CallFailure

SYSTEM = "aaaaaaaa-0000-4000-8000-000000000001"
UUID = "aaaaaaaa-0000-4000-8000-000000000059"
OTHER = "aaaaaaaa-0000-4000-8000-000000000002"
GROUP = "processor-sharing-weight"
MODES = {
    "keep_idle_procs": "keep idle procs",
    "share_idle_procs": "sre idle proces",
    "share_idle_procs_active": "sre idle procs active",
    "share_idle_procs_always": "sre idle procs always",
}


def arm_module():
    path = Path(__file__).parents[1] / "scripts/live_test/processor_sharing_weight.py"
    assert path.is_file(), "ST59 independent sharing arm must be installed"
    return importlib.import_module("live_test.processor_sharing_weight")


@pytest.fixture
def target(monkeypatch):
    # The fixture itself always exists, including at the immutable verify-red base.
    env = SimpleNamespace(
        fault="",
        events=[],
        requests=[],
        creations=[],
        name=None,
        token=None,
        exists=False,
        dedicated=False,
        mode="uncapped",
        weight=128,
        submits=0,
        token_reads=0,
        checkpoints=0,
        fail_checkpoint=None,
    )
    env.state = runner.RunState(
        config=config.LiveTestConfig(system_name="sys-R1"),
        group=GROUP,
        hmc_config=HMCConfig.from_mapping({}),
    )

    def checkpoint():
        env.events.append("checkpoint")
        env.checkpoints += 1
        if env.fail_checkpoint == env.checkpoints:
            env.fault = (
                "cleanup-exception" if env.fault == "checkpoint-cleanup" else env.fault
            )
            raise OSError("primary checkpoint failure")

    env.state.checkpoint = checkpoint
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = env.state.hmc_config
    hmc.list_system_summaries.return_value = [{"UUID": SYSTEM, "SystemName": "sys-R1"}]
    hmc.find_partition_by_name.return_value = None

    async def contained(system):
        assert system == SYSTEM
        env.events.append("contained")
        return [] if env.fault == "containment" else [{"UUID": UUID}]

    async def create(system, document):
        assert system == SYSTEM
        env.events.append("create")
        assert env.events[-2] == "checkpoint"
        assert not env.exists, "at most one retained fixture"
        root = ET.fromstring(document)
        env.creations.append(root)
        env.name = root.findtext(f"{{{UOM_NS}}}PartitionName")
        env.dedicated = (
            root.findtext(f".//{{{UOM_NS}}}HasDedicatedProcessors") == "true"
        )
        env.mode = "keep idle procs" if env.dedicated else "uncapped"
        env.weight, env.exists = 128, True
        if env.fault == "create-unknown-owned":
            env.token = env.state.results[-1]["data"]["caller_token"]
            raise HMCError("create outcome unknown after external ownership write")
        if env.fault == "create-unknown":
            raise HMCError("create outcome unknown")
        return {"UUID": UUID, "Resource": {"PartitionName": env.name}}

    async def stamp(cfg, system, name, *, agent_id, caller_token):
        assert cfg is env.state.hmc_config and system == "sys-R1" and name == env.name
        env.token = caller_token
        return None if env.fault == "stamp" else caller_token

    async def raw(partition, **kwargs):
        assert partition == UUID
        env.events.append("raw")
        if env.fault == "post-unavailable" and env.submits:
            raise HMCError("independent read unavailable")
        ident = OTHER if env.fault == "raw-identity" else UUID
        name = "foreign" if env.fault == "raw-name" else env.name
        state = "running" if env.fault == "active" else "not activated"
        kind = "OS400" if env.fault == "type" else "AIX/Linux"
        flag = str(env.dedicated).lower()
        context = (
            ""
            if env.fault == "context" or (env.fault == "late-context" and env.submits)
            else (
                f"<HasDedicatedProcessors>{flag}</HasDedicatedProcessors>"
                f"<CurrentHasDedicatedProcessors>{flag}</CurrentHasDedicatedProcessors>"
                f"<SharingMode>{env.mode}</SharingMode>"
                f"<CurrentSharingMode>{'capped' if env.fault == 'current-context' else env.mode}</CurrentSharingMode>"
                f"<SharedProcessorConfiguration><UncappedWeight>{env.weight}</UncappedWeight></SharedProcessorConfiguration>"
                f"<CurrentSharedProcessorConfiguration><CurrentUncappedWeight>{env.weight}</CurrentUncappedWeight></CurrentSharedProcessorConfiguration>"
            )
        )
        xml = (
            f'<LogicalPartition xmlns="{UOM_NS}"><Metadata><Atom><AtomID>{ident}</AtomID></Atom></Metadata>'
            f"<PartitionName>{name}</PartitionName><PartitionType>{kind}</PartitionType>"
            f"<PartitionState>{state}</PartitionState><PartitionProcessorConfiguration>{context}"
            "</PartitionProcessorConfiguration></LogicalPartition>"
        )
        if env.fault == "namespace":
            xml = xml.replace(UOM_NS, "urn:foreign")
        if env.fault == "duplicate-identity":
            xml = xml.replace("</Atom>", f"<AtomID>{UUID}</AtomID></Atom>")
        return ET.fromstring(xml), '"etag"'

    async def call(state, client, tool, **args):
        env.events.append(tool)
        if tool == "hmc_run_command":
            if "-F name" in args["cmd"]:
                return "PASS", "existing" + ("\n" + env.name if env.exists else "")
            if "proc" in args["cmd"]:
                return "PASS", "8.0"
            return (
                "PASS",
                "8192,256" if env.fault != "baseline" or not env.name else "4096,256",
            )
        if tool == "hmc_get_lpar_description":
            env.token_reads += 1
            token = (
                "foreign"
                if env.fault == "foreign" or (env.fault == "late-token" and env.submits)
                else env.token
            )
            return (
                "PASS",
                f"[hmcpctl owner:hmcpctl created:2026-10-25] [caller {token}]",
            )
        if tool == "hmc_get_lpar_state":
            if env.fault == "cleanup-exception":
                raise RuntimeError("cleanup boundary failed")
            return "PASS", "running" if env.fault in {
                "active",
                "cleanup-active",
            } else "not activated"
        if tool == "hmc_get_lpar":
            if env.fault == "entry-unavailable":
                return "FAIL", None
            return "PASS", {
                "UUID": OTHER if env.fault == "entry-identity" else UUID,
                "Resource": {
                    "PartitionName": "foreign"
                    if env.fault == "entry-name"
                    else env.name,
                    "PartitionState": "not activated",
                    "PartitionType": "AIX/Linux",
                },
            }
        if tool == "hmc_delete_lpar":
            if env.fault == "delete":
                return "FAIL", None
            env.exists = False
            return "PASS", {}
        assert tool == "hmc_dlpar_proc", "no profile apply, activation or other tool"
        assert (
            args["system_name_or_uuid"] == "sys-R1"
            and args["lpar_name_or_uuid"] == UUID
        )
        assert env.events[-2] == "hmc_get_lpar_description", (
            "fresh token immediately before submission"
        )
        assert env.events[-5:] == [
            "contained",
            "raw",
            "checkpoint",
            "hmc_get_lpar_description",
            "hmc_dlpar_proc",
        ], "durable checkpoint after fresh read and before every submission"
        env.submits += 1
        resources = args["resources"]
        env.requests.append(resources)
        weight, mode = resources.get("uncapped_weight"), resources.get("sharing_mode")
        invalid = weight == 256 or (
            weight is not None and (env.dedicated or mode == "capped")
        )
        if invalid:
            if env.fault == "negative-mutates":
                env.weight = 17
            message = (
                "uncapped_weight must be an integer from 0 to 255"
                if weight == 256
                else (
                    "Capping and uncapped_weight apply only to a shared-processor partition"
                    if env.dedicated
                    else "uncapped_weight requires uncapped mode; request explicit uncapping"
                )
            )
            if env.fault == "negative-transport":
                message = "Timeout connecting to HMC"
            return "FAIL", CallFailure(
                "ToolError",
                "ToolError: Error calling tool 'hmc_dlpar_proc': " + message,
                "",
                None,
                False,
            )
        if env.fault != "stale":
            if mode is not None:
                env.mode = MODES.get(mode, mode)
            if weight is not None:
                env.weight = weight
        return "PASS", {
            "outcome": "applied",
            "workflow_completed": True,
            "after": "untrusted echo",
        }

    @asynccontextmanager
    async def session(cfg):
        assert cfg is env.state.hmc_config
        yield hmc

    env.session = session
    monkeypatch.setattr(hmc, "list_logical_partitions", contained)
    monkeypatch.setattr(hmc, "create_logical_partition", create)
    monkeypatch.setattr(hmc, "read_logical_partition_document", raw)
    monkeypatch.setattr(ownership, "stamp_lpar_ownership", stamp)
    monkeypatch.setattr(runner.RunState, "call", call)
    monkeypatch.setattr(lpar_config, "_ABSENCE_REREAD_DELAY_S", 0)
    monkeypatch.setattr(lpar_config, "_COMPARE_REREAD_DELAY_S", 0)
    return env


async def exercise(target, monkeypatch):
    arm = arm_module()
    monkeypatch.setattr(arm, "HMCClient", target.session)
    await arm.exercise_processor_sharing_weight(object(), target.state)
    return target.state


def passed(state):
    return [
        item for item in state.observations if item["observation"]["result"] == "passed"
    ]


@pytest.mark.asyncio
async def test_sharing_arm(target, monkeypatch):
    state = await exercise(target, monkeypatch)
    assert len(target.creations) == 2 and not target.exists
    assert target.requests == [
        {"sharing_mode": "uncapped", "uncapped_weight": 64},
        {"uncapped_weight": 0},
        {"sharing_mode": "capped"},
        {"sharing_mode": "uncapped", "uncapped_weight": 255},
        {"sharing_mode": "capped", "uncapped_weight": 0},
        {"uncapped_weight": 256},
        {"sharing_mode": "keep_idle_procs"},
        {"sharing_mode": "share_idle_procs"},
        {"sharing_mode": "share_idle_procs_active"},
        {"sharing_mode": "share_idle_procs_always"},
        {"uncapped_weight": 256},
        {"uncapped_weight": 0},
    ]
    observations = passed(state)
    assert len(observations) == 2
    assert {item["operation"] for item in observations} == {"lpar.dlpar_proc"}
    assert {item["observation"]["scenario"] for item in observations} == {
        "st59-lpar-shared-weight",
        "st59-lpar-dedicated-sharing",
    }
    ids = [item["observation"]["id"] for item in observations]
    assert ids == [
        "st59-hmc-dlpar-proc-shared-weight",
        "st59-hmc-dlpar-proc-dedicated-sharing",
    ]
    assert all(item["observation"]["cleanup"] == "passed" for item in observations)
    assert target.events.count("raw") == 26
    assert target.checkpoints == 32
    assert all(
        target.events[index + 1] == "checkpoint"
        for index, event in enumerate(target.events)
        if event == "raw"
    ), "each known structural observation must be persisted before proceeding"
    for index, root in enumerate(target.creations):
        assert root.findtext(f".//{{{UOM_NS}}}MinimumMemory") == "1024"
        assert root.findtext(f".//{{{UOM_NS}}}DesiredMemory") == "2048"
        assert root.findtext(f".//{{{UOM_NS}}}MaximumMemory") == "4096"
        assert root.findtext(f".//{{{UOM_NS}}}HasDedicatedProcessors") == (
            "false" if index == 0 else "true"
        )
    assert all(
        "_" not in assertion
        for item in observations
        for assertion in item["observation"]["assertions"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "containment",
        "foreign",
        "entry-identity",
        "entry-unavailable",
        "entry-name",
        "raw-identity",
        "raw-name",
        "duplicate-identity",
        "namespace",
        "type",
        "active",
        "context",
        "current-context",
    ],
)
@pytest.mark.asyncio
async def test_sharing_arm_refuses_unknown_or_foreign_context(
    target, monkeypatch, fault
):
    target.fault = fault
    state = await exercise(target, monkeypatch)
    assert target.requests == []
    assert not passed(state)


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "late-context",
        "post-unavailable",
        "late-token",
        "negative-transport",
        "negative-mutates",
        "delete",
        "cleanup-active",
        "baseline",
        "stamp",
        "create-unknown",
    ],
)
@pytest.mark.asyncio
async def test_sharing_arm_never_promotes_incomplete_evidence(
    target, monkeypatch, fault
):
    target.fault = fault
    state = await exercise(target, monkeypatch)
    assert not passed(state)
    assert len(target.creations) == 1, (
        "failed sequence or cleanup cannot start next fixture"
    )
    if fault == "late-token":
        assert len(target.requests) == 1
    if fault in {"create-unknown", "stamp"}:
        assert not target.requests


@pytest.mark.parametrize("group", [None, "all", "lpar-memory-quantities"])
@pytest.mark.asyncio
async def test_sharing_arm_named_only(target, monkeypatch, group):
    target.state.group = group
    await exercise(target, monkeypatch)
    assert target.events == [] and not passed(target.state)


@pytest.mark.parametrize("at", [1, 2, 3, 4, 5, 8, 13, 16])
@pytest.mark.parametrize("cleanup", ["", "checkpoint-cleanup", "cleanup-active"])
@pytest.mark.asyncio
async def test_sharing_checkpoint_failure_never_starts_the_next_effect(
    target, monkeypatch, at, cleanup
):
    target.fail_checkpoint = at
    target.fault = cleanup
    with pytest.raises(OSError, match="primary checkpoint failure"):
        await exercise(target, monkeypatch)
    assert len(target.creations) == (0 if at == 1 else 1)
    assert not passed(target.state)
    failure_index = [
        i for i, event in enumerate(target.events) if event == "checkpoint"
    ][at - 1]
    assert not any(
        event in {"create", "hmc_dlpar_proc"}
        for event in target.events[failure_index + 1 :]
    )
    if at == 1:
        assert "hmc_delete_lpar" not in target.events
    elif cleanup and not (at == 16 and cleanup == "checkpoint-cleanup"):
        assert target.exists and "hmc_delete_lpar" not in target.events
        assert any(
            row["status"] == "FAIL"
            and "cleanup" in str(row).lower()
            or "teardown" in str(row).lower()
            for row in target.state.results
        )
    else:
        assert not target.exists and "hmc_delete_lpar" in target.events


@pytest.mark.asyncio
async def test_sharing_missing_checkpoint_never_creates(target, monkeypatch):
    target.state.checkpoint = None
    await exercise(target, monkeypatch)
    assert not target.creations and not target.requests and not passed(target.state)


def test_sharing_runner_registration():
    arm = arm_module()
    assert arm.SUBTASK == 59 and arm.GROUP == GROUP
    assert runner.SUBTASKS.get(59) is arm.exercise_processor_sharing_weight
    assert runner.SUBTASK_GROUPS.get(GROUP) == [59]
    assert 59 not in runner.SUBTASK_GROUPS["all"]
    assert runner._parse_arguments(["--group", GROUP]).group == GROUP


@pytest.mark.asyncio
async def test_sharing_default_dispatch_excludes_59(monkeypatch, tmp_path):
    # Run the real dispatcher while replacing its transport boundary and each other arm.
    arm = arm_module()
    seen = []

    async def task(client, state):
        seen.append(1)

    async def forbidden(client, state):
        raise AssertionError("ST59 was dispatched without its named group")

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield object()

    monkeypatch.setattr(runner, "served_client", client)
    monkeypatch.setattr(runner, "served_schemas", AsyncMock(return_value={}))
    monkeypatch.setattr(runner, "SUBTASKS", {1: task, 59: forbidden})
    monkeypatch.setattr(
        runner.scenario_validation, "validate_declared_outcomes", lambda _: None
    )
    monkeypatch.setattr(runner.evidence, "repository_root", lambda: None)
    assert arm.SUBTASK == 59
    assert (
        await runner.main(
            config=config.LiveTestConfig(),
            hmc_config=HMCConfig.from_mapping({}),
            results_path=str(tmp_path / "results.json"),
        )
        == 0
    )
    assert seen == [1]


@pytest.mark.asyncio
async def test_sharing_unknown_create_adopts_only_current_matching_token(
    target, monkeypatch
):
    target.fault = "create-unknown-owned"
    state = await exercise(target, monkeypatch)
    assert len(target.creations) == 1 and not target.exists
    assert not target.requests and not passed(state)
    assert "hmc_delete_lpar" in target.events


@pytest.mark.asyncio
async def test_sharing_checkpoint_before_next_fixture_preserves_only_completed_variant(
    target, monkeypatch
):
    target.fail_checkpoint = 17
    with pytest.raises(OSError, match="primary checkpoint failure"):
        await exercise(target, monkeypatch)
    assert len(target.creations) == 1 and not target.exists
    assert len(passed(target.state)) == 1
    assert (
        passed(target.state)[0]["observation"]["scenario"] == "st59-lpar-shared-weight"
    )
