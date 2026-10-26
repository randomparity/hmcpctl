"""Actual creation owners compose one native request and gate dependent effects."""

import csv
import io
import shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from unit.test_ssh_output_limits import connection

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import (
    LparReportingSettings,
    LparResources,
    LparSettings,
    build_vios_document,
)
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter as Adapter
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import core, workflows
from hmcpctl.operations.lpar.assignments import (
    DedicatedPcieAssignment,
    LparPcieAssignments,
)
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.ssh import lpar as native
from hmcpctl.ssh import profiles
from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command

SYSTEM = "11111111-1111-4111-8111-111111111111"
LOCAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PEER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ADAPTER = Adapter(2, "client", False, remote_lpar_id=7, remote_slot=3)
PRIVATE = "private-transport-marker"


def entry(uuid, name, native_id, kind):
    return {
        "UUID": uuid,
        "ResourceType": kind,
        "Resource": {
            "PartitionName": name,
            "PartitionID": str(native_id),
            "PartitionType": "Virtual IO Server"
            if kind == "VirtualIOServer"
            else "AIX/Linux",
            "Description": "",
            "DefaultProfileName": "default_profile" if uuid == LOCAL else "peer_saved",
            "PartitionIOConfiguration": {"MaximumVirtualIOSlots": "8"},
        },
    }


def profile(name, native_id, serial, profile_name="default_profile", **extra):
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(
        f"{k}={v}"
        for k, v in {
            "name": profile_name,
            "lpar_name": name,
            "lpar_id": native_id,
            "max_virtual_slots": 8,
            "virtual_serial_adapters": serial,
            "virtual_eth_adapters": "none",
            "virtual_fc_adapters": "none",
            "virtual_scsi_adapters": "none",
            "vnic_adapters": "none",
            **extra,
        }.items()
    )
    return output.getvalue()


@pytest.fixture(params=("AIX/Linux", "OS400", "Virtual IO Server"))
def target(request, monkeypatch):
    environment = request.param
    kind = (
        "VirtualIOServer" if environment == "Virtual IO Server" else "LogicalPartition"
    )
    local = entry(LOCAL, "new", 9, kind)
    local["Resource"]["PartitionType"] = environment
    peer = entry(PEER, "peer", 7, "VirtualIOServer")
    feeds = {"LogicalPartition": [], "VirtualIOServer": [peer]}
    b = SimpleNamespace(
        environment=environment,
        kind=kind,
        local=local,
        peer=peer,
        feeds=feeds,
        created=False,
        events=[],
        controller=False,
        fault=None,
        local_record=profile("new", 9, "2/client//7/peer/3/0"),
        peer_record=profile("peer", 7, "3/server/0/9/new/2/0", "peer_saved"),
    )
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "test", "agent_id": "agent"}
    )
    system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "system",
            "IsClassicHMCManagement": "true",
            "IsHMCPowerVMManagementController": "false",
            "IsPowerVMManagementController": "false",
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
        },
    }
    b.system = system
    hmc.get_managed_system.return_value = system
    hmc.create_logical_partition.return_value = deepcopy(local)
    hmc.create_vios.return_value = deepcopy(local)
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "system", "State": "operating"}
    ]
    hmc.list_logical_partitions.side_effect = lambda system: deepcopy(
        feeds["LogicalPartition"]
    )
    hmc.list_vios.side_effect = lambda system: deepcopy(feeds["VirtualIOServer"])

    async def get(kind, uuid):
        b.events.append("identity:" + uuid)
        if b.fault == "identity" and uuid == LOCAL:
            raise HMCError(PRIVATE)
        return deepcopy(local if uuid == LOCAL else peer)

    hmc.get_uom.side_effect = get
    hmc.get_logical_partition.return_value = deepcopy(local)

    async def find(name, **kwargs):
        if b.created and b.fault == "lookup":
            raise RuntimeError(PRIVATE)
        return deepcopy(local) if b.created else None

    hmc.find_partition_by_name.side_effect = find
    hmc.find_vios_by_name.side_effect = find

    async def command(config, cmd):
        if cmd == "lshmc -V":
            return "Version: 10\nRelease: 3\nService Pack: 1060"
        if "-F type_model" in cmd:
            return "8375-42A"
        if cmd.startswith("lshwres -r mem"):
            return "possible_hpt_ratios,possible_ppt_ratios\n1:128,1:256"
        if cmd.startswith("lssyscfg -r sys"):
            return f"{SYSTEM},system"
        if cmd.startswith("lscomgmt"):
            b.events.append("controller")
            return "1" if b.controller else "null"
        if cmd.startswith("mksyscfg"):
            b.events.append("create")
            b.created = True
            feeds[kind].append(local)
            if b.fault == "create":
                raise HMCCLIError(PRIVATE)
            if b.fault == "refusal":
                raise HMCCLIError(PRIVATE, remote_refusal_code="HSCL07C1")
            if b.fault == "timeout":
                raise TimeoutError(PRIVATE)
            return ""
        if "description=" in cmd:
            b.events.append("stamp")
            if b.fault == "stamp":
                raise HMCCLIError(PRIVATE)
            return ""
        if "-o apply" in cmd:
            b.events.append("apply")
        return ""

    async def read(config, system, name, profile_name, *, max_output_bytes):
        assert max_output_bytes == 1048576
        b.events.append("profile:" + name)
        if b.fault == "profile":
            raise HMCError(PRIVATE)
        return b.local_record if name == "new" else b.peer_record

    b.command = AsyncMock(side_effect=command)
    b.reader = AsyncMock(side_effect=read)
    for module in (
        "hmcpctl.ssh.lpar",
        "hmcpctl.ssh.profiles",
        "hmcpctl.operations.lpar.linux_roles",
        "hmcpctl.ssh.sriov",
        "hmcpctl.ssh.lpar_memory_layout",
    ):
        monkeypatch.setattr(module + ".run_hmc_command", b.command)
    monkeypatch.setattr("hmcpctl.ssh.lpar_serial.read_lpar_profile_record", b.reader)
    b.hmc = hmc
    return b


async def invoke(b, *, direct=False, adapters=(ADAPTER,), **options):
    resources = options.pop("resources", LparResources(desired_memory=1024))
    assignments = options.pop("assignments", LparPcieAssignments())
    if b.kind == "VirtualIOServer":
        return await create_vios(
            b.hmc,
            SYSTEM,
            "new",
            resources,
            max_virtual_slots=8,
            virtual_serial_adapters=adapters,
            **options,
        )
    creation = core.LparCreation(
        "new",
        b.environment,
        resources,
        max_virtual_slots=8,
        virtual_serial_adapters=adapters,
        **options,
    )
    if direct:
        return await core.create_and_stamp_lpar(b.hmc, SYSTEM, creation)
    return await workflows.create_lpar(b.hmc, SYSTEM, creation, assignments)


def serial_step(b, result):
    if b.kind == "VirtualIOServer":
        return result["serial_verification"]
    return next(s.result for s in result.steps if s.step == "virtual_serial_adapters")


@pytest.mark.asyncio
async def test_native_creation_identity_pair_and_separate_ownership(target):
    b = target
    result = await invoke(
        b, **({"apply_profile": True} if b.kind == "LogicalPartition" else {})
    )
    assert b.events.count("create") == 1
    assert b.events.index("controller") < b.events.index("create")
    assert b.events.index("identity:" + PEER) < b.events.index("create")
    commands = [
        shlex.split(c.args[1])
        for c in b.command.await_args_list
        if c.args[1].startswith("mksyscfg")
    ]
    assert len(commands) == 1
    record = commands[0][-1]
    assert '"virtual_serial_adapters=2/client//7//3/0"' in record
    assert "max_virtual_slots=8" in record and "desired_mem=1024" in record
    expected_environment = {
        "AIX/Linux": "aixlinux",
        "OS400": "os400",
        "Virtual IO Server": "vioserver",
    }[b.environment]
    assert f"lpar_env={expected_environment}" in record
    assert serial_step(b, result)["configuration"] == "verified"
    assert serial_step(b, result)["pairs"] == [
        {"slot": 2, "status": "verified-saved-pair"}
    ]
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    if b.kind == "LogicalPartition":
        assert result.workflow_completed and result.ownership_stamped is True
        assert (
            b.events.index("create")
            < b.events.index("identity:" + LOCAL)
            < b.events.index("stamp")
        )
        assert b.events.index("profile:peer") < b.events.index("apply")
        steps = [s.step for s in result.steps]
        assert steps.index("virtual_serial_adapters") < steps.index("apply_profile")
    else:
        assert "stamp" not in b.events and "apply" not in b.events
        assert result["UUID"] == LOCAL and result["ResourceType"] == "VirtualIOServer"
        b.hmc.get_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["create", "lookup", "identity", "profile", "pair", "mixed", "stamp"]
)
async def test_incomplete_creation_never_replays_or_applies(target, fault):
    b = target
    if fault == "stamp" and b.kind == "VirtualIOServer":
        pytest.skip("VIOS has no stamp owner")
    b.fault = fault
    if fault == "pair":
        b.peer_record = profile("peer", 7, "none", "peer_saved")
    if fault == "mixed":
        b.local_record = profile(
            "new", 9, "2/client//7/peer/3/0", virtual_scsi_adapters="opaque"
        )
    options = {"apply_profile": True} if b.kind == "LogicalPartition" else {}
    if fault == "create" or b.kind == "VirtualIOServer":
        with pytest.raises(HMCError) as raised:
            await invoke(b, **options)
        assert PRIVATE not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
    else:
        result = await invoke(b, **options)
        assert not result.workflow_completed
        assert PRIVATE not in str(result)
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    if fault in {"create", "lookup", "identity"}:
        assert "stamp" not in b.events
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()
    b.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family",
    [
        "virtual_eth_adapters",
        "virtual_fc_adapters",
        "virtual_scsi_adapters",
        "vnic_adapters",
    ],
)
async def test_missing_local_family_blocks_apply_and_assignments(target, family):
    target.local_record = profile("new", 9, "2/server/0////0").replace(
        f",{family}=none", ""
    )
    options = {}
    if target.kind == "LogicalPartition":
        options = {
            "apply_profile": True,
            "assignments": LparPcieAssignments(
                dedicated=(DedicatedPcieAssignment("default_profile", "21010001"),)
            ),
        }
        result = await invoke(
            target, adapters=(Adapter(2, "server", False, 0),), **options
        )
        assert not result.workflow_completed
        assert serial_step(target, result)["configuration"] == "unverified"
        assert [(step.step, step.status) for step in result.steps][-2:] == [
            ("apply_profile", "skipped"),
            ("dedicated[0]", "skipped"),
        ]
    else:
        with pytest.raises(HMCError, match="verification"):
            await invoke(target, adapters=(Adapter(2, "server", False, 0),))
    assert target.events.count("create") == 1
    assert "apply" not in target.events
    assert not [
        call
        for call in target.command.await_args_list
        if call.args[1].startswith("chsyscfg") and "description=" not in call.args[1]
    ]
    target.hmc.delete_logical_partition.assert_not_awaited()
    target.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_peer_ownership_denial_precedes_native_effect(target):
    b = target
    b.peer["Resource"]["Description"] = "[hmcpctl owner:other created:2026-10-26]"
    with pytest.raises(PermissionError):
        await invoke(b)
    assert "create" not in b.events and "stamp" not in b.events


@pytest.mark.asyncio
async def test_controller_current_unknown_blocks_followups_without_profile_reads(
    target,
):
    b = target
    b.controller = True
    if b.kind == "VirtualIOServer":
        with pytest.raises(HMCError, match="exists or may exist"):
            await invoke(b)
    else:
        result = await invoke(b, apply_profile=False)
        assert not result.workflow_completed
        assert serial_step(b, result)["plane"] == "current-unavailable"
    b.reader.assert_not_awaited()
    assert "apply" not in b.events
    record = next(
        c.args[1] for c in b.command.await_args_list if c.args[1].startswith("mksyscfg")
    )
    assert "profile_name=" not in record and "sync_curr_profile=" not in record


@pytest.mark.asyncio
async def test_controller_apply_refused_before_create(target):
    if target.kind == "VirtualIOServer":
        pytest.skip("VIOS has no profile apply input")
    target.controller = True
    with pytest.raises(ValueError, match="Controller"):
        await invoke(target, apply_profile=True)
    assert "create" not in target.events


@pytest.mark.asyncio
async def test_unbound_server_never_claims_pair(target):
    target.local_record = profile("new", 9, "2/server/0////0")
    result = await invoke(target, adapters=(Adapter(2, "server", False, 0),))
    assert serial_step(target, result)["pairs"] == [{"slot": 2, "status": "unbound"}]
    assert "profile:peer" not in target.events


@pytest.mark.asyncio
async def test_update_only_weight_stays_before_serial_effect(target):
    with pytest.raises(ValueError, match="weight"):
        await invoke(
            target, resources=LparResources(desired_memory=1024, uncapped_weight=12)
        )
    assert "create" not in target.events


@pytest.mark.asyncio
async def test_native_writer_composes_c12_and_serial_once(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr(native, "run_hmc_command", command)
    await native.create_lpar_via_cli(
        HMCConfig.from_mapping({}),
        "system",
        "new",
        "OS400",
        LparResources(desired_memory=1024),
        max_virtual_slots=8,
        physical_io=LparPhysicalIO(lpar_io_pool_ids=("0", "2"), console_slot="hmc"),
        virtual_serial_adapters=(
            Adapter(2, "server", False, 0),
            Adapter(3, "server", True),
        ),
    )
    command.assert_awaited_once()
    record = shlex.split(command.await_args.args[1])[-1]
    assert '"lpar_io_pool_ids=0,2"' in record
    assert '"virtual_serial_adapters=2/server/0////0,3/server/////1"' in record
    assert "console_slot=hmc" in record


@pytest.mark.asyncio
async def test_native_serial_validation_precedes_transport(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(native, "run_hmc_command", command)
    with pytest.raises(ValueError, match="max_virtual_slots"):
        await native.create_lpar_via_cli(
            HMCConfig.from_mapping({}),
            "system",
            "new",
            virtual_serial_adapters=(ADAPTER,),
        )
    command.assert_not_awaited()


def test_vios_document_optional_maximum_preserves_omission():
    assert "MaximumVirtualIOSlots" not in build_vios_document("new")
    assert ">8</MaximumVirtualIOSlots>" in build_vios_document(
        "new", max_virtual_slots=8
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["refusal", "timeout"])
async def test_source_capability_refusal_or_lost_response_is_one_unknown_attempt(
    target, fault
):
    target.fault = fault
    with pytest.raises(HMCError, match="unknown") as raised:
        await invoke(target)
    assert PRIVATE not in str(raised.value) and raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert target.events.count("create") == 1
    assert "stamp" not in target.events
    target.hmc.create_logical_partition.assert_not_awaited()
    target.hmc.create_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_serial_barrier_skips_real_assignment_collection(target):
    if target.kind == "VirtualIOServer":
        pytest.skip("VIOS has no assignment workflow")
    target.peer_record = profile("peer", 7, "none", "peer_saved")
    assignments = LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", "21010001"),)
    )
    result = await invoke(target, assignments=assignments, apply_profile=True)
    assert not result.workflow_completed
    assert [(step.step, step.status) for step in result.steps][-2:] == [
        ("apply_profile", "skipped"),
        ("dedicated[0]", "skipped"),
    ]
    assert "apply" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize("prerequisite", ["settings", "reporting", "layout"])
async def test_verified_serial_does_not_bypass_other_prerequisite(target, prerequisite):
    if target.kind == "VirtualIOServer" and prerequisite != "layout":
        pytest.skip("VIOS has no general/reporting creation inputs")
    options = {
        "settings": {"settings": LparSettings(allow_perf_collection=True)},
        "reporting": {"reporting_settings": LparReportingSettings(work_group_id=1)},
        "layout": {"memory_layout": LparMemoryLayout(hpt_ratio="1:128")},
    }[prerequisite]
    if target.kind == "VirtualIOServer":
        with pytest.raises(HMCError, match="verification"):
            await invoke(target, **options)
    else:
        result = await invoke(target, apply_profile=True, **options)
        assert not result.workflow_completed
        assert serial_step(target, result)["configuration"] == "verified"
        assert "apply" not in target.events
        assert (
            next(s for s in result.steps if s.step == "apply_profile").status
            == "skipped"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("prerequisite", ["vtpm", "io", "layout"])
async def test_other_admission_guards_remain_before_serial_submission(
    target, prerequisite
):
    options = {
        "vtpm": {"vtpm_settings": LparVtpmSettings(vtpm_enabled=True)},
        "io": {"physical_io": LparPhysicalIO(console_slot="hmc")},
        "layout": {"memory_layout": LparMemoryLayout(hpt_ratio="unadvertised")},
    }[prerequisite]
    if prerequisite == "io" and target.kind == "VirtualIOServer":
        pytest.skip("VIOS has no C12 creation input")
    with pytest.raises(ValueError):
        await invoke(target, **options)
    assert "create" not in target.events


@pytest.mark.asyncio
async def test_lpar_required_stamp_policy_still_fails_without_uncertain_stamp(target):
    if target.kind == "VirtualIOServer":
        pytest.skip("VIOS has no stamp policy")
    target.fault = "identity"
    with pytest.raises(HMCError, match="ownership was not stamped") as raised:
        await invoke(target, stamp_policy="required")
    assert PRIVATE not in str(raised.value) and raised.value.__cause__ is None
    assert "stamp" not in target.events and "apply" not in target.events


@pytest.mark.asyncio
async def test_omission_preserves_rest_and_result_contract(target):
    result = await invoke(target, direct=True, adapters=())
    assert "controller" not in target.events and "create" not in target.events
    target.reader.assert_not_awaited()
    if target.kind == "VirtualIOServer":
        assert "serial_verification" not in result
        target.hmc.create_vios.assert_awaited_once()
        assert "stamp" not in target.events
    else:
        assert result.serial_step is None and result.ownership_stamped is True
        target.hmc.create_logical_partition.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_server_preserves_all_components_and_independent_client(target):
    target.local_record = profile("new", 9, "2/server/0/7/peer/3/1")
    target.peer_record = profile("peer", 7, "3/client//9/new/2/0", "peer_saved")
    result = await invoke(
        target,
        adapters=(Adapter(2, "server", True, 0, 7, "peer", 3),),
    )
    assert serial_step(target, result)["pairs"] == [
        {"slot": 2, "status": "verified-saved-pair"},
    ]
    record = next(
        shlex.split(call.args[1])[-1]
        for call in target.command.await_args_list
        if call.args[1].startswith("mksyscfg")
    )
    assert '"virtual_serial_adapters=2/server/0/7/peer/3/1"' in record


@pytest.mark.asyncio
@pytest.mark.parametrize("adapters", [[ADAPTER], (ADAPTER, ADAPTER), ("bad",)])
async def test_invalid_direct_collection_refused_before_external_reads(
    target, adapters
):
    with pytest.raises((TypeError, ValueError)):
        await invoke(target, adapters=adapters)
    target.command.assert_not_awaited()
    target.hmc.get_uom.assert_not_awaited()
    target.hmc.find_partition_by_name.assert_not_awaited()
    target.hmc.find_vios_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_serial_keylock_combination_refused_before_reads(target):
    if target.kind == "VirtualIOServer":
        pytest.skip("VIOS has no keylock input")
    with pytest.raises(ValueError, match="keylock"):
        await invoke(target, keylock="normal")
    target.command.assert_not_awaited()
    target.hmc.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_profile_collection_bound_stops_dependent_effects(
    target, monkeypatch, stream
):
    conn, process = connection(monkeypatch, **{stream: b"x" * (1048576 + 8192)})
    target.hmc.config = target.hmc.config.model_copy(update={"password": "fixture"})

    async def command(config, text, **kwargs):
        if text.startswith("lssyscfg -r prof "):
            return await run_hmc_command(config, text, **kwargs)
        return await target.command(config, text)

    monkeypatch.setattr(profiles, "run_hmc_command", command)
    monkeypatch.setattr(
        "hmcpctl.ssh.lpar_serial.read_lpar_profile_record",
        profiles.read_lpar_profile_record,
    )
    if target.kind == "VirtualIOServer":
        with pytest.raises(HMCError):
            await invoke(target)
    else:
        result = await invoke(target, apply_profile=True)
        assert not result.workflow_completed
    conn.run.assert_not_awaited()
    conn.abort.assert_called_once()
    process.close.assert_called_once()
    assert getattr(process, stream).data
    assert target.events.count("create") == 1
    assert "apply" not in target.events
    target.hmc.delete_logical_partition.assert_not_awaited()
    target.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"memory_layout": LparMemoryLayout(hpt_ratio="1:128")},
        {"hardware_mem_encryption": False},
    ],
)
async def test_serial_combined_submission_discards_provider_chain(target, options):
    if target.kind == "VirtualIOServer":
        pytest.skip("VIOS has a separate submission owner")
    target.fault = "create"
    creation = core.LparCreation(
        "new",
        target.environment,
        LparResources(desired_memory=1024),
        max_virtual_slots=8,
        virtual_serial_adapters=(ADAPTER,),
        **options,
    )
    with pytest.raises(HMCError, match="Serial creation outcome unknown") as raised:
        await core._create_via_cli(target.hmc, SYSTEM, "system", creation)
    assert PRIVATE not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert target.events.count("create") == 1
    assert "stamp" not in target.events and "apply" not in target.events
    target.hmc.create_logical_partition.assert_not_awaited()
    target.hmc.delete_logical_partition.assert_not_awaited()
