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
                "link": {
                    "@attrs": {
                        "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{L}"
                    }
                }
            },
        },
    }
    h = SimpleNamespace(
        config=HMCConfig.from_mapping(
            {"host": "hmc.test", "user": "test", "agent_id": "test"}
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
        profile["Resource"]["AssociatedPartition"]["link"]["@attrs"]["href"] = (
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
    profile["Resource"]["AssociatedPartition"]["link"]["@attrs"]["href"] = (
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
