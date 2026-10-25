"""Offline ST55 proof through shared admission, identity and cleanup logic."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.xmlutil import ATOM_NS

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
PARTITION = "00000002-abcd-4ef0-8abc-000000000002"
OTHER = "00000003-abcd-4ef0-8abc-000000000003"
GROUP = "lpar-hardware-memory"


@pytest.fixture
def target(monkeypatch):
    from live_test import lpar_config
    from live_test import lpar_hardware_memory as arm

    env = SimpleNamespace(
        arm=arm, name="", token="", exists=False, fault="", events=[], token_reads=0
    )
    hmc = AsyncMock(spec=HMCClient)
    hmc.list_system_summaries.return_value = [{"SystemName": "system", "UUID": SYSTEM}]

    def entry():
        return {
            "UUID": OTHER if env.fault == "entry-identity" else PARTITION,
            "Resource": {
                "PartitionName": "foreign" if env.fault == "entry-name" else env.name
            },
        }

    async def contained(system):
        assert system == SYSTEM
        env.events.append("contained")
        return [] if env.fault == "foreign-system" else [{"UUID": PARTITION}]

    async def current(partition):
        assert partition == PARTITION
        env.events.append("current")
        if env.fault == "current-unavailable":
            raise ValueError("unavailable")
        return entry()

    async def detail(path, kind, *, uuid_path_arguments):
        env.events.append(kind)
        if kind == "ManagedSystem":
            assert uuid_path_arguments == {"uuid": SYSTEM}
            if env.fault == "detail-unavailable":
                raise OSError("unavailable")
            if env.fault == "malformed":
                return "<broken"
            classic = "false" if env.fault == "tuple" else "true"
            cap = "false" if env.fault == "capability" else "true"
            compression = "false" if env.fault == "compression" else "true"
            body = (
                f"<IsClassicHMCManagement>{classic}</IsClassicHMCManagement>"
                "<IsHMCPowerVMManagementController>false</IsHMCPowerVMManagementController>"
                "<IsPowerVMManagementController>false</IsPowerVMManagementController>"
                "<AssociatedSystemCapabilities>"
                f"<HardwareMemoryEncryptionCapable>{cap}</HardwareMemoryEncryptionCapable>"
                f"<HardwareMemoryCompressionCapable>{compression}</HardwareMemoryCompressionCapable>"
                "</AssociatedSystemCapabilities>"
            )
            identity = SYSTEM
        else:
            assert kind == "LogicalPartition" and uuid_path_arguments == {
                "uuid": PARTITION
            }
            value = "false" if env.fault == "mismatch" else "true"
            state = "running" if env.fault == "active" else "not activated"
            name = "foreign" if env.fault == "detail-name" else env.name
            memory = (
                (
                    f"<MemoryEncryptionHardwareAccessEnabled>{value}</MemoryEncryptionHardwareAccessEnabled>"
                    "<MemoryExpansionHardwareAccessEnabled>true</MemoryExpansionHardwareAccessEnabled>"
                )
                if env.fault != "access-unavailable"
                else ""
            )
            body = (
                f"<PartitionName>{name}</PartitionName><PartitionType>AIX/Linux</PartitionType>"
                f"<PartitionState>{state}</PartitionState>"
                f"<PartitionMemoryConfiguration>{memory}</PartitionMemoryConfiguration>"
            )
            identity = OTHER if env.fault == "detail-identity" else PARTITION
        assert path == f"/rest/api/uom/{kind}/{uuid_path_arguments['uuid']}"
        return (
            f'<entry xmlns="{ATOM_NS}"><id>{identity}</id><content>'
            f'<{kind} xmlns="{UOM_NS}">{body}</{kind}></content></entry>'
        )

    hmc.list_logical_partitions.side_effect = contained
    hmc.get_logical_partition.side_effect = current
    hmc._get.side_effect = detail

    @asynccontextmanager
    async def session(config):
        assert config is env.state.hmc_config
        env.events.append("session")
        yield hmc

    monkeypatch.setattr(arm, "HMCClient", session)
    monkeypatch.setattr(lpar_config.asyncio, "sleep", AsyncMock())
    env.state = SimpleNamespace(
        group=GROUP,
        hmc_config=HMCConfig.from_mapping({}),
        config=SimpleNamespace(system_name="system"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )

    async def call(client, tool, **args):
        env.events.append(tool)
        if tool == "hmc_run_command":
            cmd = args["cmd"]
            if env.fault == "baseline-unavailable" and not env.name:
                return "FAIL", None
            if cmd.endswith("-F name"):
                if env.fault == "stranded" and not env.name:
                    return "PASS", "hmcpctl-live-lpar-stranded"
                return "PASS", "unrelated" + ("\n" + env.name if env.exists else "")
            if cmd.endswith("curr_avail_sys_proc_units"):
                return "PASS", "10.0"
            assert cmd.endswith("curr_avail_sys_mem,mem_region_size")
            return (
                "PASS",
                "8192,256" if env.fault != "pool-drift" or not env.name else "4096,256",
            )
        if tool == "hmc_create_lpar":
            env.name, env.token, env.exists = args["name"], args["caller_token"], True
            if env.fault == "create-exception":
                raise ValueError("creation outcome unknown")
            if env.fault == "interrupted":
                raise InterruptedRun
            if env.fault == "create-unknown":
                return "FAIL", "outcome unknown"
            if env.fault == "create-unstructured":
                return "PASS", "unstructured"
            return "PASS", {
                "lpar": {}
                if env.fault == "uuid"
                else {"UUID": "invalid" if env.fault == "invalid-uuid" else PARTITION},
                "workflow_completed": env.fault != "workflow",
                "ownership_stamped": env.fault not in {"stamp", "unstamped"},
            }
        if tool == "hmc_get_lpar_description":
            env.token_reads += 1
            if env.fault == "unstamped":
                return "PASS", "no ownership stamp"
            if env.fault == "unknown-retained" or (
                env.fault == "foreign-system" and env.token_reads > 1
            ):
                return "FAIL", None
            token = (
                "foreign"
                if env.fault == "token"
                or (env.fault == "token-on-read" and env.token_reads == 1)
                else env.token
            )
            return "PASS", f"[hmcpctl owner:test created:2026-01-01] [caller {token}]"
        if tool == "hmc_get_lpar_state":
            return "PASS", "Running" if env.fault == "active" else "Not Activated"
        if tool == "hmc_get_lpar":
            assert args["lpar_name_or_uuid"] == env.name
            return "PASS", entry()
        if tool == "hmc_delete_lpar":
            assert args == {
                "system_name_or_uuid": "system",
                "lpar_name_or_uuid": PARTITION,
            }
            if env.fault == "delete-refused":
                return "FAIL", "refused"
            env.exists = False
            return "PASS", {}
        raise AssertionError(f"unexpected effect: {tool}")

    env.state.call = AsyncMock(side_effect=call)
    env.hmc = hmc
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["capability", "compression", "tuple", "detail-unavailable", "malformed"]
)
async def test_admission_before_effects(target, fault):
    target.fault = fault
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    target.state.skip.assert_called_once()
    target.state.call.assert_not_awaited()
    target.state.record_verified.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["named-group", "config"])
async def test_opt_in_before_session(target, missing):
    if missing == "named-group":
        target.state.group = "all"
    else:
        target.state.hmc_config = None
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    assert target.events == []
    target.state.skip.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stranded", "baseline-unavailable"])
async def test_baseline_before_creation(target, fault):
    target.fault = fault
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    target.state.skip.assert_called_once()
    assert "hmc_create_lpar" not in target.events


@pytest.mark.asyncio
async def test_single_create_independent_read_and_guarded_cleanup(target):
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    creates = [
        call
        for call in target.state.call.await_args_list
        if call.args[1] == "hmc_create_lpar"
    ]
    assert len(creates) == 1
    assert creates[0].kwargs == {
        "system_name_or_uuid": "system",
        "name": target.name,
        "partition_type": "AIX/Linux",
        "resources": target.arm.RESOURCES,
        "caller_token": target.token,
        "hardware_mem_encryption": True,
        "hardware_mem_expansion": True,
        "apply_partition_profile": False,
    }
    assert target.name.removeprefix(
        target.arm.NAME_PREFIX
    ) == target.token.removeprefix(target.arm.TOKEN_PREFIX)
    assert len(target.token.removeprefix(target.arm.TOKEN_PREFIX)) == 8
    assert target.events.index("ManagedSystem") < target.events.index("hmc_create_lpar")
    assert (
        target.events.index("hmc_create_lpar")
        < target.events.index("current")
        < target.events.index("LogicalPartition")
    )
    assert target.events.count("hmc_delete_lpar") == 1 and not target.exists
    assert target.token_reads == 2
    result = target.state.record_verified.call_args
    assert result.kwargs["cleanup"] == "passed"
    assert result.kwargs["assertions"][0].holds
    assert result.kwargs["scenario"] == "st55-lpar-hardware-memory-creation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "create-exception",
        "create-unknown",
        "create-unstructured",
        "uuid",
        "invalid-uuid",
        "workflow",
        "stamp",
        "unstamped",
        "unknown-retained",
        "entry-identity",
        "entry-name",
        "foreign-system",
        "current-unavailable",
        "detail-identity",
        "detail-name",
        "access-unavailable",
        "mismatch",
        "token-on-read",
        "token",
        "active",
    ],
)
async def test_failures_never_claim_access_and_finally_uses_real_cleanup(target, fault):
    target.fault = fault
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    assert target.events.count("hmc_create_lpar") == 1
    result = target.state.record_verified.call_args
    assert not result.kwargs["assertions"][0].holds
    retained = fault in {
        "token",
        "active",
        "unstamped",
        "unknown-retained",
        "foreign-system",
    }
    assert target.exists is retained
    assert target.events.count("hmc_delete_lpar") == (0 if retained else 1)
    assert result.kwargs["cleanup"] == ("failed" if retained else "passed")
    if fault in {
        "create-exception",
        "create-unknown",
        "create-unstructured",
        "uuid",
        "workflow",
        "stamp",
        "token-on-read",
        "token",
    }:
        assert "LogicalPartition" not in target.events


class InterruptedRun(BaseException):
    """Model cancellation after creation without being caught as an arm failure."""


@pytest.mark.asyncio
async def test_interruption_still_runs_guarded_cleanup(target):
    target.fault = "interrupted"
    with pytest.raises(InterruptedRun):
        await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    assert target.events.count("hmc_create_lpar") == 1
    assert target.events.count("hmc_delete_lpar") == 1
    assert not target.exists
    target.state.record_verified.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["delete-refused", "pool-drift"])
async def test_cleanup_is_truthful(target, fault):
    target.fault = fault
    await target.arm.exercise_lpar_hardware_memory(object(), target.state)
    result = target.state.record_verified.call_args
    assert result.kwargs["assertions"][0].holds
    assert result.kwargs["cleanup"] == "failed"
    assert target.exists is (fault == "delete-refused")


def test_named_dispatch_preflight_and_independent_recovery():
    import live_test_preflight as preflight
    import live_test_recovery as recovery
    from live_test import config, runner

    assert runner.SUBTASK_GROUPS[GROUP] == [55]
    assert runner.SUBTASKS[55].__name__ == "exercise_lpar_hardware_memory"
    assert 55 not in runner.SUBTASK_GROUPS["all"]
    verdict = preflight._ARM_VERDICTS[GROUP](
        config.LiveTestConfig(system_name="system")
    )
    assert verdict.runnable
    assert 55 in recovery._WITNESSED_SUBTASKS
    inputs = recovery.lpar_config_inputs_from_document(
        {"config": {"system_name": "system"}}, [55]
    )
    assert inputs is not None and inputs.system_name == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [None, "all", GROUP])
async def test_default_selection_excludes_new_arm_before_client_entry(
    monkeypatch, tmp_path, group
):
    import json

    from live_test import config, runner

    class ClientEntry(Exception):
        pass

    @asynccontextmanager
    async def forbidden():
        raise ClientEntry
        yield  # pragma: no cover

    monkeypatch.setattr(runner, "served_client", forbidden)
    monkeypatch.setattr(runner, "SUBTASKS", {**runner.SUBTASKS, 55: AsyncMock()})
    monkeypatch.setattr(runner.evidence, "repository_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    result = tmp_path / "results.json"
    with pytest.raises(ClientEntry):
        await runner.main(
            None,
            str(result),
            config=config.LiveTestConfig(system_name="system"),
            hmc_config=HMCConfig.from_mapping({}),
            group=group,
        )
    tasks = json.loads(result.read_text())["run"]["subtasks"]
    if group == GROUP:
        assert tasks == [55]
    else:
        assert 55 not in tasks


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
        {"config": {"system_name": "system"}}, [55]
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
