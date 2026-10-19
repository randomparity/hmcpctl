"""Whole system-profile inventory, admission and outcome contracts (#1725)."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.systems import profiles as ops

S = "11111111-1111-4111-8111-111111111111"
L = "22222222-2222-4222-8222-222222222222"
P = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def system_profiles(monkeypatch):
    member = {
        "UUID": L,
        "Resource": {
            "PartitionID": "1",
            "PartitionName": "member-a",
            "PartitionState": "not activated",
            "PartitionType": "AIX/Linux",
            "Description": "[hmcpctl owner:test created:2026-10-19]",
        },
    }
    profile = {
        "UUID": P,
        "Resource": {
            "ProfileName": "base",
            "AssociatedPartition": {
                "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{L}"
            },
        },
    }
    h = SimpleNamespace(
        config=HMCConfig.from_mapping(
            {"host": "hmc.test", "user": "test", "agent_id": "test"}
        ),
        get_console_info=AsyncMock(
            return_value={
                "Resource": {
                    "VersionInfo": {
                        "Version": "10",
                        "Release": "3",
                        "ServicePackName": "1060",
                    }
                }
            }
        ),
        get_managed_system=AsyncMock(
            return_value={
                "UUID": S,
                "Resource": {
                    "State": "operating",
                    "MachineTypeModelAndSerialNumber": {
                        "MachineType": "9009",
                        "Model": "42A",
                        "SerialNumber": "TEST",
                    },
                },
            }
        ),
        list_logical_partitions=AsyncMock(side_effect=lambda _: deepcopy([member])),
        list_vios=AsyncMock(return_value=[]),
        list_child=AsyncMock(side_effect=lambda *_: deepcopy([profile])),
    )

    async def command(_config, cmd, **kwargs):
        assert kwargs == {"include_stderr": True}
        return (
            "name:lpar_ids:profile_names\nscratch:1:base\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    ssh = AsyncMock(side_effect=command)
    monkeypatch.setattr(ops, "run_hmc_command", ssh)
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", ssh)
    return h, member, profile, ssh


@pytest.mark.asyncio
async def test_inventory_binding(system_profiles):
    h, _, _, ssh = system_profiles
    result = await ops.list_system_profiles(h, S)
    assert (
        result.command_status == "completed"
        and result.source == "ssh-command"
        and result.job is None
    )
    assert result.records[0].name == "scratch"
    m = result.records[0].members[0]
    assert (
        m.partition_uuid,
        m.partition_id,
        m.partition_name,
        m.profile_uuid,
        m.profile_name,
    ) == (L, 1, "member-a", P, "base")
    assert h.list_logical_partitions.await_count == 2 and h.list_child.await_count == 2
    assert all(" --filter " not in c.args[1] for c in ssh.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "name:lpar_ids:profile_names\nscratch:1,2:base\n",
        "name:lpar_ids:profile_names\nscratch:0:base\n",
        "name:lpar_ids:profile_names\nscratch:1:foreign\n",
        "name:lpar_ids:profile_names\nscratch:1:base\nscratch:1:base\n",
        "No results were found.\n[stderr]\nwarning",
        "name:lpar_ids:profile_names\nscratch:1:base\n[stderr]\nwarning",
        "name:lpar_ids:profile_names\nscratch:1:base:extra\n",
    ],
)
async def test_inventory_binding_refuses_whole_invalid_inventory(system_profiles, raw):
    h, _, _, ssh = system_profiles
    original = ssh.side_effect

    async def command(config, cmd, **kwargs):
        return raw if "name:lpar_ids" in cmd else await original(config, cmd, **kwargs)

    ssh.side_effect = command
    result = await ops.list_system_profiles(h, S)
    assert result.records is None and result.command_status == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["id", "uuid", "parent", "missing-profile", "duplicate-id", "namespace", "drift"],
)
async def test_inventory_binding_current_identity(system_profiles, fault):
    h, member, profile, ssh = system_profiles
    if fault == "id":
        member["Resource"]["PartitionID"] = "invalid"
    if fault == "uuid":
        member["UUID"] = "invalid"
    if fault == "parent":
        profile["Resource"]["AssociatedPartition"]["href"] = (
            f"https://foreign.test/rest/api/uom/LogicalPartition/{L}"
        )
    if fault == "missing-profile":
        h.list_child.return_value = []
        h.list_child.side_effect = None
    if fault == "duplicate-id":
        h.list_vios.return_value = [deepcopy(member)]
    if fault == "namespace":
        original = ssh.side_effect

        async def command(c, cmd, **kw):
            return (
                "name\nother\n"
                if "name:lpar_ids" not in cmd
                else await original(c, cmd, **kw)
            )

        ssh.side_effect = command
    if fault == "drift":
        calls = 0

        async def members(_):
            nonlocal calls
            calls += 1
            r = deepcopy(member)
            if calls > 1:
                r["Resource"]["PartitionName"] = "changed"
            return [r]

        h.list_logical_partitions.side_effect = members
    assert (await ops.list_system_profiles(h, S)).records is None


@pytest.mark.asyncio
async def test_inventory_empty_and_lookup(system_profiles):
    h, _, _, ssh = system_profiles
    ssh.return_value = "No results were found."
    ssh.side_effect = None
    result = await ops.list_system_profiles(h, S)
    assert result.records == () and result.command_status == "completed"
    with pytest.raises(ValueError, match="missing"):
        await ops.list_system_profiles(h, S, name="missing")


@pytest.mark.asyncio
async def test_inventory_bounds(system_profiles):
    h, _, _, ssh = system_profiles
    ssh.return_value = "x" * (5 * 1024 * 1024 + 1)
    ssh.side_effect = None
    assert (await ops.list_system_profiles(h, S)).records is None


@pytest.mark.asyncio
async def test_inventory_binding_multirow_and_vios(system_profiles):
    h, member, profile, ssh = system_profiles
    h.list_logical_partitions.return_value = []
    h.list_logical_partitions.side_effect = None
    h.list_vios.return_value = [member]
    profile["Resource"]["AssociatedPartition"]["href"] = (
        f"https://hmc.test/rest/api/uom/ManagedSystem/{S}/VirtualIOServer/{L}"
    )

    async def command(_c, cmd, **_kw):
        return (
            (
                Path(__file__).parents[1]
                / "fixtures/system_profiles/inventory-two-rows.txt"
            ).read_text()
            if "name:lpar_ids" in cmd
            else "name\nscratch\nsecond\n"
        )

    ssh.side_effect = command
    result = await ops.list_system_profiles(h, S)
    assert (
        len(result.records) == 2
        and result.records[1].members[0].resource_type == "VirtualIOServer"
    )


@pytest.mark.asyncio
async def test_inventory_bounds_contained_feed(system_profiles):
    h, _, profile, _ = system_profiles
    profile["Resource"]["large"] = "x" * (5 * 1024 * 1024)
    assert (await ops.list_system_profiles(h, S)).records is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "window",
        "foreign",
        "missing-description",
        "duplicate",
        "missing-profile",
        "collision",
    ],
)
async def test_all_members_preflight_refusal(system_profiles, fault):
    h, member, _, _ = system_profiles
    pairs = [ops.SystemProfilePair(partition="member-a", profile="base")]
    kwargs = {"exclusive_writer_window": True}
    name = "new"
    if fault == "window":
        kwargs["exclusive_writer_window"] = False
    elif fault == "foreign":
        member["Resource"]["Description"] = "[hmcpctl owner:other created:2026-10-19]"
    elif fault == "missing-description":
        del member["Resource"]["Description"]
    elif fault == "duplicate":
        pairs *= 2
    elif fault == "missing-profile":
        pairs = [ops.SystemProfilePair(partition=L, profile="absent")]
    elif fault == "collision":
        name = "scratch"
    with pytest.raises((ValueError, PermissionError)):
        await ops._admit(h, S, name, action="create", members=pairs, **kwargs)


@pytest.mark.asyncio
async def test_all_members_update_union(system_profiles):
    h, member, _, _ = system_profiles
    member["Resource"]["Description"] = ""
    phase, old, desired = await ops._admit(
        h,
        S,
        "scratch",
        action="update",
        new_name="renamed",
        exclusive_writer_window=True,
    )
    assert (
        phase.system_uuid == S and old.name == "scratch" and desired.name == "renamed"
    )
    assert desired.members == old.members
    member["Resource"]["Description"] = "[hmcpctl owner:other created:2026-10-19]"
    with pytest.raises(PermissionError):
        await ops._admit(h, S, "scratch", action="delete", exclusive_writer_window=True)


@pytest.mark.asyncio
async def test_refreshes_before_effect(system_profiles):
    h, member, _, _ = system_profiles
    phase, _, _ = await ops._admit(h, S, "scratch", action="validate")
    member["Resource"]["PartitionState"] = "running"
    with pytest.raises(ValueError, match="changed"):
        await ops._refresh(h, phase)


def terminal(status="COMPLETED_OK", code="0", job_id="job-1", error=""):
    return {
        "Resource": {
            "JobID": job_id,
            "Status": status,
            "Results": {
                "JobParameter": [
                    {"ParameterName": "returnCode", "ParameterValue": code},
                    {"ParameterName": "ErrorData", "ParameterValue": error},
                ]
            },
        },
        "link": f"/rest/api/uom/jobs/{job_id}",
    }


@pytest.mark.parametrize(
    "fault", ["foreign-link", "wrong-id", "warning", "missing-code", "error"]
)
def test_job_and_member_outcomes_refuse_false_success(system_profiles, fault):
    h, _, _, _ = system_profiles
    job = terminal()
    if fault == "foreign-link":
        job["link"] = "https://foreign.test/rest/api/uom/jobs/job-1"
    if fault == "wrong-id":
        job["Resource"]["JobID"] = "other"
    if fault == "warning":
        job["Resource"]["Status"] = "COMPLETED_WITH_WARNINGS"
    if fault == "missing-code":
        job["Resource"]["Results"]["JobParameter"] = []
    if fault == "error":
        job["Resource"]["Results"]["JobParameter"][1]["ParameterValue"] = "diagnostic"
    with pytest.raises(ValueError):
        ops._job_facts(h, job, "job-1")


def activation_fact(state, profile, memory, uuid=P):
    return {
        "resource": {
            "PartitionState": state,
            "LastActivatedProfile": profile,
            "AssociatedPartitionProfile": {
                "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{L}/LogicalPartitionProfile/{uuid}"
            },
            "PartitionMemoryConfiguration": {
                "SharedMemoryEnabled": "false",
                "CurrentMemory": str(memory),
                "RuntimeMemory": str(memory),
            },
            "PartitionProcessorConfiguration": {
                "CurrentHasDedicatedProcessors": "false",
                "CurrentSharingMode": "capped",
                "CurrentSharedProcessorConfiguration": {
                    "AllocatedVirtualProcessors": "1",
                    "CurrentProcessingUnits": "0.05",
                },
            },
        },
        "cli_profile": profile,
        "profile": {
            "ProfileMemory": {
                "DesiredMemory": "1024",
                "ActiveMemorySharingEnabled": "false",
            },
            "ProcessorAttributes": {
                "HasDedicatedProcessors": "false",
                "SharingMode": "capped",
                "SharedProcessorConfiguration": {
                    "DesiredVirtualProcessors": "1",
                    "DesiredProcessingUnits": "0.05",
                },
            },
        },
    }


@pytest.mark.parametrize(
    "fault", [None, "stale", "starting", "memory", "profile", "missing"]
)
def test_job_and_member_outcomes_witness(system_profiles, fault):
    h, _, _, _ = system_profiles
    member = ops.SystemProfileMember("LogicalPartition", L, 1, "member-a", P, "base")
    before = activation_fact("not activated", "", 0)
    before["resource"].pop("AssociatedPartitionProfile")
    after = activation_fact("open firmware", "base", 1024)
    if fault == "stale":
        before["cli_profile"] = "base"
    if fault == "starting":
        after["resource"]["PartitionState"] = "Starting"
    if fault == "memory":
        after["resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "512"
    if fault == "profile":
        after["resource"]["LastActivatedProfile"] = "other"
    if fault == "missing":
        del after["resource"]["PartitionMemoryConfiguration"]["RuntimeMemory"]
    assert ops._new_witness(h, member, before, after) is (fault is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "failed", "timeout", "wrong-job"])
async def test_create_outcomes_one_effect(system_profiles, failure):
    h, _, _, ssh = system_profiles
    created = False

    async def command(_config, cmd, **kwargs):
        if "name:lpar_ids" in cmd:
            return "name:lpar_ids:profile_names\n" + ("new:1:base\n" if created else "")
        return "name\n" + ("new\n" if created else "")

    async def submit(path, xml):
        nonlocal created
        assert path == f"/rest/api/uom/ManagedSystem/{S}/do/CreateSystemProfile"
        created = True
        if failure == "timeout":
            raise TimeoutError("unknown submission")
        return terminal(job_id="job-1")

    h.submit_job = AsyncMock(side_effect=submit)
    h.wait_for_job_entry = AsyncMock(
        return_value=terminal(
            status="FAILED_TO_START" if failure == "failed" else "COMPLETED_OK",
            job_id="other" if failure == "wrong-job" else "job-1",
        )
    )
    ssh.side_effect = command
    result = await ops.create_system_profile(
        h,
        S,
        "new",
        [ops.SystemProfilePair(partition=L, profile=P)],
        exclusive_writer_window=True,
    )
    assert h.submit_job.await_count == 1
    assert result.state == (
        "completed"
        if failure is None
        else "failed"
        if failure == "failed"
        else "uncertain"
    )
    assert result.postcondition == ("verified" if failure is None else "unverified")
    assert result.job is not None if failure != "timeout" else result.job is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "diagnostic", "nonzero", "drift"])
async def test_delete_outcomes(system_profiles, fault):
    import asyncssh

    from hmcpctl.ssh.transport import HMCCLIError

    h, member, _, ssh = system_profiles
    deleted = False

    async def command(_config, cmd, **kwargs):
        nonlocal deleted
        if cmd.startswith("rmsyscfg"):
            deleted = True
            if fault == "diagnostic":
                return "\n[stderr]\ndiagnostic"
            if fault == "nonzero":
                try:
                    raise asyncssh.ProcessError(None, None, None, 1, None, 1, "", "")
                except asyncssh.ProcessError as exc:
                    raise HMCCLIError("failed") from exc
            if fault == "drift":
                member["Resource"]["Description"] = "changed"
            return ""
        return (
            "No results were found.\n"
            if deleted
            else "name:lpar_ids:profile_names\nscratch:1:base\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    ssh.side_effect = command
    result = await ops.delete_system_profile(
        h, S, "scratch", exclusive_writer_window=True
    )
    assert sum(c.args[1].startswith("rmsyscfg") for c in ssh.await_args_list) == 1
    assert result.job is None and result.return_code is None
    assert result.state == (
        "completed"
        if fault is None
        else "failed"
        if fault == "nonzero"
        else "uncertain"
    )
    assert result.command_status == (
        "completed"
        if fault in (None, "drift")
        else "failed"
        if fault == "nonzero"
        else "uncertain"
    )


@pytest.mark.parametrize(
    "backend,case,expected",
    [
        ("completed", "new", "complete"),
        ("failed", "new", "unknown"),
        ("failed", "mixed", "partial"),
        ("failed", "unchanged", "failed"),
        ("completed", "missing", "unknown"),
    ],
)
def test_job_and_member_outcomes_aggregate(system_profiles, backend, case, expected):
    h, _, _, _ = system_profiles
    m = ops.SystemProfileMember("LogicalPartition", L, 1, "member-a", P, "base")
    before = activation_fact("not activated", "", 0)
    before["resource"].pop("AssociatedPartitionProfile")
    after = activation_fact("running", "base", 1024)
    old = {"a": before, "b": activation_fact("open firmware", "other", 1024)}
    other = deepcopy(old["b"])
    other["profile"]["ProfileMemory"]["DesiredMemory"] = "1024"
    other["profile"]["ProfileName"] = "other"
    other["requested_profile"] = {"ProfileMemory": {"DesiredMemory": "512"}}
    other["profile_uuid"] = "44444444-4444-4444-8444-444444444444"
    other["resource"]["AssociatedPartitionProfile"]["href"] = (
        "https://hmc.test/rest/api/uom/LogicalPartitionProfile/44444444-4444-4444-8444-444444444444"
    )
    new = {"a": after, "b": other}
    members = (
        m,
        ops.SystemProfileMember(
            "LogicalPartition",
            "55555555-5555-4555-8555-555555555555",
            2,
            "b",
            P,
            "base",
        ),
    )
    old = {members[0].partition_uuid: old["a"], members[1].partition_uuid: old["b"]}
    new = {members[0].partition_uuid: new["a"], members[1].partition_uuid: new["b"]}
    if case == "new":
        members = members[:1]
    if case == "unchanged":
        new = deepcopy(old)
    if case == "missing":
        new = {}
    assert ops._activation_execution(h, members, old, new, backend) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [False, True])
async def test_validation_outcomes(system_profiles, diagnostic):
    h, _, _, ssh = system_profiles

    async def command(_config, cmd, **kwargs):
        if cmd.startswith("chsysstate"):
            import shlex

            assert shlex.split(cmd) == [
                "chsysstate",
                "-m",
                "9009-42A*TEST",
                "-r",
                "sysprof",
                "-n",
                "scratch",
                "--test",
            ]
            return "native result" + ("\n[stderr]\ndiagnostic" if diagnostic else "")
        if "-F name:lpar_id:curr_profile" in cmd:
            return "name:lpar_id:curr_profile\nmember-a:1:\n"
        return (
            "name:lpar_ids:profile_names\nscratch:1:base\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    ssh.side_effect = command
    result = await ops.activate_system_profile(h, S, "scratch", action="validate")
    assert result.state == ("uncertain" if diagnostic else "completed")
    assert result.job is None and result.activation_execution is None
    assert result.postcondition == ("unverified" if diagnostic else "not-applicable")
    assert sum(c.args[1].startswith("chsysstate") for c in ssh.await_args_list) == 1


@pytest.mark.asyncio
async def test_preflight_refusal_unsupported_release(system_profiles):
    h, _, _, _ = system_profiles
    h.get_console_info = AsyncMock(
        return_value={
            "Resource": {
                "VersionInfo": {
                    "Version": "10",
                    "Release": "2",
                    "ServicePackName": "1040",
                }
            }
        }
    )
    with pytest.raises(ValueError, match="10.3.1060"):
        await ops._admit(
            h,
            S,
            "new",
            action="create",
            members=[ops.SystemProfilePair(partition=L, profile=P)],
            exclusive_writer_window=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["namespace", "membership", "delete"])
async def test_delete_real_transport_diagnostics(system_profiles, monkeypatch, site):
    from hmcpctl.ssh import transport

    h, _, _, _ = system_profiles
    h.config = h.config.model_copy(update={"password": "synthetic-test-password"})
    deleted = False
    mutations = []

    async def run(cmd, **kwargs):
        nonlocal deleted
        assert kwargs["check"] is True
        if cmd.startswith("rmsyscfg"):
            mutations.append(cmd)
            deleted = True
            return SimpleNamespace(
                stdout="", stderr="diagnostic" if site == "delete" else ""
            )
        whole = "name:lpar_ids" in cmd
        raw = (
            "No results were found."
            if deleted
            else "name:lpar_ids:profile_names\nscratch:1:base\n"
            if whole
            else "name\nscratch\n"
        )
        diagnostic = site == ("membership" if whole else "namespace")
        return SimpleNamespace(stdout=raw, stderr="diagnostic" if diagnostic else "")

    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.run.side_effect = run
    monkeypatch.setattr(transport.asyncssh, "connect", lambda **_: connection)
    monkeypatch.setattr(ops, "run_hmc_command", transport.run_hmc_command)
    monkeypatch.setattr(
        "hmcpctl.ssh.profiles.run_hmc_command", transport.run_hmc_command
    )
    if site != "delete":
        with pytest.raises(ValueError):
            await ops.delete_system_profile(
                h, S, "scratch", exclusive_writer_window=True
            )
        assert mutations == []
    else:
        result = await ops.delete_system_profile(
            h, S, "scratch", exclusive_writer_window=True
        )
        assert result.state == result.command_status == "uncertain"
        assert len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["old", "new"])
async def test_all_members_old_new_union(system_profiles, foreign):
    h, member, profile, _ = system_profiles
    second_uuid = "66666666-6666-4666-8666-666666666666"
    second = deepcopy(member)
    second["UUID"] = second_uuid
    second["Resource"].update(PartitionName="member-b", PartitionID="2")
    target = member if foreign == "old" else second
    target["Resource"]["Description"] = "[hmcpctl owner:foreign created:2026-10-19]"
    h.list_logical_partitions.side_effect = lambda _: deepcopy([member, second])

    async def profiles(kind, uuid, child):
        p = deepcopy(profile)
        if uuid == second_uuid:
            p["UUID"] = "77777777-7777-4777-8777-777777777777"
        p["Resource"]["AssociatedPartition"]["href"] = (
            f"https://hmc.test/rest/api/uom/LogicalPartition/{uuid}"
        )
        return [p]

    h.list_child.side_effect = profiles
    with pytest.raises(PermissionError):
        await ops._admit(
            h,
            S,
            "scratch",
            action="update",
            members=[ops.SystemProfilePair(partition=second_uuid, profile="base")],
            exclusive_writer_window=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["complete", "stale", "missing-memory", "failed-unchanged"]
)
async def test_job_and_member_outcomes_public_activation(system_profiles, case):
    h, member, profile, ssh = system_profiles
    profile["Resource"].update(activation_fact("running", "base", 1024)["profile"])
    original = deepcopy(member["Resource"])
    member["Resource"]["LastActivatedProfile"] = {"ksv": "V1_0"}
    if case == "stale":
        member["Resource"]["LastActivatedProfile"] = "base"
        member["Resource"]["AssociatedPartitionProfile"] = activation_fact(
            "running", "base", 1024
        )["resource"]["AssociatedPartitionProfile"]
    activated = False

    async def command(_config, cmd, **kwargs):
        if "-F name:lpar_id:curr_profile" in cmd:
            return (
                "name:lpar_id:curr_profile\nmember-a:1:"
                + ("base" if activated or case == "stale" else "")
                + "\n"
            )
        return (
            "name:lpar_ids:profile_names\nscratch:1:base\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    async def wait(*args, **kwargs):
        nonlocal activated
        if case != "failed-unchanged":
            activated = True
            member["Resource"] = {
                **original,
                **activation_fact("open firmware", "base", 1024)["resource"],
            }
            if case == "missing-memory":
                del member["Resource"]["PartitionMemoryConfiguration"]["RuntimeMemory"]
        return terminal(
            "FAILED_BEFORE_COMPLETION"
            if case == "failed-unchanged"
            else "COMPLETED_OK",
            "1" if case == "failed-unchanged" else "0",
        )

    ssh.side_effect = command
    h.submit_job = AsyncMock(return_value=terminal("NOT_STARTED"))
    h.wait_for_job_entry = AsyncMock(side_effect=wait)
    result = await ops.activate_system_profile(
        h, S, "scratch", exclusive_writer_window=True
    )
    assert result.activation_execution == (
        "complete"
        if case == "complete"
        else "failed"
        if case == "failed-unchanged"
        else "unknown"
    )
    assert result.state == (
        "completed"
        if case == "complete"
        else "failed"
        if case == "failed-unchanged"
        else "uncertain"
    )
    assert h.submit_job.await_count == 1 and result.job.job_id == "job-1"


@pytest.mark.asyncio
async def test_preflight_refusal_unprintable_parent_link(system_profiles):
    h, _, profile, _ = system_profiles
    profile["Resource"]["AssociatedPartition"]["href"] = (
        f"https://hmc.test/rest/api/uom/LogicalPartition/\n{L}"
    )
    result = await ops.list_system_profiles(h, S)
    assert result.records is None and result.command_status == "uncertain"


def test_job_and_member_outcomes_unprintable_self(system_profiles):
    h, _, _, _ = system_profiles
    job = terminal()
    job["link"] = "/rest/api/uom/jobs/\njob-1"
    with pytest.raises(ValueError):
        ops._job_facts(h, job, "job-1")


def test_job_and_member_outcomes_unprintable_association(system_profiles):
    h, _, _, _ = system_profiles
    member = ops.SystemProfileMember("LogicalPartition", L, 1, "member-a", P, "base")
    before = activation_fact("not activated", "", 0)
    before["resource"].pop("AssociatedPartitionProfile")
    after = activation_fact("running", "base", 1024)
    after["resource"]["AssociatedPartitionProfile"]["href"] = (
        f"https://hmc.test/rest/api/uom/LogicalPartition/{L}/LogicalPartitionProfile/\n{P}"
    )
    assert ops._new_witness(h, member, before, after) is False


@pytest.mark.parametrize(
    "href",
    ["https:/rest/api/uom/jobs/job-1", "https://hmc.test/rest/api/uom/jobs/job-1"],
)
def test_job_and_member_outcomes_require_exact_origin(system_profiles, href):
    h, _, _, _ = system_profiles
    h.config = h.config.model_copy(update={"port": 12443})
    job = terminal()
    job["link"] = href
    with pytest.raises(ValueError):
        ops._job_facts(h, job, "job-1")


@pytest.mark.parametrize(
    "href",
    [
        "//hmc.test:12443/rest/api/uom/jobs/job-1",
        "//hmc.test:443/rest/api/uom/jobs/job-1",
    ],
)
def test_job_and_member_outcomes_no_network_relative_origin(system_profiles, href):
    h, _, _, _ = system_profiles
    h.config = h.config.model_copy(update={"port": 12443})
    job = terminal()
    job["link"] = href
    with pytest.raises(ValueError):
        ops._job_facts(h, job, "job-1")


def test_job_and_member_outcomes_identity_bound(system_profiles):
    h, _, _, _ = system_profiles
    with pytest.raises(ValueError):
        ops._job_facts(h, terminal(job_id="a" * 4097), "a" * 4097)


@pytest.mark.asyncio
async def test_inventory_malformed_resource_is_uncertain(system_profiles):
    h, member, _, _ = system_profiles
    member["Resource"] = None
    result = await ops.list_system_profiles(h, S)
    assert result.records is None and result.command_status == "uncertain"


@pytest.mark.asyncio
async def test_job_malformed_results_preserves_known_handle(
    system_profiles, monkeypatch
):
    h, _, _, _ = system_profiles
    h.submit_job = AsyncMock(return_value=terminal())
    malformed = terminal()
    malformed["Resource"]["Results"] = []
    monkeypatch.setattr(
        "hmcpctl.jobs.wait_for_submitted_job", AsyncMock(return_value=malformed)
    )
    state, job, *_ = await ops._rest(
        h, S, "CreateSystemProfile", "<JobRequest/>", 30, 1
    )
    assert state == "uncertain" and job.job_id == "job-1"


@pytest.mark.parametrize("raw", [None, [], {"link": []}, {"href": 0}])
def test_malformed_association_is_not_absence(system_profiles, raw):
    h, _, _, _ = system_profiles
    with pytest.raises((TypeError, ValueError)):
        ops._associated(h, {"AssociatedPartitionProfile": raw}, SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("rename,replace", [(True, False), (False, True), (True, True)])
async def test_public_update_variants(system_profiles, rename, replace):
    h, _, profile, ssh = system_profiles
    other = deepcopy(profile)
    other["UUID"] = "44444444-4444-4444-8444-444444444444"
    other["Resource"]["ProfileName"] = "alternate"
    h.list_child.side_effect = lambda *_: deepcopy([profile, other])
    changed = False

    async def command(_config, cmd, **kwargs):
        name = "renamed" if changed and rename else "scratch"
        pname = "alternate" if changed and replace else "base"
        return (
            f"name:lpar_ids:profile_names\n{name}:1:{pname}\n"
            if "name:lpar_ids" in cmd
            else f"name\n{name}\n"
        )

    async def wait(*args, **kwargs):
        nonlocal changed
        changed = True
        return terminal()

    ssh.side_effect = command
    h.submit_job = AsyncMock(return_value=terminal("NOT_STARTED"))
    h.wait_for_job_entry = AsyncMock(side_effect=wait)
    result = await ops.update_system_profile(
        h,
        S,
        "scratch",
        new_name="renamed" if rename else None,
        members=[ops.SystemProfilePair(partition=L, profile=other["UUID"])]
        if replace
        else None,
        exclusive_writer_window=True,
    )
    assert result.state == "completed" and result.postcondition == "verified"
    assert result.name == ("renamed" if rename else "scratch")
    assert h.submit_job.await_count == 1
    document = h.submit_job.await_args.args[1]
    assert ("alternate" in document) is replace
    assert ("renamed" in document) is rename


@pytest.mark.asyncio
@pytest.mark.parametrize("change_phase", ["before-effect", "after-effect"])
async def test_refreshes_contained_profile_public_mutation(
    system_profiles, change_phase
):
    h, _, profile, ssh = system_profiles
    events = []
    changed = False
    reads = 0

    async def profiles(*args):
        nonlocal reads
        reads += 1
        events.append(f"profile-read-{reads}")
        observed = deepcopy(profile)
        observed["Resource"]["Description"] = (
            "B"
            if change_phase == "before-effect" and reads >= 3
            else "C"
            if change_phase == "after-effect" and changed
            else "A"
        )
        return [observed]

    async def command(_config, cmd, **kwargs):
        return (
            "name:lpar_ids:profile_names\nscratch:1:base\n"
            + ("new:1:base\n" if changed else "")
            if "name:lpar_ids" in cmd
            else "name\nscratch\n" + ("new\n" if changed else "")
        )

    async def submit(*args):
        events.append("effect")
        return terminal("NOT_STARTED")

    async def wait(*args, **kwargs):
        nonlocal changed
        changed = True
        return terminal()

    h.list_child.side_effect = profiles
    ssh.side_effect = command
    h.submit_job = AsyncMock(side_effect=submit)
    h.wait_for_job_entry = AsyncMock(side_effect=wait)
    if change_phase == "before-effect":
        with pytest.raises(ValueError, match="admission changed"):
            await ops.create_system_profile(
                h,
                S,
                "new",
                [ops.SystemProfilePair(partition=L, profile=P)],
                exclusive_writer_window=True,
            )
        assert h.submit_job.await_count == 0
        assert events == [f"profile-read-{n}" for n in range(1, 5)]
    else:
        result = await ops.create_system_profile(
            h,
            S,
            "new",
            [ops.SystemProfilePair(partition=L, profile=P)],
            exclusive_writer_window=True,
        )
        assert h.submit_job.await_count == 1
        assert events == [f"profile-read-{n}" for n in range(1, 5)] + ["effect"] + [
            "profile-read-5",
            "profile-read-6",
        ]
        assert result.state == "uncertain" and result.postcondition == "drifted"
        assert result.job.job_id == "job-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_parse_feed_parent_reaches_public_lifecycle(system_profiles, nested):
    from hmcpctl.xmlutil import parse_feed

    h, _, _, ssh = system_profiles
    href = f"https://hmc.test/rest/api/uom/ManagedSystem/{S}/LogicalPartition/{L}"
    association = f'<link href="{href}" rel="related"/>' if nested else ""
    attrs = "" if nested else f' href="{href}" rel="related"'
    feed = parse_feed(
        f'<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:{P}</id><content><LogicalPartitionProfile xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"><ProfileName>base</ProfileName><AssociatedPartition{attrs}>{association}</AssociatedPartition></LogicalPartitionProfile></content></entry>'
    )
    h.list_child.side_effect = lambda *_: deepcopy(feed)
    deleted = False

    async def command(_config, cmd, **kwargs):
        nonlocal deleted
        if cmd.startswith("rmsyscfg"):
            deleted = True
            return ""
        return (
            "No results were found.\n"
            if deleted
            else "name:lpar_ids:profile_names\nscratch:1:base\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    ssh.side_effect = command
    inventory = await ops.list_system_profiles(h, S)
    assert inventory.command_status == "completed"
    result = await ops.delete_system_profile(
        h, S, "scratch", exclusive_writer_window=True
    )
    assert result.state == "completed" and result.postcondition == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["create", "update", "delete", "activate", "validate", "validate-and-activate"],
)
@pytest.mark.parametrize("drift", ["profile", "member"])
async def test_public_mutations_refuse_fresh_association_drift(
    system_profiles, operation, drift
):
    h, member, profile, ssh = system_profiles
    second_id = "66666666-6666-4666-8666-666666666666"
    second = deepcopy(member)
    second["UUID"] = second_id
    second["Resource"].update(PartitionID="2", PartitionName="member-b")
    h.list_logical_partitions.side_effect = lambda _: deepcopy([member, second])
    alternate = deepcopy(profile)
    alternate["UUID"] = "44444444-4444-4444-8444-444444444444"
    alternate["Resource"]["ProfileName"] = "alternate"

    async def profiles(kind, uuid, child):
        if uuid == L:
            return deepcopy([profile, alternate])
        p = deepcopy(profile)
        p["UUID"] = "77777777-7777-4777-8777-777777777777"
        p["Resource"]["AssociatedPartition"]["href"] = (
            f"https://hmc.test/rest/api/uom/LogicalPartition/{uuid}"
        )
        return [p]

    h.list_child.side_effect = profiles
    reads = 0
    effects = []

    async def command(_config, cmd, **kwargs):
        nonlocal reads
        if cmd.startswith(("chsysstate", "rmsyscfg")):
            effects.append(cmd)
            return ""
        if "name:lpar_ids" in cmd:
            reads += 1
            association = (
                "1:base"
                if reads == 1
                else "1:alternate"
                if drift == "profile"
                else "2:base"
            )
            return f"name:lpar_ids:profile_names\nscratch:{association}\n"
        if "-F name:lpar_id:curr_profile" in cmd:
            return "name:lpar_id:curr_profile\nmember-a:1:\n"
        return "name\nscratch\n"

    ssh.side_effect = command
    h.submit_job = AsyncMock(return_value=terminal())
    h.wait_for_job_entry = AsyncMock(return_value=terminal())
    with pytest.raises(ValueError, match="admission changed"):
        if operation == "create":
            await ops.create_system_profile(
                h,
                S,
                "new",
                [ops.SystemProfilePair(partition=L, profile=P)],
                exclusive_writer_window=True,
            )
        elif operation == "update":
            await ops.update_system_profile(
                h, S, "scratch", new_name="renamed", exclusive_writer_window=True
            )
        elif operation == "delete":
            await ops.delete_system_profile(
                h, S, "scratch", exclusive_writer_window=True
            )
        else:
            await ops.activate_system_profile(
                h, S, "scratch", action=operation, exclusive_writer_window=True
            )
    assert reads == 2 and not effects and h.submit_job.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_public_activation_retains_job_when_requested_profile_disappears(
    system_profiles, failed
):
    h, _, profile, ssh = system_profiles
    changed = False

    async def command(_config, cmd, **kwargs):
        if "-F name:lpar_id:curr_profile" in cmd:
            return "name:lpar_id:curr_profile\nmember-a:1:\n"
        return (
            f"name:lpar_ids:profile_names\nscratch:1:{'alternate' if changed else 'base'}\n"
            if "name:lpar_ids" in cmd
            else "name\nscratch\n"
        )

    async def wait(*args, **kwargs):
        nonlocal changed
        changed = True
        profile["UUID"] = "44444444-4444-4444-8444-444444444444"
        profile["Resource"]["ProfileName"] = "alternate"
        return terminal(
            "FAILED_BEFORE_COMPLETION" if failed else "COMPLETED_OK",
            "1" if failed else "0",
        )

    ssh.side_effect = command
    h.submit_job = AsyncMock(return_value=terminal("NOT_STARTED"))
    h.wait_for_job_entry = AsyncMock(side_effect=wait)
    result = await ops.activate_system_profile(
        h, S, "scratch", exclusive_writer_window=True
    )
    assert result.job.job_id == "job-1" and h.submit_job.await_count == 1
    assert result.state == ("failed" if failed else "uncertain")
    assert result.return_code == ("1" if failed else "0")
    assert (
        result.activation_execution == "unknown"
        and result.postcondition == "unverified"
    )
    assert "requested contained profile" in result.error


@pytest.mark.parametrize("surface", ["parent", "job", "associated"])
def test_native_reference_explicit_zero_port_is_not_default(system_profiles, surface):
    h, _, profile, _ = system_profiles
    with pytest.raises(ValueError):
        if surface == "parent":
            profile["Resource"]["AssociatedPartition"]["href"] = (
                f"https://hmc.test:0/rest/api/uom/LogicalPartition/{L}"
            )
            ops._profile_parent(profile, L, S, h, "LogicalPartition")
        elif surface == "job":
            job = terminal()
            job["link"] = "https://hmc.test:0/rest/api/uom/jobs/job-1"
            ops._job_identity(h, job, "job-1")
        else:
            ops._associated(
                h,
                {
                    "AssociatedPartitionProfile": {
                        "href": f"https://hmc.test:0/rest/api/uom/LogicalPartition/{L}/LogicalPartitionProfile/{P}"
                    }
                },
                SimpleNamespace(resource_type="LogicalPartition", partition_uuid=L),
            )


def test_parse_feed_associated_profile_and_conflicting_references(system_profiles):
    from hmcpctl.xmlutil import parse_feed

    h, _, profile, _ = system_profiles
    href = f"https://hmc.test/rest/api/uom/LogicalPartition/{L}/LogicalPartitionProfile/{P}"
    feed = parse_feed(
        f'<LogicalPartition><AssociatedPartitionProfile><link href="{href}" rel="related"/></AssociatedPartitionProfile></LogicalPartition>'
    )
    assert (
        ops._associated(
            h,
            feed[0]["Resource"],
            SimpleNamespace(resource_type="LogicalPartition", partition_uuid=L),
        )
        == P
    )
    profile["Resource"]["AssociatedPartition"]["link"] = {
        "href": "https://foreign.test/rest/api/uom/LogicalPartition/other"
    }
    with pytest.raises(ValueError, match="conflicting"):
        ops._profile_parent(profile, L, S, h, "LogicalPartition")
    with pytest.raises(ValueError, match="conflicting"):
        ops._associated(
            h,
            {
                "AssociatedPartitionProfile": {
                    "href": href,
                    "link": {"href": "https://foreign.test/other"},
                }
            },
            SimpleNamespace(resource_type="LogicalPartition", partition_uuid=L),
        )
