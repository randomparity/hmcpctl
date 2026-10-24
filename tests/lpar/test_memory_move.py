"""Native D02 memory movement through real admission and independent readback."""

import shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_memory import LparMemoryMove
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.memory_move import move_lpar_memory
from hmcpctl.ssh.memory import move_lpar_memory_quantity

SYSTEM = "aaaaaaaa-0000-0000-0000-000000000001"
SOURCE = "bbbbbbbb-0000-0000-0000-000000000001"
TARGET = "bbbbbbbb-0000-0000-0000-000000000002"
CONFIG = HMCConfig.from_mapping({})


def entry(uuid, name, number):
    return {
        "UUID": uuid,
        "Resource": {
            "PartitionName": name,
            "PartitionID": str(number),
            "PartitionType": "AIX/Linux",
            "PartitionState": "running",
            "ResourceMonitoringControlState": "active",
            "CurrentProfileSync": "Disabled",
            "IsOperationInProgress": "false",
            "PartitionCapabilities": {"DynamicLogicalPartitionMemoryCapable": "true"},
            "PartitionMemoryConfiguration": {
                "CurrentMemory": "2048",
                "CurrentMinimumMemory": "1024",
                "CurrentMaximumMemory": "4096",
                "DesiredMemory": "2048",
                "MinimumMemory": "1024",
                "MaximumMemory": "4096",
                "RuntimeMemory": "2048",
                "SharedMemoryEnabled": "false",
            },
            "PartitionProcessorConfiguration": {
                "HasDedicatedProcessors": "true",
                "CurrentDedicatedProcessorConfiguration": {
                    "CurrentMinimumProcessors": "1",
                    "CurrentProcessors": "1",
                    "CurrentMaximumProcessors": "2",
                },
            },
        },
    }


def mem(value):
    return value["Resource"]["PartitionMemoryConfiguration"]


@pytest.fixture
def boundary(monkeypatch):
    before = {
        SOURCE: entry(SOURCE, "source-R1", 7),
        TARGET: entry(TARGET, "target-R2", 8),
    }
    after = deepcopy(before)
    for uuid, quantity in ((SOURCE, "1792"), (TARGET, "2304")):
        mem(after[uuid])["CurrentMemory"] = quantity
        mem(after[uuid])["RuntimeMemory"] = quantity
    state = SimpleNamespace(
        before=before,
        after=after,
        submitted=False,
        errors={},
        submit_error=None,
        fail_owner=None,
        drift=None,
        reads=[],
        kinds={SOURCE: "LogicalPartition", TARGET: "LogicalPartition"},
    )
    state.system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            "State": "operating",
            "AssociatedSystemMemoryConfiguration": {
                "MemoryRegionSize": "256",
                "ConfigurableSystemMemory": "16384",
                "CurrentAvailableSystemMemory": "0",
            },
        },
    }

    async def read(uuid):
        state.reads.append((uuid, state.submitted))
        if state.submitted and uuid in state.errors:
            raise state.errors[uuid]
        value = deepcopy((state.after if state.submitted else state.before)[uuid])
        if (
            not state.submitted
            and state.drift == uuid
            and state.reads.count((uuid, False)) > 1
        ):
            mem(value)["CurrentMemory"] = "1792"
        return value

    async def submit(config, command, **kwargs):
        state.submitted = True
        if state.submit_error:
            raise state.submit_error
        return ""

    async def description(config, system, name):
        owner = "foreign-owner" if name == state.fail_owner else "hmcpctl"
        return f"[hmcpctl owner:{owner} created:2026-10-24]"

    state.hmc = SimpleNamespace(
        config=CONFIG,
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        list_logical_partitions=AsyncMock(
            side_effect=lambda system: [
                deepcopy(v)
                for k, v in state.before.items()
                if state.kinds[k] == "LogicalPartition"
            ]
        ),
        list_vios=AsyncMock(
            side_effect=lambda system: [
                deepcopy(v)
                for k, v in state.before.items()
                if state.kinds[k] == "VirtualIOServer"
            ]
        ),
        get_logical_partition=AsyncMock(side_effect=read),
        get_vios=AsyncMock(side_effect=read),
        get_managed_system=AsyncMock(side_effect=lambda uuid: deepcopy(state.system)),
    )
    state.command = AsyncMock(side_effect=submit)
    state.description = AsyncMock(side_effect=description)
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", state.command)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", state.description
    )
    return state


async def move(
    boundary, source="source-R1", target="target-R2", quantity=256, force=False
):
    return await move_lpar_memory(
        boundary.hmc,
        source,
        target,
        LparMemoryMove(quantity_mib=quantity, force=force),
        system_name_or_uuid="sys-R1",
    )


@pytest.mark.parametrize(
    "values",
    [
        {"quantity_mib": -1},
        {"quantity_mib": True},
        {"quantity_mib": 1.5},
        {"quantity_mib": "256"},
        {"quantity_mib": 256, "force": 1},
        {"quantity_mib": 256, "entitled_mib": "auto"},
        {"quantity_mib": 256, "operation": "remove"},
        {},
    ],
)
def test_move_model(values):
    with pytest.raises(ValueError):
        LparMemoryMove(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_move_command(monkeypatch, force):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    await move_lpar_memory_quantity(
        CONFIG, "system; ' quoted", 7, 8, LparMemoryMove(quantity_mib=256, force=force)
    )
    assert shlex.split(command.call_args.args[1]) == [
        "chhwres",
        "-r",
        "mem",
        "-m",
        "system; ' quoted",
        "-o",
        "m",
        "--id",
        "7",
        "--tid",
        "8",
        "-q",
        "256",
        *(["--force"] if force else []),
    ]
    command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [(0, 8), (7, 0), (True, 8), (7, "8"), (7, 7)])
async def test_bad_transport_ids(monkeypatch, source, target):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    with pytest.raises(ValueError):
        await move_lpar_memory_quantity(
            CONFIG, "sys-R1", source, target, LparMemoryMove(quantity_mib=256)
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_applies_both_with_no_free_system_memory(boundary):
    result = await move(boundary)
    assert result["outcome"] == "applied" and result["workflow_completed"]
    assert result["source"]["after"]["current_memory_mib"] == 1792
    assert result["destination"]["after"]["current_memory_mib"] == 2304
    assert boundary.command.await_count == 1
    assert {call.args[2] for call in boundary.description.await_args_list} == {
        "source-R1",
        "target-R2",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,target",
    [("source-R1", SOURCE), (SOURCE, SOURCE), ("foreign", TARGET), (SOURCE, "foreign")],
)
async def test_rejects_identity(boundary, source, target):
    with pytest.raises(ValueError):
        await move(boundary, source, target)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
async def test_either_owner_denial(boundary, target):
    boundary.fail_owner = boundary.before[target]["Resource"]["PartitionName"]
    with pytest.raises(PermissionError, match="owned by"):
        await move(boundary)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,field,value",
    [
        (SOURCE, "CurrentMinimumMemory", "2048"),
        (TARGET, "CurrentMaximumMemory", "2048"),
        (SOURCE, "SharedMemoryEnabled", "true"),
        (TARGET, "SharedMemoryEnabled", "true"),
        (SOURCE, "CurrentMemory", "bad"),
        (TARGET, "CurrentMemory", "2.5"),
        (SOURCE, "CurrentMinimumMemory", None),
        (TARGET, "CurrentMaximumMemory", None),
    ],
)
async def test_admission_rejects(boundary, target, field, value):
    mem(boundary.before[target])[field] = value
    with pytest.raises(ValueError):
        await move(boundary)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [1, 4096])
async def test_quantity_alignment_and_underflow(boundary, quantity):
    with pytest.raises(ValueError):
        await move(boundary, quantity=quantity)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "starting"),
        ("ResourceMonitoringControlState", "inactive"),
        ("IsOperationInProgress", "true"),
        ("PartitionType", "unknown"),
    ],
)
async def test_state_variants(boundary, target, field, value):
    boundary.before[target]["Resource"][field] = value
    with pytest.raises(ValueError):
        await move(boundary)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
async def test_missing_capability(boundary, target):
    boundary.before[target]["Resource"]["PartitionCapabilities"] = {}
    with pytest.raises(ValueError):
        await move(boundary)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["AIX/Linux", "Virtual IO Server", "OS400"])
async def test_partition_types_and_vios_reader(boundary, kind):
    for snapshots in (boundary.before, boundary.after):
        for value in snapshots.values():
            value["Resource"]["PartitionType"] = kind
    if kind == "Virtual IO Server":
        boundary.kinds = {SOURCE: "VirtualIOServer", TARGET: "VirtualIOServer"}
    if kind == "OS400":
        for snapshots in (boundary.before, boundary.after):
            for value in snapshots.values():
                value["Resource"]["ResourceMonitoringControlState"] = "inactive"
    assert (await move(boundary))["workflow_completed"]
    if kind == "Virtual IO Server":
        boundary.hmc.get_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
async def test_force_deferred_and_ibmi_refusal(boundary, target):
    for snapshots in (boundary.before, boundary.after):
        snapshots[target]["Resource"]["ResourceMonitoringControlState"] = "inactive"
    mem(boundary.after[target])["RuntimeMemory"] = "2048"
    result = await move(boundary, force=True)
    assert result["workflow_completed"]
    peer = result["source" if target == SOURCE else "destination"]
    assert any("restart" in warning for warning in peer["warnings"])
    boundary.submitted = False
    boundary.command.reset_mock()
    boundary.before[target]["Resource"]["PartitionType"] = "OS400"
    with pytest.raises(ValueError):
        await move(boundary, force=True)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_stays_native_admission(boundary):
    for snapshots in (boundary.before, boundary.after):
        for value in snapshots.values():
            value["Resource"]["PartitionState"] = "not activated"
            value["Resource"]["ResourceMonitoringControlState"] = "inactive"
            mem(value)["RuntimeMemory"] = "0"
    result = await move(boundary)
    assert result["workflow_completed"]
    assert all(
        any("No running-guest" in w for w in result[peer]["warnings"])
        for peer in ("source", "destination")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [0, 1024])
@pytest.mark.parametrize("force", [False, True])
async def test_inactive_zero_envelope_has_no_native_submission(
    boundary, quantity, force
):
    destination = boundary.before[TARGET]
    destination["Resource"]["PartitionState"] = "not activated"
    destination["Resource"]["ResourceMonitoringControlState"] = "inactive"
    for field in (
        "CurrentMemory",
        "CurrentMinimumMemory",
        "CurrentMaximumMemory",
        "RuntimeMemory",
    ):
        mem(destination)[field] = "0"
    if quantity:
        with pytest.raises(ValueError, match="cannot verify.*zero current envelope"):
            await move(boundary, quantity=quantity, force=force)
    else:
        result = await move(boundary, quantity=quantity, force=force)
        assert result["outcome"] == "noop" and result["workflow_completed"]
        assert result["destination"]["before"] == result["destination"]["after"]
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero(boundary):
    result = await move(boundary, quantity=0)
    assert result["outcome"] == "noop" and result["workflow_completed"]
    assert result["source"]["before"] == result["source"]["after"]
    assert result["destination"]["before"] == result["destination"]["after"]
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
async def test_drift(boundary, target):
    boundary.drift = target
    with pytest.raises(ValueError, match="changed"):
        await move(boundary)
    boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed,failed,outcome",
    [
        ([], False, "unverified"),
        ([], True, "rejected"),
        ([SOURCE], False, "partial"),
        ([TARGET], True, "partial"),
        ([SOURCE, TARGET], True, "applied"),
    ],
)
async def test_outcomes(boundary, changed, failed, outcome):
    for uuid in (SOURCE, TARGET):
        if uuid not in changed:
            boundary.after[uuid] = deepcopy(boundary.before[uuid])
    if failed:
        error = HMCError("native refused")
        error.__cause__ = asyncssh.ProcessError(
            exit_status=1,
            exit_signal=None,
            returncode=1,
            command="fixed",
            subsystem=None,
            env={},
            stdout="",
            stderr="refused",
        )
        boundary.submit_error = error
    result = await move(boundary)
    assert result["outcome"] == outcome
    assert result["workflow_completed"] == (outcome == "applied")
    assert boundary.command.await_count == 1
    assert {(SOURCE, True), (TARGET, True)} <= set(boundary.reads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,signal,returncode",
    [
        (-1, ("TERM", False, "", ""), -15),
        (None, None, None),
    ],
)
async def test_uncertain_process_exit_never_means_refused(
    boundary, status, signal, returncode
):
    boundary.after = deepcopy(boundary.before)
    error = HMCError("submission terminated")
    error.__cause__ = asyncssh.ProcessError(
        env={},
        command="fixed",
        subsystem=None,
        exit_status=status,
        exit_signal=signal,
        returncode=returncode,
        stdout="",
        stderr="",
    )
    boundary.submit_error = error
    result = await move(boundary)
    assert result["outcome"] == "unverified" and not result["workflow_completed"]
    assert result["submission_error"] and boundary.command.await_count == 1
    assert {(SOURCE, True), (TARGET, True)} <= set(boundary.reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("value", [None, "malformed"])
async def test_cpu_preservation_requires_valid_evidence(boundary, target, phase, value):
    resource = getattr(boundary, phase)[target]["Resource"]
    if value is None:
        resource.pop("PartitionProcessorConfiguration")
    else:
        resource["PartitionProcessorConfiguration"][
            "CurrentDedicatedProcessorConfiguration"
        ]["CurrentProcessors"] = value
    if phase == "before":
        with pytest.raises(ValueError, match="numeric processor assignments"):
            await move(boundary)
        boundary.command.assert_not_awaited()
    else:
        result = await move(boundary)
        failed = "source" if target == SOURCE else "destination"
        peer = "destination" if target == SOURCE else "source"
        assert result["outcome"] == "unverified" and not result["workflow_completed"]
        assert result[failed]["read_error"] and result[peer]["after"] is not None
        assert {(SOURCE, True), (TARGET, True)} <= set(boundary.reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
async def test_peer_read_survives(boundary, target):
    boundary.errors[target] = HMCError("read unavailable")
    result = await move(boundary)
    assert result["outcome"] == "unverified" and not result["workflow_completed"]
    missing = "source" if target == SOURCE else "destination"
    peer = "destination" if target == SOURCE else "source"
    assert result[missing]["after"] is None
    assert result[peer]["after"] is not None
    assert {(SOURCE, True), (TARGET, True)} <= set(boundary.reads)
    assert boundary.command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [SOURCE, TARGET])
@pytest.mark.parametrize(
    "field,value",
    [
        ("CurrentMemory", "bad"),
        ("RuntimeMemory", "2048"),
        ("CurrentMinimumMemory", "768"),
        ("CurrentMaximumMemory", "4352"),
    ],
)
async def test_readback_cannot_complete_one_matching_endpoint(
    boundary, target, field, value
):
    mem(boundary.after[target])[field] = value
    result = await move(boundary)
    assert not result["workflow_completed"]
    assert result["outcome"] in ("partial", "unverified")


@pytest.mark.asyncio
async def test_profile_locations(boundary):
    for snapshots in (boundary.before, boundary.after):
        snapshots[SOURCE]["Resource"]["CurrentProfileSync"] = "On"
        snapshots[SOURCE]["Resource"]["LastActivatedProfile"] = "profile-R1"
    result = await move(boundary)
    assert (
        result["source"]["change_location"]["lives_in"]
        == "current-configuration-and-profile"
    )
    assert (
        result["destination"]["change_location"]["lives_in"] == "current-configuration"
    )
