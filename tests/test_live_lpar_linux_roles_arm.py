"""C05 synthetic lifecycle through actual MCP tools and external-only HMC doubles."""

import asyncio
import json
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import leaf_text, parse_feed

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_linux_roles as arm
from live_test import runner
from live_test.config import LiveTestConfig
from live_test.observation import CallFailure

SYSTEM = "0000000e-abcd-4ef0-8abc-00000000000e"
PARTITION = "0000000c-abcd-4ef0-8abc-00000000000c"


def captured(name):
    path = Path(__file__).parent / "fixtures" / "live" / name
    return parse_feed(json.loads(path.read_text())["body"])[0]


@pytest.fixture
def external_hmc(monkeypatch):
    """All eligibility is synthetic; captured hardware alone is ineligible."""
    system = captured("rest-ms-entry-mtms-pair.json")
    system["UUID"] = SYSTEM
    system["Resource"].update(
        SystemName="sys-R1", SystemFirmware="VL1060_FW1060.10 (1)"
    )
    system["Resource"]["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = "true"
    template = captured("rest-lpar-entry.json")
    template["UUID"] = PARTITION
    template["Resource"].update(
        PendingProcessorCompatibilityMode="POWER10",
        PartitionState="not activated",
        IsOperationInProgress="false",
        PowerVMManagementCapable="false",
        KvmCapable="false",
    )
    template["Resource"]["PartitionProcessorConfiguration"].update(
        HasDedicatedProcessors="true",
        SharingMode="keep idle procs",
        DedicatedProcessorConfiguration={
            "MinimumProcessors": "1",
            "DesiredProcessors": "1",
            "MaximumProcessors": "1",
        },
    )
    b = SimpleNamespace(
        system=system,
        entry=None,
        template=template,
        description="",
        commands=[],
        deletes=[],
        failure=None,
        updates=0,
        reads=0,
        baseline_reads=0,
    )

    async def session(self):
        return self

    async def close(self, *args):
        return None

    async def get_system(self, uuid):
        assert uuid == SYSTEM
        return deepcopy(b.system)

    async def systems(self, **kwargs):
        return [{"UUID": SYSTEM, "SystemName": "sys-R1"}]

    async def console(self):
        return {
            "Resource": {
                "VersionInfo": {
                    "Version": "10",
                    "Release": "3",
                    "ServicePackName": "1060",
                    "Maintenance": "1",
                }
            }
        }

    async def find(self, name, **kwargs):
        assert kwargs.get("system_uuid") == SYSTEM
        return (
            deepcopy(b.entry)
            if b.entry and b.entry["Resource"]["PartitionName"] == name
            else None
        )

    async def listing(self, uuid, **kwargs):
        assert uuid == SYSTEM
        return [deepcopy(b.entry)] if b.entry else []

    async def get(self, uuid, **kwargs):
        assert uuid == PARTITION
        b.reads += 1
        if b.failure == "read-transport" and b.updates:
            raise HMCError("synthetic transport unavailable")
        return deepcopy(b.entry)

    async def quick(self, kind, uuid, prop):
        assert (
            kind == "LogicalPartition"
            and uuid == PARTITION
            and prop == "PartitionState"
        )
        return leaf_text(b.entry["Resource"][prop])

    async def delete(self, uuid):
        assert uuid == PARTITION and b.entry is not None
        b.deletes.append(uuid)
        if b.failure == "cleanup":
            raise HMCError("synthetic delete failed")
        b.entry = None

    for name, replacement in {
        "__aenter__": session,
        "__aexit__": close,
        "get_managed_system": get_system,
        "list_system_summaries": systems,
        "get_console_info": console,
        "find_partition_by_name": find,
        "list_logical_partitions": listing,
        "get_logical_partition": get,
        "get_quick_property": quick,
        "delete_logical_partition": delete,
    }.items():
        monkeypatch.setattr(HMCClient, name, replacement)

    async def remote(config, command, **kwargs):
        b.commands.append(command)
        if command.startswith("mksyscfg "):
            fields = dict(
                item.split("=", 1) for item in shlex.split(command)[-1].split(",")
            )
            assert b.entry is None
            b.entry = deepcopy(b.template)
            b.entry["Resource"].update(
                PartitionName=fields["name"],
                KvmCapable="true" if fields.get("kvm_capable") == "1" else "false",
                PowerVMManagementCapable="true"
                if fields.get("powervm_mgmt_capable") == "1"
                else "false",
            )
            if b.failure == "creation-role":
                b.entry["Resource"]["PowerVMManagementCapable"] = "true"
            if b.failure == "resources":
                b.entry["Resource"]["PartitionProcessorConfiguration"][
                    "SharingMode"
                ] = "share idle procs"
            if b.failure == "create-unknown":
                raise HMCError("synthetic lost creation response")
            return ""
        if "-o apply" in command:
            return ""
        if command.startswith("chsyscfg "):
            fields = dict(
                item.split("=", 1) for item in shlex.split(command)[-1].split(",")
            )
            if "description" in fields:
                b.description = fields["description"]
                return ""
            b.updates += 1
            if b.failure == "cancel":
                raise asyncio.CancelledError
            if b.failure == "write-transport":
                raise HMCError("synthetic lost update response")
            for field, rest in (
                ("kvm_capable", "KvmCapable"),
                ("powervm_mgmt_capable", "PowerVMManagementCapable"),
            ):
                if field in fields and not (
                    b.failure == "lost-false" and fields[field] == "0"
                ):
                    b.entry["Resource"][rest] = (
                        "true" if fields[field] == "1" else "false"
                    )
            if b.failure == "preserved-drift":
                b.entry["Resource"]["PartitionMemoryConfiguration"]["DesiredMemory"] = (
                    "8192"
                )
            return ""
        if command.startswith("chsysstate "):
            assert "-o apply" in command
            return ""
        if command.endswith("-F description"):
            if not b.entry:
                raise HMCCLIError("HSCL8012 The partition does not exist")
            if b.failure == "foreign" and b.reads > 1:
                return "[hmcpctl owner:foreign created:2026-10-24]"
            if b.failure == "active" and b.reads > 1:
                b.entry["Resource"]["PartitionState"] = "running"
            return b.description
        if command.endswith("-F uuid,name"):
            return f"{SYSTEM},sys-R1"
        if command == "lssyscfg -r sys -F name":
            return "sys-R1"
        if command.endswith("-F lpar_proc_compat_modes"):
            return "POWER10"
        if command.endswith(
            "-F desired_lpar_proc_compat_mode,curr_lpar_proc_compat_mode,default_profile"
        ):
            return "POWER10,POWER10,default_profile"
        if command.endswith("-F lpar_proc_compat_mode"):
            return "default" if b.failure == "companion" else "POWER10"
        if "-F lpar_env,allow_perf_collection," in command:
            return "aixlinux,0,0,0,0,0"
        if command.endswith("-F name,state"):
            scratch = (
                "\n"
                + b.entry["Resource"]["PartitionName"]
                + ","
                + leaf_text(b.entry["Resource"]["PartitionState"])
                if b.entry
                else ""
            )
            return "original-R1,running" + scratch
        if command.endswith("-F name"):
            b.baseline_reads += 1
            if b.failure == "stranded" and not b.entry:
                return "original-R1\nhmcpctl-live-lpar-stranded"
            return (
                "original-R1\n" + b.entry["Resource"]["PartitionName"]
                if b.entry
                else "original-R1"
            )
        if command.endswith("-F curr_avail_sys_proc_units"):
            if b.failure == "baseline-unavailable":
                raise HMCError("synthetic baseline unavailable")
            return "30" if b.entry else "32"
        if command.endswith("-F curr_avail_sys_mem,mem_region_size"):
            return (
                "30720,256"
                if b.entry or (b.failure == "baseline" and b.baseline_reads > 1)
                else "32768,256"
            )
        raise AssertionError(f"unexpected external SSH: {command}")

    async def arbitrary(command, config):
        return await remote(config, command)

    for module in ("hmcpctl.ssh.lpar", "hmcpctl.ssh.profiles"):
        monkeypatch.setattr(module + ".run_hmc_command", remote)
    monkeypatch.setattr("hmcpctl.server_tools.command.run_hmc_cli", arbitrary)
    monkeypatch.setattr("live_test.lpar_config._COMPARE_REREAD_DELAY_S", 0)
    monkeypatch.setattr("live_test.lpar_config._ABSENCE_REREAD_DELAY_S", 0)
    for name, value in {
        "HMC_HOST": "hmc.test",
        "HMC_USER": "user",
        "HMC_PASSWORD": "password",  # pragma: allowlist secret -- synthetic fixture
        "HMC_AGENT_ID": "agent-R1",
    }.items():
        monkeypatch.setenv(name, value)
    return b


def application():
    names = [
        "hmc_create_lpar",
        "hmc_configure_lpar",
        "hmc_get_lpar",
        "hmc_get_lpar_proc_compat",
        "hmc_get_lpar_description",
        "hmc_delete_lpar",
        "hmc_run_command",
    ]
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": names,
                            "connections": ["<default>"],
                            "targets": "all-targets",
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )
    return create_mcp(policy)


async def exercise(b, *, group=arm.GROUP):
    state = runner.RunState(
        config=LiveTestConfig(system_name="sys-R1"),
        group=group,
        hmc_config=HMCConfig.from_mapping(
            {
                "host": "hmc.test",
                "user": "user",
                "password": "password",  # pragma: allowlist secret -- synthetic fixture
                "agent_id": "agent-R1",
                "verify_ssl": False,
            }
        ),
    )
    async with runner.served_client() as client:
        state.schemas = await runner.served_schemas(client)
        await arm.exercise_lpar_linux_roles(client, state)
    return state


async def recovery_findings():
    import live_test_recovery as recovery

    state = runner.RunState()
    async with runner.served_client() as client:

        async def call(tool, **kwargs):
            return await state.call(client, tool, **kwargs)

        return await recovery.check_lpar_config(
            call, recovery.LparConfigInputs("sys-R1")
        )


def test_registration_and_recovery():
    import live_test_preflight as preflight
    import live_test_recovery as recovery

    assert arm.SUBTASK == 53 and arm.GROUP == "lpar-linux-roles"
    assert runner.SUBTASKS[53] is arm.exercise_lpar_linux_roles
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [53]
    assert 53 not in runner.SUBTASK_GROUPS["all"]
    assert arm.GROUP in preflight._ARM_VERDICTS
    assert 53 in recovery._WITNESSED_SUBTASKS
    assert (
        recovery.lpar_config_inputs_from_document(
            {"config": {"system_name": "sys-R1"}}, [53]
        ).system_name
        == "sys-R1"
    )
    assert runner.SUBTASK_GROUPS["lpar-vtpm"] == [51]
    assert runner.SUBTASK_GROUPS["lpar-security-policy"] == [52]


@pytest.mark.asyncio
@pytest.mark.parametrize("controller", [False, True])
async def test_eligible_synthetic_success(external_hmc, controller):
    b = external_hmc
    if controller:
        b.system["Resource"].update(
            IsClassicHMCManagement="false", IsHMCPowerVMManagementController="true"
        )
    state = await exercise(b)
    assert b.entry is None and b.deletes == [PARTITION]
    assert await recovery_findings() == []
    creates = [cmd for cmd in b.commands if cmd.startswith("mksyscfg ")]
    assert len(creates) == 1
    for value in (
        "kvm_capable=1",
        "powervm_mgmt_capable=0",
        "lpar_proc_compat_mode=POWER10",
        "proc_mode=ded",
        "sharing_mode=keep_idle_procs",
    ):
        assert value in creates[0]
    assert "all_resources" not in creates[0]
    assert ("profile_name=" in creates[0]) is (not controller)
    assert sum("-o apply" in cmd for cmd in b.commands) == (0 if controller else 1)
    assert b.updates == 3
    fixture_name = shlex.split(creates[0])[-1].split(",", 1)[0].removeprefix("name=")
    role_updates = [
        cmd
        for cmd in b.commands
        if cmd.startswith("chsyscfg ")
        and "description=" not in cmd
        and "-o apply" not in cmd
    ]
    assert [
        dict(item.split("=", 1) for item in shlex.split(cmd)[-1].split(","))
        for cmd in role_updates
    ] == [
        {
            "name": fixture_name,
            "kvm_capable": "0",
        },
        {
            "name": fixture_name,
            "powervm_mgmt_capable": "1",
        },
        {
            "name": fixture_name,
            "powervm_mgmt_capable": "0",
        },
    ]
    assert b.reads >= 8
    assert len(state.observations) == 3
    assert all(
        row["result"] == "passed"
        for row in state.results
        if row["tool"] in {"hmc_create_lpar", "hmc_configure_lpar", "hmc_get_lpar"}
    )
    assert not any(
        cmd.startswith(("chsysstate -r lpar -o on", "chhwres", "chkey"))
        for cmd in b.commands
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["captured", "authority", "firmware", "hmc"])
async def test_ineligible_before_any_fixture(external_hmc, admission, monkeypatch):
    b = external_hmc
    if admission == "captured":
        b.system["Resource"] = captured("rest-ms-entry-mtms-pair.json")["Resource"]
        b.system["Resource"]["SystemName"] = "sys-R1"
    elif admission == "authority":
        b.system["Resource"].pop("IsClassicHMCManagement")
    elif admission == "firmware":
        b.system["Resource"]["SystemFirmware"] = "unknown"
    else:

        async def unknown(self):
            return {"Resource": {}}

        monkeypatch.setattr(HMCClient, "get_console_info", unknown)
    state = await exercise(b)
    assert b.entry is None and b.deletes == []
    assert not any(
        cmd.startswith(("mksyscfg", "chsyscfg", "chsysstate")) for cmd in b.commands
    )
    assert not state.observations
    assert any(row["result"] == "skipped" for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "lost-false",
        "companion",
        "write-transport",
        "read-transport",
        "create-unknown",
        "foreign",
        "active",
        "cleanup",
        "baseline",
        "preserved-drift",
        "creation-role",
        "resources",
    ],
)
async def test_failure_cannot_pass_or_replay(external_hmc, failure):
    b = external_hmc
    b.failure = failure
    state = await exercise(b)
    assert len([cmd for cmd in b.commands if cmd.startswith("mksyscfg ")]) == 1
    assert not any(
        row["result"] == "passed"
        for row in state.results
        if row["tool"] in {"hmc_create_lpar", "hmc_configure_lpar", "hmc_get_lpar"}
    )
    assert any(row["result"] == "failed" for row in state.results)
    assert b.baseline_reads >= 2
    expected_updates = 0
    if failure in {"cleanup", "baseline"}:
        expected_updates = 3
    elif failure in {
        "lost-false",
        "write-transport",
        "read-transport",
        "preserved-drift",
    }:
        expected_updates = 1
    assert b.updates == expected_updates
    assert len(b.deletes) <= 1
    assert not any(cmd.startswith("chsysstate") for cmd in b.commands)
    if failure in {"foreign", "active", "create-unknown", "read-transport"}:
        assert b.deletes == [] and b.entry is not None
        assert len(await recovery_findings()) == 1
    elif failure != "cleanup":
        assert b.deletes == [PARTITION] and b.entry is None


@pytest.mark.asyncio
async def test_cancelled_transport_attempts_cleanup_and_requires_recovery(external_hmc):
    b = external_hmc
    b.failure = "cancel"
    state = await exercise(b)
    assert b.entry is not None and b.deletes == []
    assert any(
        row["tool"] == "scratch partition teardown" and row["status"] == "FAIL"
        for row in state.results
    )
    assert any(
        row["tool"] == "system baseline compare" and row["status"] == "FAIL"
        for row in state.results
    )
    assert len(await recovery_findings()) == 1
    assert state.observations and all(
        row["observation"]["result"] == "failed" for row in state.observations
    )


@pytest.mark.asyncio
async def test_actual_runstate_toolerror_reason_and_unchanged_guard(external_hmc):
    b = external_hmc
    b.system["Resource"]["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = (
        "false"
    )
    state = runner.RunState()
    before = deepcopy(b.system)
    async with Client(application()) as client:
        status, failure = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=SYSTEM,
            name="scratch",
            settings={"kvm_capable": False},
        )
    assert status == "FAIL" and isinstance(failure, CallFailure)
    assert failure.exception_type == "ToolError"
    assert (
        failure.message
        == "ToolError: Error calling tool 'hmc_create_lpar': KVM selection requires positive system capability and HMC >=10.3.1060.1"
    )
    assert (
        b.system == before
        and b.entry is None
        and b.commands == ["lssyscfg -r sys -m sys-R1 -F lpar_proc_compat_modes"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stranded", "baseline-unavailable"])
async def test_baseline_refusal_before_create(external_hmc, failure):
    b = external_hmc
    b.failure = failure
    state = await exercise(b)
    assert not b.entry and not b.deletes and not state.observations
    assert any(row["result"] == "skipped" for row in state.results)
    assert not any(
        cmd.startswith(("mksyscfg", "chsyscfg", "chsysstate")) for cmd in b.commands
    )


@pytest.mark.asyncio
async def test_opt_in_precedes_any_external_request(external_hmc):
    b = external_hmc
    state = await exercise(b, group="all")
    assert b.commands == [] and not b.entry and not b.deletes
    assert not state.observations and state.results[0]["result"] == "skipped"
