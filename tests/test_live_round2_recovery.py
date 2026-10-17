"""Round2 producer/recovery integration at offline hardware boundaries."""

from __future__ import annotations

import copy
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict, make_dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import live_test_baseline as baseline
import live_test_recovery as recovery
import live_test_runner as runner
from live_test import inventory, provisioning

from hmcpctl.config import HMCConfig

VIOS_UUID = "00000000-0000-4000-8000-000000000007"
CONFIG = HMCConfig.from_mapping({"host": "hmc.test", "user": "fixture"})


def snapshot(state):
    tables = {name: [] for name in baseline.TABLES}
    tables["system"] = [
        {
            "name": state.config.system_name,
            "type_model": "9009-42A",
            "serial_num": "anonymous",
        }
    ]
    tables["partitions"] = [
        {
            "lpar_id": "7",
            "name": "vios-R1",
            "lpar_env": "vioserver",
            "state": "Running",
        },
        {
            "lpar_id": "8",
            "name": state.config.lp3_name,
            "lpar_env": "aixlinux",
            "state": "Not Activated",
        },
    ]
    tables["vios"] = [{"id": "7", "name": "vios-R1", "uuid": VIOS_UUID}]
    return {
        "version": 1,
        "scope": {
            "host": CONFIG.host,
            "system": state.config.system_name,
            "vios_ids": [7],
        },
        "tables": tables,
    }


@pytest.fixture
def state():
    value = runner.RunState(group="round2")
    value.artifacts.vios_uuid = VIOS_UUID
    value.artifacts.vios_partition_id = 7
    value.artifacts.vg_uuid = "volume-group"
    value.artifacts.vdisk_vg_name = value.config.vdisk_volume_group_name
    value.artifacts.lp3_baseline = {
        "description": "original",
        "proc_compat": {"profile": "default", "profile_mode": "default"},
        "state": "Not Activated",
    }
    return value


class Hardware:
    """MCP boundary preserving original objects and emulating owned temporary ones."""

    def __init__(self, state):
        self.state = state
        self.calls = []
        self.partitions = {state.config.lp3_name: ("original-uuid", "original")}
        self.disks = {state.config.vdisk_name}
        self.mappings = []
        self.fail = None
        self.foreign = False
        self.raw_mappings = []

    async def call(self, _client, tool, **kw):
        self.calls.append((tool, kw))
        if tool == "hmc_list_virtual_networks":
            return "PASS", [
                {"Resource": {"NetworkVLANID": self.state.config.provision_vlan_id}}
            ]
        if tool == "hmc_run_command":
            if kw["cmd"].startswith("viosvrcmd"):
                header = f"{self.state.config.vdisk_volume_group_name}:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\n"
                return "PASS", header + "".join(
                    f"{name} jfs2 1 1 1 closed/syncd N/A\n"
                    for name in sorted(self.disks)
                )
            if "-F name" in kw["cmd"]:
                return "PASS", "name\n" + "\n".join(self.partitions)
            return "PASS", ""
        if tool == "hmc_get_lpar":
            name = kw["lpar_name_or_uuid"]
            if name not in self.partitions:
                return "FAIL", runner.CallFailure(
                    "HMCCLIError", "HSCL8012 partition not found", "", None, False
                )
            return "PASS", {"uuid": self.partitions[name][0]}
        if tool == "hmc_get_lpar_description":
            name = kw["lpar_name_or_uuid"]
            return "PASS", self.partitions[name][1]
        if tool == "hmc_list_lpars":
            return "PASS", [
                {"UUID": identity, "Resource": {"PartitionName": name}}
                for name, (identity, _) in self.partitions.items()
            ]
        if tool == "hmc_create_virtual_disk":
            self.disks.add(kw["disk_name"])
            return "PASS", {}
        if tool == "hmc_provision_lpar":
            name = kw["name"]
            token = "foreign" if self.foreign else kw.get("caller_token", "")
            self.partitions[name] = (
                "new-uuid",
                f"[hmcpctl owner:hmcpctl created:2026-10-14] [caller {token}]",
            )
            self.mappings = [
                {
                    "id": "vhost1/vtscsi1",
                    "lpar_uuid": "new-uuid",
                    "backing_kind": "VirtualDisk",
                    "backing_name": kw["storage"]["storage_name"],
                }
            ]
            if self.fail == "interrupt":
                raise KeyboardInterrupt
            if self.fail == "lost-response":
                return "FAIL", "transport lost after mutation"
            return "PASS", {
                "resource_created": True,
                "workflow_completed": True,
                "lpar_uuid": "new-uuid",
                "steps": [],
            }
        if tool in {"hmc_decommission_lpar", "hmc_delete_lpar"}:
            name = kw["lpar_name_or_uuid"]
            if self.fail == "cleanup":
                return "FAIL", "cleanup refused"
            self.partitions.pop(name, None)
            self.mappings = []
            return "PASS", {"resource_deleted": True, "workflow_completed": True}
        if tool == "hmc_list_storage_mappings":
            from hmcpctl.operations.storage.resources import _storage_mapping

            return "PASS", [
                *self.mappings,
                *(asdict(_storage_mapping(row)) for row in self.raw_mappings),
            ]
        if tool == "hmc_get_vios_storage_detail":
            return "PASS", {
                "Resource": {
                    "VirtualSCSIMappings": {"VirtualSCSIMapping": self.raw_mappings}
                }
            }
        if tool == "hmc_delete_virtual_disk":
            self.disks.discard(kw["disk_name"])
            return "PASS", {}
        return "PASS", {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "lost-response", "interrupt"])
async def test_st14_preserves_originals_and_cleans_owned_resources(
    state, monkeypatch, failure
):
    hardware = Hardware(state)
    hardware.fail = failure
    monkeypatch.setattr(runner.RunState, "call", hardware.call)
    try:
        await provisioning.exercise_storage_provisioning(None, state)
    except KeyboardInterrupt:
        assert failure == "interrupt"
    assert hardware.partitions == {state.config.lp3_name: ("original-uuid", "original")}
    assert hardware.disks == {state.config.vdisk_name}
    provisions = [kw for tool, kw in hardware.calls if tool == "hmc_provision_lpar"]
    assert len(provisions) == 1
    assert provisions[0]["name"] != state.config.lp3_name
    assert provisions[0]["storage"]["storage_name"] != state.config.vdisk_name
    assert provisions[0]["caller_token"]
    assert not any(kw.get("ownership_override") for _, kw in hardware.calls)


@pytest.mark.asyncio
async def test_st14_refuses_foreign_cleanup(state, monkeypatch):
    hardware = Hardware(state)
    hardware.foreign = True
    monkeypatch.setattr(runner.RunState, "call", hardware.call)
    await provisioning.exercise_storage_provisioning(None, state)
    assert not any(tool == "hmc_decommission_lpar" for tool, _ in hardware.calls)
    assert any(row["status"] == "FAIL" for row in state.results)
    assert hardware.partitions[state.config.lp3_name] == ("original-uuid", "original")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "state",
        "vios-identity",
        "description",
        "profile",
        "missing",
        "scope",
        "partial",
        "unreadable",
    ],
)
def test_actual_round2_recovery_has_independent_witness(
    state, monkeypatch, tmp_path, fault
):
    before = snapshot(state)
    state.artifacts.lp3_baseline["round2_snapshot"] = before
    document = {
        "run": {
            "group": "round2",
            "subtasks": runner.SUBTASK_GROUPS["round2"],
            "partial": False,
        },
        "config": asdict(state.config),
        "hmc": runner._hmc_identity(CONFIG),
        "artifacts": asdict(state.artifacts),
        "results": [],
    }
    if fault == "missing":
        document["artifacts"]["lp3_baseline"].pop("round2_snapshot")
    if fault == "scope":
        document["hmc"]["host"] = "other.example.test"
    if fault == "partial":
        document["run"]["partial"] = True
    reads = []

    async def capture(config, system, ids):
        reads.append((system, ids))
        result = copy.deepcopy(before)
        if fault == "state":
            result["tables"]["partitions"][1]["state"] = "Running"
        if fault == "vios-identity":
            result["tables"]["vios"][0]["uuid"] = "00000000-0000-4000-8000-000000000009"
        if fault == "unreadable":
            result["tables"].pop("partitions")
        return result

    async def call(_state, _client, tool, **kwargs):
        recovery.guard_read_only(tool, kwargs)
        if tool == "hmc_get_lpar_description":
            return "PASS", "changed" if fault == "description" else "original"
        if tool == "hmc_get_lpar_proc_compat":
            return "PASS", {
                "profile": "default",
                "profile_mode": "POWER9" if fault == "profile" else "default",
            }
        raise AssertionError(f"unexpected hardware read {tool}")

    @asynccontextmanager
    async def served():
        yield None

    monkeypatch.setattr(baseline, "capture", capture)
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(runner, "served_client", served)
    monkeypatch.setattr(runner.RunState, "call", call)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG, raising=False)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document))
    expected = (
        0
        if fault is None
        else (2 if fault in {"missing", "scope", "partial", "unreadable"} else 1)
    )
    assert recovery.main(["--results", str(path)]) == expected
    assert reads == (
        [] if fault in {"missing", "scope"} else [(state.config.system_name, [7])]
    )


@pytest.mark.asyncio
async def test_st8_refuses_existing_scratch(state, monkeypatch):
    hardware = Hardware(state)
    hardware.partitions[state.config.scratch_name] = ("foreign-uuid", "foreign")
    monkeypatch.setattr(runner.RunState, "call", hardware.call)
    await runner.exercise_lpar_lifecycle(None, state)
    assert not any(tool == "hmc_create_lpar" for tool, _ in hardware.calls)
    assert hardware.partitions[state.config.scratch_name] == ("foreign-uuid", "foreign")


@pytest.mark.asyncio
@pytest.mark.parametrize("served", [False, True])
async def test_st0_captures_fresh_round2_witness(state, monkeypatch, served):
    expected = snapshot(state)
    seen = []

    async def capture(config, system, ids):
        seen.append((system, ids))
        return copy.deepcopy(expected)

    async def call(_state, _client, tool, **kwargs):
        if tool == "hmc_get_lpar_description":
            return "PASS", "original"
        if tool == "hmc_get_lpar_proc_compat":
            return "PASS", (
                make_dataclass("Compatibility", ["profile", "profile_mode"])(
                    "default", "default"
                )
                if served
                else {"profile": "default", "profile_mode": "default"}
            )
        if tool == "hmc_list_vios":
            return "PASS", [{"UUID": VIOS_UUID, "Resource": {"PartitionID": "7"}}]
        if tool == "hmc_run_command":
            return "PASS", "1,Not Activated"
        return "PASS", {}

    monkeypatch.setattr(baseline, "capture", capture)
    monkeypatch.setattr(runner.RunState, "call", call)
    await inventory.capture_lpar_baseline(None, state)
    assert state.artifacts.lp3_baseline.get("round2_snapshot") == expected
    assert seen == [(state.config.system_name, [7])]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["partition", "disk", "inventory", "cleanup"])
async def test_fixture_admission_and_cleanup_fail_closed(state, monkeypatch, fault):
    hardware = Hardware(state)
    monkeypatch.setattr(provisioning.secrets, "token_hex", lambda _: "deadbeef")
    if fault == "partition":
        hardware.partitions["hmcpctl-live-round2-deadbeef"] = ("foreign", "foreign")
    if fault == "disk":
        hardware.disks.add("hp14deadbeef")
    if fault == "cleanup":
        hardware.fail = fault
    original = hardware.call

    async def call(_state, client, tool, **kwargs):
        if fault == "inventory" and tool == "hmc_run_command":
            return "PASS", "unparseable listing"
        return await original(client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", call)
    await provisioning.exercise_storage_provisioning(None, state)
    assert any(row["status"] == "FAIL" for row in state.results)
    assert hardware.partitions[state.config.lp3_name] == ("original-uuid", "original")
    assert state.config.vdisk_name in hardware.disks
    if fault != "cleanup":
        assert not any(
            tool
            in {
                "hmc_provision_lpar",
                "hmc_create_virtual_disk",
                "hmc_delete_virtual_disk",
                "hmc_decommission_lpar",
            }
            for tool, _ in hardware.calls
        )
    else:
        assert "hp14deadbeef" in hardware.disks
        assert not any(tool == "hmc_delete_virtual_disk" for tool, _ in hardware.calls)


@pytest.mark.asyncio
async def test_round2_property_reader_enforces_readonly(state, monkeypatch):
    inputs = recovery.Round2Inputs(
        snapshot(state),
        runner._hmc_identity(CONFIG),
        state.config.lp3_name,
        "original",
        {"profile": "default", "profile_mode": "default"},
    )
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)

    async def capture(*args):
        return snapshot(state)

    monkeypatch.setattr(baseline, "capture", capture)
    calls = []

    async def remote(_state, _client, tool, **kw):
        calls.append(tool)
        return "PASS", "original" if "description" in tool else inputs.compatibility

    monkeypatch.setattr(runner.RunState, "call", remote)
    guarded = recovery._read_only_caller(None, state)
    assert await recovery.check_round2(guarded, inputs) == []
    assert calls == ["hmc_get_lpar_description", "hmc_get_lpar_proc_compat"]
    with pytest.raises(recovery.MutatingCallRefused):
        await guarded("hmc_set_lpar_description", description="changed")
    assert len(calls) == 2


def test_producer_to_recovery_composes_real_baseline_parsers(
    state, monkeypatch, tmp_path
):
    """Real producer, serializer, recovery main and baseline over only transport doubles."""
    import asyncio
    import shlex

    from hmcpctl.ssh import io_inventory, profiles, sriov
    from hmcpctl.ssh.commands import HMC_NO_RESULTS
    from hmcpctl.xmlutil import ATOM_NS

    uom = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"

    hardware = Hardware(state)
    group_uuid = "00000000-0000-4000-8000-000000000008"
    state.artifacts.vg_uuid = group_uuid
    transport_reads = []

    async def ssh(config, command):
        transport_reads.append(command)
        recovery.guard_read_only("hmc_run_command", {"cmd": command})
        if command.startswith("viosvrcmd "):
            return (await hardware.call(None, "hmc_run_command", cmd=command))[1]
        fields = shlex.split(command)[shlex.split(command).index("-F") + 1]
        if "-r sys " in command:
            return fields + f"\n{state.config.system_name},9009-42A,anonymous\n"
        if "-r lpar " in command:
            return (
                fields
                + f"\n7,vios-R1,vioserver,Running\n8,{state.config.lp3_name},aixlinux,Not Activated\n"
            )
        return fields + "\n"

    class ReadOnlyHMC:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_system_summaries(self):
            return [
                {
                    "UUID": "00000000-0000-4000-8000-000000000010",
                    "SystemName": state.config.system_name,
                }
            ]

        async def find_vios_by_name(self, name, **kwargs):
            assert name == "vios-R1"
            return {"UUID": VIOS_UUID}

        async def _get(self, path, kind, **kwargs):
            transport_reads.append(path)
            if kind == "VirtualIOServer":
                assert not hardware.mappings
                resource = "<VirtualIOServer><VirtualSCSIMappings><Metadata><Atom/></Metadata></VirtualSCSIMappings></VirtualIOServer>"
                identity = VIOS_UUID
            else:
                assert kind == "VolumeGroup"
                resource = f"<VolumeGroup><GroupName>{state.config.vdisk_volume_group_name}</GroupName></VolumeGroup>"
                identity = group_uuid
            resource = resource.replace(f"<{kind}>", f'<{kind} xmlns="{uom}">')
            return f'<feed xmlns="{ATOM_NS}"><entry><id>{identity}</id><content>{resource}</content></entry></feed>'

        def __getattr__(self, name):
            raise AssertionError(f"unapproved HMC transport {name}")

    for module in (baseline, io_inventory, profiles, sriov):
        monkeypatch.setattr(module, "run_hmc_command", ssh)
    monkeypatch.setattr(baseline, "HMCClient", lambda _: ReadOnlyHMC())
    before = asyncio.run(baseline.capture(CONFIG, state.config.system_name, [7]))
    state.artifacts.lp3_baseline["round2_snapshot"] = before
    original_call = hardware.call

    async def call(_state, client, tool, **kwargs):
        if tool == "hmc_get_lpar_proc_compat":
            return "PASS", {"profile": "default", "profile_mode": "default"}
        if tool == "hmc_run_command" and kwargs["cmd"].startswith("lshwres "):
            return "PASS", HMC_NO_RESULTS
        return await original_call(client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", call)
    asyncio.run(provisioning.exercise_storage_provisioning(None, state))
    document = {
        "run": {
            "group": "round2",
            "subtasks": runner.SUBTASK_GROUPS["round2"],
            "partial": False,
        },
        "config": asdict(state.config),
        "hmc": runner._hmc_identity(CONFIG),
        "artifacts": asdict(state.artifacts),
        "results": state.results,
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document))

    @asynccontextmanager
    async def served():
        yield None

    monkeypatch.setattr(runner, "served_client", served)
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: CONFIG)
    assert recovery.main(["--results", str(path)]) == 0
    assert len(transport_reads) >= 16


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["scratch", "provision"])
async def test_malformed_partition_admission_never_mutates(
    state, monkeypatch, scenario
):
    hardware = Hardware(state)
    original = hardware.call

    async def call(_state, client, tool, **kwargs):
        if tool == "hmc_run_command" and kwargs["cmd"].startswith("lssyscfg "):
            return "PASS", "not a partition inventory"
        return await original(client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", call)
    if scenario == "scratch":
        await runner.exercise_lpar_lifecycle(None, state)
    else:
        await provisioning.exercise_storage_provisioning(None, state)
    assert not any(
        tool in {"hmc_create_lpar", "hmc_create_virtual_disk", "hmc_provision_lpar"}
        for tool, _ in hardware.calls
    )


def test_restored_context_drops_prior_round2_witnesses(state, tmp_path):
    state.artifacts.lp3_baseline.update(
        round2_snapshot=snapshot(state), round2_fixture={"name": "old"}
    )
    path = tmp_path / "prior.json"
    path.write_text(
        json.dumps(
            {
                "config": asdict(state.config),
                "hmc": runner._hmc_identity(CONFIG),
                "artifacts": asdict(state.artifacts),
                "results": [],
            }
        )
    )
    restored = runner.RunState(config=state.config)
    assert runner._restore_artifacts_from_results(restored, CONFIG, str(path))
    assert "round2_snapshot" not in restored.artifacts.lp3_baseline
    assert "round2_fixture" not in restored.artifacts.lp3_baseline
    assert restored.artifacts.lp3_baseline["description"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["bare", "optical", "malformed", "fixture"])
async def test_st14_cleanup_classifies_complete_unbacked_mapping_inventory(
    state, monkeypatch, shape
):
    hardware = Hardware(state)
    mapping = {
        "ServerAdapter": {
            "AdapterName": "vhost9",
            "VirtualSlotNumber": "19",
            "RemoteLogicalPartitionID": "9",
            "RemoteSlotNumber": "3",
        }
    }
    if shape == "optical":
        mapping["TargetDevice"] = {
            "VirtualOpticalTargetDevice": {"TargetName": "vtopt9"}
        }
    elif shape == "malformed":
        mapping["Storage"] = {}
    elif shape == "fixture":
        mapping["TargetDevice"] = {"VirtualDiskTargetDevice": {"TargetName": "vtscsi9"}}
        mapping["Storage"] = {"VirtualDisk": {"DiskName": "hp14deadbeef"}}
    hardware.raw_mappings = [mapping]
    monkeypatch.setattr(provisioning.secrets, "token_hex", lambda _: "deadbeef")
    monkeypatch.setattr(runner.RunState, "call", hardware.call)
    await provisioning.exercise_storage_provisioning(None, state)
    assert hardware.partitions == {state.config.lp3_name: ("original-uuid", "original")}
    if shape in {"bare", "optical"}:
        assert hardware.disks == {state.config.vdisk_name}
        assert not any(row["status"] == "FAIL" for row in state.results)
    else:
        assert "hp14deadbeef" in hardware.disks
        assert not any(tool == "hmc_delete_virtual_disk" for tool, _ in hardware.calls)
        assert any(row["status"] == "FAIL" for row in state.results)
