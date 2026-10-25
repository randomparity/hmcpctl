"""ST57 independently observes serial fixtures and uses guarded shared cleanup."""

import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
PARTITION = "00000002-abcd-4ef0-8abc-000000000002"
GROUP = "lpar-affinity-group"


@pytest.fixture
def target(monkeypatch):
    from live_test import lpar_config

    for name in ("_ABSENCE_REREAD_DELAY_S", "_COMPARE_REREAD_DELAY_S"):
        monkeypatch.setattr(lpar_config, name, 0)
    b = SimpleNamespace(
        name="",
        token="",
        exists=False,
        fault="",
        controller=False,
        events=[],
        values=[],
    )
    b.hmc = AsyncMock(spec=HMCClient)
    b.hmc.config = HMCConfig.from_mapping({})
    b.hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "system"}
    ]
    b.hmc.list_logical_partitions.return_value = [{"UUID": PARTITION}]
    b.hmc.get_managed_system.return_value = {
        "Resource": {
            "SystemName": "system",
            "IsClassicHMCManagement": "true",
            "IsHMCPowerVMManagementController": "false",
            "IsPowerVMManagementController": "false",
        }
    }

    def entry():
        return {
            "UUID": PARTITION,
            "Resource": {
                "PartitionName": b.name,
                "PartitionID": "7",
                "PartitionType": "AIX/Linux",
                "PartitionState": "running" if b.fault == "active" else "not activated",
            },
        }

    b.hmc.get_logical_partition.side_effect = lambda _: entry()
    b.state = SimpleNamespace(
        group=GROUP,
        hmc_config=HMCConfig.from_mapping({}),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )

    async def call(client, tool, **args):
        b.events.append(tool)
        if tool == "hmc_run_command":
            cmd = args["cmd"]
            if "affinity_group_id" in cmd:
                b.events.append("observation")
                words = shlex.split(cmd)
                kind = words[words.index("-r") + 1]
                fields = words[words.index("-F") + 1].split(",")
                assert words[words.index("--filter") + 1] == (
                    "lpar_ids=7,profile_names=default_profile"
                    if kind == "prof"
                    else "lpar_ids=7"
                )
                row = {
                    "lpar_id": "8" if b.fault == "wrong-id" else "7",
                    "lpar_name": b.name,
                    "name": "default_profile" if kind == "prof" else b.name,
                    "lpar_env": "aixlinux",
                    "affinity_group_id": "2"
                    if b.fault == "mismatch"
                    else str(b.values[-1]),
                }
                if b.fault == "unavailable":
                    return "FAIL", None
                raw = ",".join(fields) + "\n" + ",".join(row[f] for f in fields) + "\n"
                return "PASS", "\n" + raw if b.fault == "header" else raw
            if cmd.endswith("-F name"):
                return "PASS", "unrelated" + ("\n" + b.name if b.exists else "")
            if cmd.endswith("curr_avail_sys_proc_units"):
                return "PASS", "10.0"
            assert cmd.endswith("curr_avail_sys_mem,mem_region_size")
            return (
                "PASS",
                "4096,256" if b.fault == "pool-drift" and b.values else "8192,256",
            )
        if tool == "hmc_create_lpar":
            assert not b.exists, "second fixture before witnessed cleanup"
            assert args["apply_partition_profile"] is False
            b.name, b.token, b.exists = args["name"], args["caller_token"], True
            b.values.append(args["affinity_group_id"])
            if b.fault == "interrupt":
                raise InterruptedRun
            if b.fault == "unknown":
                return "FAIL", None
            return "PASS", {
                "lpar": entry(),
                "workflow_completed": b.fault != "workflow",
                "ownership_stamped": b.fault != "stamp",
                "steps": [
                    {
                        "step": "affinity_group",
                        "status": "ok",
                        "result": "malformed"
                        if b.fault == "result"
                        else {
                            "status": "verified",
                            "requested": args["affinity_group_id"],
                        },
                    }
                ],
            }
        if tool == "hmc_get_lpar_description":
            token = "foreign" if b.fault == "foreign" else b.token
            return "PASS", f"[hmcpctl owner:test created:2026-01-01] [caller {token}]"
        if tool == "hmc_get_lpar_state":
            return "PASS", "Running" if b.fault == "active" else "Not Activated"
        if tool == "hmc_get_lpar":
            return "PASS", entry()
        if tool == "hmc_delete_lpar":
            assert (
                args["lpar_name_or_uuid"] == PARTITION
                and args["system_name_or_uuid"] == "system"
            )
            if b.fault == "delete":
                return "FAIL", None
            b.exists = False
            return "PASS", {}
        raise AssertionError(f"unexpected effect: {tool}")

    b.state.call = AsyncMock(side_effect=call)
    return b


async def exercise(b, monkeypatch):
    from live_test import lpar_affinity_group as arm

    @asynccontextmanager
    async def session(config):
        yield b.hmc

    monkeypatch.setattr(arm, "HMCClient", session)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.linux_roles.run_hmc_command",
        AsyncMock(return_value="1" if b.controller else "null"),
    )
    await arm.exercise_lpar_affinity_group(object(), b.state)


@pytest.mark.asyncio
async def test_serial_independent_observations(target, monkeypatch):
    await exercise(target, monkeypatch)
    assert target.values == ["none", 1] and not target.exists
    assert target.events.count("observation") == 4
    assert target.events.count("hmc_delete_lpar") == 2
    result = target.state.record_verified.call_args.kwargs
    assert result["scenario"] == "st57-lpar-affinity-group-creation"
    assert result["data"]["authority"] == "classic"
    assert result["assertions"][0].holds and result["cleanup"] == "passed"
    assert set(result["data"]["observations"][0]) == {
        "native-partition",
        "native-profile",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "mismatch",
        "unavailable",
        "wrong-id",
        "header",
        "result",
        "workflow",
        "stamp",
        "unknown",
        "active",
        "foreign",
        "delete",
        "pool-drift",
    ],
)
async def test_failure_stops_second_fixture_and_never_claims_proof(
    target, monkeypatch, fault
):
    target.fault = fault
    await exercise(target, monkeypatch)
    assert target.values == ["none"]
    result = target.state.record_verified.call_args.kwargs
    assert not result["assertions"][0].holds
    if fault in {"active", "foreign", "delete"}:
        assert target.exists
    if fault in {"active", "foreign"}:
        assert "hmc_delete_lpar" not in target.events
    if fault in {"active", "foreign", "workflow", "stamp", "unknown"}:
        assert "observation" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [None, "all"])
async def test_opt_in_before_session(target, monkeypatch, group):
    target.state.group = group
    await exercise(target, monkeypatch)
    target.state.skip.assert_called_once()
    target.state.call.assert_not_awaited()
    assert not target.hmc.mock_calls


class InterruptedRun(BaseException):
    """Cancellation after a create must still reach guarded cleanup."""


@pytest.mark.asyncio
async def test_interruption_cleanup(target, monkeypatch):
    target.fault = "interrupt"
    with pytest.raises(InterruptedRun):
        await exercise(target, monkeypatch)
    assert not target.exists and target.values == ["none"]
    target.state.record_verified.assert_not_called()


def test_registration_preflight_recovery():
    import live_test_preflight as preflight
    import live_test_recovery as recovery
    from live_test import config, runner

    assert runner.SUBTASK_GROUPS[GROUP] == [57]
    assert runner.SUBTASKS[57].__name__ == "exercise_lpar_affinity_group"
    assert 57 not in runner.SUBTASK_GROUPS["all"]
    assert preflight._ARM_VERDICTS[GROUP](
        config.LiveTestConfig(system_name="system")
    ).runnable
    assert 57 in recovery._WITNESSED_SUBTASKS
    assert (
        recovery.lpar_config_inputs_from_document(
            {"config": {"system_name": "system"}}, [57]
        ).system_name
        == "system"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [None, "all", GROUP])
async def test_default_selection_excludes_arm_before_client(
    monkeypatch, tmp_path, group
):
    import json

    from live_test import config, runner

    class Entered(Exception):
        pass

    @asynccontextmanager
    async def forbidden():
        raise Entered
        yield

    monkeypatch.setattr(runner, "served_client", forbidden)
    monkeypatch.setattr(runner.evidence, "repository_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    result = tmp_path / "results.json"
    with pytest.raises(Entered):
        await runner.main(
            None,
            str(result),
            config=config.LiveTestConfig(system_name="system"),
            hmc_config=HMCConfig.from_mapping({}),
            group=group,
        )
    tasks = json.loads(result.read_text())["run"]["subtasks"]
    assert (57 in tasks) is (group == GROUP)


@pytest.mark.asyncio
async def test_controller_observes_only_native_partition(target, monkeypatch):
    target.controller = True
    await exercise(target, monkeypatch)
    assert target.values == ["none", 1] and not target.exists
    assert target.events.count("observation") == 2
    result = target.state.record_verified.call_args.kwargs
    assert result["assertions"][0].holds
    assert result["data"]["authority"] == "controller"
    assert result["data"]["observations"] == [
        {"native-partition": "none"},
        {"native-partition": "1"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing,clean",
    [
        ("unrelated,Running", True),
        ("hmcpctl-live-lpar-stranded,Not Activated", False),
        (None, False),
    ],
)
async def test_independent_recovery_is_read_only(listing, clean):
    import live_test_recovery as recovery

    inputs = recovery.lpar_config_inputs_from_document(
        {"config": {"system_name": "system"}}, [57]
    )
    assert inputs is not None
    calls = []

    async def call(tool, **args):
        recovery.guard_read_only(tool, args)
        calls.append((tool, args))
        return ("PASS", listing) if listing is not None else ("FAIL", None)

    if listing is None:
        with pytest.raises(recovery.StateUnreadable):
            await recovery.check_lpar_config(call, inputs)
    else:
        assert (not await recovery.check_lpar_config(call, inputs)) is clean
    assert calls == [
        ("hmc_run_command", {"cmd": "lssyscfg -r lpar -m system -F name,state"})
    ]
