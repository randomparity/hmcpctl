"""Native affinity creation preserves containment and ordered side effects."""

import shlex
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from lpar.test_serial_creation import invoke as invoke_serial
from lpar.test_serial_creation import target as serial_target  # noqa: F401 - pytest fixture

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import assignments as assignment_ops
from hmcpctl.operations.lpar import core, workflows
from hmcpctl.operations.lpar.assignments import (
    AssignmentResult,
    DedicatedPcieAssignment,
    LparPcieAssignments,
    SriovLogicalPortAssignment,
    VnicAssignment,
)
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import ATOM_NS

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
PARTITION = "00000002-abcd-4ef0-8abc-000000000002"


@pytest.fixture
def target(monkeypatch):
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({})
    entry = {
        "UUID": PARTITION,
        "Resource": {
            "PartitionName": "part-R1",
            "PartitionID": "7",
            "PartitionType": "AIX/Linux",
            "PartitionState": "not activated",
            "DefaultProfileName": "default_profile",
        },
    }
    system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            "IsClassicHMCManagement": "true",
            "IsHMCPowerVMManagementController": "false",
            "IsPowerVMManagementController": "false",
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
        },
    }
    hmc.get_managed_system.return_value = system
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "sys-R1", "State": "operating"}
    ]
    hmc.list_logical_partitions.return_value = [entry]
    hmc.list_vios.return_value = [entry]
    hmc.get_logical_partition.return_value = entry
    hmc.get_vios.return_value = entry
    hmc.get_uom.return_value = entry
    hmc.find_partition_by_name.side_effect = [None, entry]
    hmc.find_vios_by_name.side_effect = [None, entry]
    hmc.create_logical_partition.return_value = entry
    hmc.create_vios.return_value = entry
    b = SimpleNamespace(
        hmc=hmc,
        entry=entry,
        system=system,
        events=[],
        fault=None,
        value="1",
        controller=False,
    )

    async def command(config, cmd):
        words = shlex.split(cmd)
        if cmd.startswith("lscomgmt"):
            b.events.append("authority")
            return "1" if b.controller else "null"
        if cmd.startswith("lssyscfg -r sys"):
            return f"{SYSTEM},sys-R1"
        if "affinity_group_id" in cmd and cmd.startswith("lssyscfg"):
            kind = words[words.index("-r") + 1]
            b.events.append(kind)
            if b.fault == "read" or b.fault == f"{kind}-read":
                raise HMCCLIError("PRIVATE")
            fields = words[words.index("-F") + 1].split(",")
            values = {
                "lpar_id": "7",
                "name": "default_profile" if kind == "prof" else "part-R1",
                "lpar_name": "part-R1",
                "lpar_env": {
                    "Virtual IO Server": "vioserver",
                    "OS400": "os400",
                    "AIX/Linux": "aixlinux",
                }[b.entry["Resource"]["PartitionType"]],
                "affinity_group_id": b.value,
            }
            if b.fault == "identity":
                values["lpar_id"] = "8"
            if b.fault == "environment":
                values["lpar_env"] = "os400"
            if b.fault == "lpar-value" and kind == "lpar":
                values["affinity_group_id"] = "PRIVATE"
            if b.fault == "lpar-identity" and kind == "lpar":
                values["lpar_id"] = "8"
            if b.fault == "mismatch" and kind == "lpar":
                values["affinity_group_id"] = "2"
            if b.fault == "profile-mismatch" and kind == "prof":
                values["affinity_group_id"] = "2"
            header = ",".join(fields)
            if b.fault == "quoted-header" or b.fault == f"{kind}-header":
                header = ",".join(f'"{field}"' for field in fields)
            if b.fault == "leading-blank":
                header = "\n" + header
            if b.fault == "extra-blank":
                header += "\n"
            row = ",".join(values[f] for f in fields)
            if b.fault == "malformed":
                row = "broken"
            return (
                header
                + "\n"
                + row
                + "\n"
                + (row + "\n" if b.fault == "multiple" else "")
            )
        event = "create" if cmd.startswith("mksyscfg") else "apply"
        if "description=" in cmd:
            event = "stamp"
        b.events.append(event)
        if b.fault == event:
            raise HMCCLIError("PRIVATE")
        return ""

    b.command = AsyncMock(side_effect=command)
    for module in [
        "hmcpctl.ssh.lpar",
        "hmcpctl.ssh.profiles",
        "hmcpctl.operations.lpar.linux_roles",
    ]:
        monkeypatch.setattr(module + ".run_hmc_command", b.command)
    b.assign = AsyncMock(return_value=AssignmentResult(True, False, ()))
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", b.assign)
    return b


async def invoke(b, kind="workflow", value=1, **options):
    if kind == "vios":
        b.entry["Resource"]["PartitionType"] = "Virtual IO Server"
        return await create_vios(
            b.hmc,
            SYSTEM,
            "part-R1",
            LparResources(desired_memory=1024),
            affinity_group_id=value,
            **options,
        )
    creation = core.LparCreation(
        "part-R1",
        b.entry["Resource"]["PartitionType"],
        LparResources(desired_memory=1024),
        affinity_group_id=value,
        **options,
    )
    if kind == "direct":
        return await core.create_and_stamp_lpar(b.hmc, SYSTEM, creation)
    return await workflows.create_lpar(b.hmc, SYSTEM, creation, LparPcieAssignments())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
@pytest.mark.parametrize("value", [None, "none", 1, 255])
async def test_native_authority_and_omission(target, monkeypatch, kind, value):
    b = target
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", b.command)
    b.value = str(value)
    result = await invoke(b, kind, value)
    if value is None:
        assert "authority" not in b.events and "lpar" not in b.events
        return
    assert b.events.count("create") == 1
    assert (
        b.events.index("authority") < b.events.index("create") < b.events.index("lpar")
    )
    assert "prof" in b.events
    b.hmc.get_console_info.assert_not_awaited()
    if kind == "vios":
        assert result["UUID"] == b.entry["UUID"]
        assert (
            result["affinity_verification"]["status"] == "saved-configuration-verified"
        )
    else:
        step = (
            result.affinity_group_step
            if kind == "direct"
            else next(s for s in result.steps if s.step == "affinity_group")
        )
        assert (
            step.status == "ok"
            and step.result["status"] == "saved-configuration-verified"
        )
        assert set(step.result["observed"]) == {"native-partition", "native-profile"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "read",
        "identity",
        "environment",
        "profile-mismatch",
        "malformed",
        "multiple",
        "quoted-header",
        "leading-blank",
        "extra-blank",
    ],
)
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
async def test_observation_domain(target, monkeypatch, failure, kind):
    b = target
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", b.command)
    b.fault = failure
    if kind == "vios":
        with pytest.raises(HMCError, match="exists or may exist") as exc:
            await invoke(b, kind)
        assert "PRIVATE" not in str(exc.value)
    else:
        result = await invoke(b, kind, apply_profile=True)
        step = (
            result.affinity_group_step
            if kind == "direct"
            else next(s for s in result.steps if s.step == "affinity_group")
        )
        assert step.status == "error"
        assert step.result["status"] == (
            "mismatch" if "mismatch" in failure else "readback-unavailable"
        )
        assert "apply" not in b.events
        b.assign.assert_not_awaited()
    assert b.events.count("create") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
async def test_partial_effect_barriers(target, kind):
    target.fault = "create"
    with pytest.raises(HMCError, match="unknown") as error:
        await invoke(target, kind)
    assert "PRIVATE" not in str(error.value) and error.value.__cause__ is None
    assert target.events.count("create") == 1 and "apply" not in target.events
    target.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, 0, 256, "1", 1.0])
async def test_invalid_before_any_io(target, value):
    with pytest.raises((ValueError, TypeError), match="affinity_group_id"):
        await invoke(target, value=value)
    assert target.hmc.mock_calls == [] and target.events == []


@pytest.mark.asyncio
async def test_controller_grammar_and_apply(target, monkeypatch):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.controller = True
    result = await invoke(target, apply_profile=False)
    record = next(
        shlex.split(c.args[1])[-1]
        for c in target.command.await_args_list
        if c.args[1].startswith("mksyscfg")
    )
    assert "profile_name=" not in record and "prof" not in target.events
    assert not result.workflow_completed
    step = next(s for s in result.steps if s.step == "affinity_group")
    assert step.result["status"] == "readback-unavailable"
    assert step.result["observation_status"] == {"native-partition": "matches-request"}
    target.assign.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_explicit_apply_refused(target):
    target.controller = True
    with pytest.raises(ValueError, match="apply"):
        await invoke(target, apply_profile=True)
    assert "create" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "wrong-uuid",
        "wrong-name",
        "wrong-env",
        "bad-id",
        "outside",
        "missing-profile",
        "changed-profile",
    ],
)
async def test_contained_fresh_identity_before_observation(target, monkeypatch, case):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    fresh = deepcopy(target.entry)
    if case == "missing":
        fresh = None
    elif case == "wrong-uuid":
        fresh["UUID"] = SYSTEM
    elif case == "wrong-name":
        fresh["Resource"]["PartitionName"] = "other"
    elif case == "wrong-env":
        fresh["Resource"]["PartitionType"] = "OS400"
    elif case == "bad-id":
        fresh["Resource"]["PartitionID"] = "07"
    elif case == "missing-profile":
        fresh["Resource"].pop("DefaultProfileName")
    elif case == "changed-profile":
        fresh["Resource"]["DefaultProfileName"] = "other"
    else:
        target.hmc.list_logical_partitions.return_value = []
    target.hmc.get_logical_partition.return_value = fresh
    result = await invoke(target, apply_profile=True)
    step = next(s for s in result.steps if s.step == "affinity_group")
    assert result.resource_created and not result.workflow_completed
    assert step.result["status"] == "readback-unavailable"
    assert not {"lpar", "prof", "apply"} & set(target.events)
    target.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "256", "01", "NONE", "1.0", " 1", "", "none "])
async def test_noncanonical_observation_is_unavailable(target, monkeypatch, value):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.value = value
    result = await invoke(target, apply_profile=True)
    assert (
        next(s for s in result.steps if s.step == "affinity_group").result["status"]
        == "readback-unavailable"
    )
    target.assign.assert_not_awaited()
    assert "apply" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_ownership_failure_blocks_followups(target, monkeypatch, required):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.fault = "stamp"
    if required:
        with pytest.raises(HMCError, match="stamp_policy"):
            await invoke(target, stamp_policy="required", apply_profile=True)
    else:
        result = await invoke(target, apply_profile=True)
        assert result.resource_created and result.ownership_stamped is False
        assert not result.workflow_completed
    target.assign.assert_not_awaited()
    assert "apply" not in target.events and target.events.count("create") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sibling", ["role", "hardware"])
@pytest.mark.parametrize("matches", [False, True])
async def test_mixed_creation_domains(target, monkeypatch, sibling, matches):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    if sibling == "role":
        target.controller = True
        target.entry["Resource"]["PowerVMManagementCapable"] = (
            "false" if matches else "true"
        )
        # The existing native settings parser receives a complete fixed field row.
        command = target.command.side_effect

        async def mixed(config, cmd):
            if "-F lpar_env,allow_perf_collection," in cmd:
                return "aixlinux,0,0,0,0,0"
            return await command(config, cmd)

        target.command.side_effect = mixed
        result = await invoke(
            target,
            settings=LparSettings(powervm_mgmt_capable=False),
            apply_profile=False,
        )
        assert target.events.count("authority") == 1
        assert "prof" not in target.events
        # A selected role remains a separate prerequisite, including readback failure.
        assert next(s for s in result.steps if s.step == "settings").status == (
            "ok" if matches else "error"
        )
    else:
        target.system["Resource"]["AssociatedSystemCapabilities"] = {
            "HardwareMemoryEncryptionCapable": "true"
        }
        target.entry["Resource"]["PartitionMemoryConfiguration"] = {
            "MemoryEncryptionHardwareAccessEnabled": "true" if matches else "false"
        }

        async def raw(path, kind, *, uuid_path_arguments):
            entry = target.system if kind == "ManagedSystem" else target.entry
            root = Element(f"{{{ATOM_NS}}}entry")
            SubElement(root, f"{{{ATOM_NS}}}id").text = entry["UUID"]
            resource = SubElement(
                SubElement(root, f"{{{ATOM_NS}}}content"), f"{{{UOM_NS}}}{kind}"
            )

            def add(parent, key, value):
                node = SubElement(parent, f"{{{UOM_NS}}}{key}")
                if isinstance(value, dict):
                    for name, item in value.items():
                        add(node, name, item)
                else:
                    node.text = value

            for key, value in entry["Resource"].items():
                add(resource, key, value)
            return tostring(root, encoding="unicode")

        target.hmc._get.side_effect = raw
        result = await invoke(target, hardware_mem_encryption=True, apply_profile=True)
        assert next(s for s in result.steps if s.step == "hardware_memory").status == (
            "ok" if matches else "error"
        )
        assert (
            next(s for s in result.steps if s.step == "affinity_group").status == "ok"
        )
        assert ("apply" in target.events) is matches
    if matches and sibling != "role":
        target.assign.assert_awaited_once()
        assert result.workflow_completed
    else:
        target.assign.assert_not_awaited()
        assert not result.workflow_completed


@pytest.mark.asyncio
async def test_affinity_does_not_require_linux(target, monkeypatch):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.entry["Resource"]["PartitionType"] = "OS400"
    result = await invoke(target, apply_profile=True)
    assert result.workflow_completed and "apply" in target.events
    target.hmc.get_console_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow"])
async def test_keylock_conflict_is_before_io(target, kind):
    with pytest.raises(ValueError, match="keylock"):
        await invoke(target, kind, keylock="normal")
    assert target.hmc.mock_calls == [] and target.events == []


@pytest.mark.asyncio
async def test_controller_initial_profile_io_refused(target):
    target.controller = True
    with pytest.raises(ValueError, match="I/O"):
        await invoke(
            target,
            apply_profile=False,
            physical_io=LparPhysicalIO(lpar_io_pool_ids=("1",)),
        )
    assert "create" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
async def test_created_identity_unavailable_retains_uncertainty(target, kind):
    lookup = (
        target.hmc.find_vios_by_name
        if kind == "vios"
        else target.hmc.find_partition_by_name
    )
    lookup.side_effect = [None, HMCError("PRIVATE")]
    if kind == "vios":
        with pytest.raises(HMCError, match="exists or may exist") as caught:
            await invoke(target, kind)
        assert caught.value.__cause__ is None and "PRIVATE" not in str(caught.value)
    else:
        result = await invoke(target, kind, apply_profile=True)
        assert (
            result.resource_created
            and result.lpar is None
            and result.ownership_stamped is None
        )
        step = (
            result.affinity_group_step
            if kind == "direct"
            else next(s for s in result.steps if s.step == "affinity_group")
        )
        assert step.result["status"] == "readback-unavailable"
        assert "PRIVATE" not in str(result)
    assert target.events.count("create") == 1
    assert not {"lpar", "prof", "apply", "stamp"} & set(target.events)
    target.assign.assert_not_awaited()


@pytest.fixture
def mixed_layout(target, monkeypatch):
    """Construct protocol rows at the external boundary for C06/C08 composition."""
    import csv
    import io

    state = SimpleNamespace(fault=None)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.affinity_group.run_hmc_command", target.command
    )

    async def read(config, command):
        columns = shlex.split(command)[shlex.split(command).index("-F") + 1].split(",")
        if command.startswith("lshwres"):
            target.events.append("layout-admission")
            values = ["1:64,1:128", "1:128,1:256"]
        else:
            target.events.append("layout-read")
            if state.fault == "unavailable":
                raise HMCCLIError("PRIVATE-LAYOUT")
            values = [
                "part-R1",
                "default_profile",
                "null",
                "null",
                "null",
                "1:64" if state.fault == "mismatch" else "1:128",
                "null",
                "null",
            ]
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerow(values)
        return stream.getvalue()

    state.read = AsyncMock(side_effect=read)
    monkeypatch.setattr("hmcpctl.ssh.lpar_memory_layout.run_hmc_command", state.read)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow"])
@pytest.mark.parametrize(
    "fault,affinity_status,layout_status",
    [
        (None, "saved-configuration-verified", "verified"),
        ("affinity-mismatch", "mismatch", "verified"),
        ("affinity-unavailable", "readback-unavailable", "verified"),
        ("layout-mismatch", "saved-configuration-verified", "mismatch"),
        ("layout-unavailable", "saved-configuration-verified", "unavailable"),
        ("stamp", "saved-configuration-verified", "verified"),
        ("controller", "readback-unavailable", "unavailable"),
    ],
)
async def test_mixed_layout_affinity_keeps_independent_results_and_barriers(
    target, mixed_layout, kind, fault, affinity_status, layout_status
):
    from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

    b = target
    if fault == "controller":
        b.controller = True
    elif fault == "stamp":
        b.fault = "stamp"
    elif fault and fault.startswith("affinity-"):
        b.fault = "profile-mismatch" if fault.endswith("mismatch") else "read"
    elif fault:
        mixed_layout.fault = fault.removeprefix("layout-")
    result = await invoke(
        b,
        kind,
        memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
        apply_profile=not b.controller,
    )
    assert result.resource_created
    assert result.ownership_stamped is (fault != "stamp")
    steps = (
        {step.step: step for step in result.steps}
        if kind == "workflow"
        else {
            "affinity_group": result.affinity_group_step,
            "memory_layout": result.memory_layout_step,
            "apply_profile": result.apply_step,
        }
    )
    assert steps["affinity_group"].result["status"] == affinity_status
    assert steps["memory_layout"].result.status == layout_status
    verified = fault is None
    assert steps["apply_profile"].status == ("ok" if verified else "skipped")
    if kind == "workflow":
        assert result.workflow_completed is verified
        assert b.assign.await_count == int(verified)
    assert b.events.count("authority") == 1
    assert b.events.count("create") == 1
    assert b.events.count("apply") == int(verified)
    assert "PRIVATE" not in str(result)
    create = next(
        call.args[1]
        for call in b.command.await_args_list
        if call.args[1].startswith("mksyscfg")
    )
    record = shlex.split(create)[-1]
    assert "affinity_group_id=1" in record and "hpt_ratio=1:128" in record
    assert ("profile_name=" not in record) is b.controller
    assert b.events.index("layout-admission") < b.events.index("create")
    assert b.events.count("layout-read") == int(not b.controller)
    b.hmc.get_console_info.assert_not_awaited()
    assert not any(
        "lpar_proc_compat_mode" in call.args[1] for call in b.command.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("affinity", [None, 1])
@pytest.mark.parametrize("fault", [None, "mismatch", "unavailable", "controller"])
async def test_vios_layout_mode_only_and_mixed_share_one_authority(
    target, mixed_layout, affinity, fault
):
    from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

    b = target
    b.entry["Resource"]["PartitionType"] = "Virtual IO Server"
    b.controller = fault == "controller"
    mixed_layout.fault = fault if fault != "controller" else None
    call = create_vios(
        b.hmc,
        SYSTEM,
        "part-R1",
        LparResources(desired_memory=1024),
        memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
        affinity_group_id=affinity,
    )
    if fault:
        with pytest.raises(HMCError, match="VIOS exists or may exist") as caught:
            await call
        assert "PRIVATE" not in str(caught.value)
    else:
        result = await call
        assert result["UUID"] == b.entry["UUID"]
        assert ("affinity_verification" in result) is (affinity is not None)
    assert b.events.count("authority") == 1
    assert b.events.count("create") == 1
    assert b.events.count("layout-read") == int(not b.controller)
    assert b.events.count("lpar") == int(affinity is not None and fault is None)
    b.hmc.get_console_info.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    assert "apply" not in b.events
    record = shlex.split(
        next(
            call.args[1]
            for call in b.command.await_args_list
            if call.args[1].startswith("mksyscfg")
        )
    )[-1]
    assert "hpt_ratio=1:128" in record
    assert ("affinity_group_id=1" in record) is (affinity is not None)
    assert ("profile_name=" not in record) is b.controller


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
async def test_mixed_affinity_does_not_bypass_layout_admission(
    target, mixed_layout, kind
):
    from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

    with pytest.raises(ValueError, match="not advertised"):
        await invoke(target, kind, memory_layout=LparMemoryLayout(hpt_ratio="1:32"))
    assert target.events == ["authority", "layout-admission"]
    target.assign.assert_not_awaited()
    target.hmc.create_logical_partition.assert_not_awaited()
    target.hmc.create_vios.assert_not_awaited()


def test_affinity_appends_after_landed_memory_layout_inputs():
    import inspect
    from dataclasses import fields

    from hmcpctl.ssh.lpar import create_lpar_via_cli

    assert [item.name for item in fields(core.LparCreation)][-5:] == [
        "memory_layout",
        "virtual_serial_adapters",
        "creation_memory",
        "affinity_group_id",
        "start_settings",
    ]
    assert [item.name for item in fields(core.LparCreationResult)][-4:] == [
        "memory_layout_step",
        "serial_step",
        "creation_memory_step",
        "affinity_group_step",
    ]
    assert list(inspect.signature(create_vios).parameters)[-6:] == [
        "memory_layout",
        "max_virtual_slots",
        "virtual_serial_adapters",
        "creation_memory",
        "affinity_group_id",
        "start_settings",
    ]
    assert list(inspect.signature(create_lpar_via_cli).parameters)[-5:] == [
        "memory_layout",
        "virtual_serial_adapters",
        "creation_memory",
        "affinity_group_id",
        "start_settings",
    ]


@pytest.fixture
def assignment_target(target, mixed_layout, monkeypatch):
    """Keep admission real; provide native authority and foreign inventory replies."""
    from hmcpctl.operations.virtualization import pcie

    b = target
    b.system["Resource"]["AssociatedSystemCapabilities"] = {
        "KvmOnPowerVMCapable": "true"
    }
    b.entry["Resource"]["KvmCapable"] = "false"
    b.hmc.get_console_info.return_value = {
        "Resource": {
            "VersionInfo": {
                "Version": "10",
                "Release": "3",
                "ServicePackName": "1060",
                "Minor": "1",
            }
        }
    }
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.linux_roles.get_proc_compat_modes",
        AsyncMock(return_value=[]),
    )
    original = b.command.side_effect
    b.replies = ["null", "1"]

    async def command(config, cmd):
        if cmd.startswith("lscomgmt"):
            observed = b.replies.pop(0)
            b.events.append("authority:" + observed)
            return observed
        if "lpar_env,allow_perf_collection" in cmd:
            return "aixlinux,1,127,0,0,0"
        return await original(config, cmd)

    b.command.side_effect = command
    monkeypatch.setattr(
        assignment_ops, "resolve_ssh_names", AsyncMock(return_value=("sys-R1", None))
    )

    async def environment(*args):
        b.events.append("inventory")
        return "Version: 10\nRelease: 3\nService Pack: 1060", "8375-42A"

    monkeypatch.setattr(pcie, "read_sriov_environment", environment)
    for name, items in [
        ("list_sriov_adapters", [SimpleNamespace(mode="sriov", availability="1")]),
        (
            "list_sriov_physical_ports",
            [
                SimpleNamespace(
                    availability="up", minimum_capacity_granularity_percent=Decimal(1)
                )
            ],
        ),
        ("list_sriov_logical_ports", []),
    ]:
        monkeypatch.setattr(
            assignment_ops,
            name,
            AsyncMock(
                return_value=SimpleNamespace(
                    capability="available", items=items, system="sys-R1"
                )
            ),
        )
    for name in ["list_sriov_configured_logical_port_rows", "list_vnic_backing_rows"]:
        monkeypatch.setattr(assignment_ops, name, AsyncMock(return_value=[]))
    return b


def assignment_creation(domain):
    from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

    selections = {
        "C05": {"settings": LparSettings(kvm_capable=False)},
        "C06": {"memory_layout": LparMemoryLayout(hpt_ratio="1:128")},
        "C08": {"affinity_group_id": 1},
    }
    options = (
        selections[domain]
        if domain != "mixed"
        else {k: v for fields in selections.values() for k, v in fields.items()}
    )
    return core.LparCreation(
        "part-R1",
        "AIX/Linux",
        LparResources(desired_memory=1024),
        apply_profile=False,
        **options,
    )


def profile_assignments(kind):
    item = (
        DedicatedPcieAssignment("default_profile", "21010001")
        if kind == "dedicated"
        else SriovLogicalPortAssignment(
            "default_profile", "1", "0", "27004002", Decimal(2)
        )
    )
    return LparPcieAssignments(**{kind: (item,)})


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["C05", "C06", "C08", "mixed"])
@pytest.mark.parametrize("kind", ["dedicated", "sriov"])
@pytest.mark.parametrize("route", ["workflow", "direct"])
async def test_assignment_authority_is_observed_once(
    assignment_target, domain, kind, route
):
    b = assignment_target
    if route == "workflow":
        result = await workflows.create_lpar(
            b.hmc, SYSTEM, assignment_creation(domain), profile_assignments(kind)
        )
        assert result.workflow_completed
        b.assign.assert_awaited_once_with(
            b.hmc, SYSTEM, "part-R1", profile_assignments(kind)
        )
    else:
        result = await core.create_and_stamp_lpar(
            b.hmc,
            SYSTEM,
            assignment_creation(domain),
            preflight_assignments=profile_assignments(kind),
        )
        b.assign.assert_not_awaited()
    assert result.resource_created
    assert [e for e in b.events if e.startswith("authority:")] == ["authority:null"]
    assert (
        b.events.index("authority:null")
        < b.events.index("inventory")
        < b.events.index("create")
    )
    record = next(
        c.args[1] for c in b.command.await_args_list if c.args[1].startswith("mksyscfg")
    )
    assert "profile_name=default_profile" in record
    assert b.hmc.get_console_info.await_count == int(domain in ("C05", "mixed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["C05", "C06", "C08", "mixed"])
@pytest.mark.parametrize("kind", ["dedicated", "sriov"])
async def test_controller_assignment_denial_precedes_inventory(
    assignment_target, domain, kind
):
    b = assignment_target
    b.replies = ["1"]
    with pytest.raises(ValueError, match="controller mode"):
        await workflows.create_lpar(
            b.hmc, SYSTEM, assignment_creation(domain), profile_assignments(kind)
        )
    assert b.events == ["authority:1"]
    b.hmc.create_logical_partition.assert_not_awaited()
    b.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,selected,error",
    [
        (False, True, TypeError),
        (LparPcieAssignments(), True, ValueError),
        (LparPcieAssignments(vnics=(object(),)), True, ValueError),
        (profile_assignments("dedicated"), False, ValueError),
    ],
)
async def test_preflight_assignments_rejects_unsupported_direct_use(
    target, requested, selected, error
):
    creation = assignment_creation("C08")
    if not selected:
        creation = replace(creation, affinity_group_id=None)
    with pytest.raises(error, match="preflight_assignments"):
        await core.create_and_stamp_lpar(
            target.hmc, SYSTEM, creation, preflight_assignments=requested
        )
    assert target.events == [] and target.hmc.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["duplicate", "vnic", "inventory", "initial-sriov"])
async def test_assignment_prevalidation_survives_authority_owner_move(
    assignment_target, monkeypatch, fault
):
    from hmcpctl.operations.virtualization.vnic import VnicBackingSelector

    b = assignment_target
    creation = assignment_creation("C08")
    requested = profile_assignments("dedicated")
    message = "duplicate"
    if fault == "duplicate":
        requested = replace(requested, dedicated=requested.dedicated * 2)
    elif fault == "vnic":
        requested = replace(
            requested,
            vnics=(
                VnicAssignment(
                    VnicBackingSelector("vios-R1", "1", "1", "0", Decimal(2)), 4095
                ),
            ),
        )
        message = "port_vlan_id"
    else:
        requested = profile_assignments("sriov")
        monkeypatch.setattr(
            assignment_ops,
            "list_sriov_adapters",
            AsyncMock(return_value=SimpleNamespace(capability="unavailable")),
        )
        message = "unavailable"
        if fault == "initial-sriov":
            creation = replace(creation, physical_io=LparPhysicalIO(console_slot="hmc"))
    with pytest.raises(ValueError, match=message):
        await workflows.create_lpar(b.hmc, SYSTEM, creation, requested)
    assert b.events[0] == "authority:null" and "create" not in b.events
    b.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["none", 1, 255])
@pytest.mark.parametrize("environment", ["AIX/Linux", "OS400", "Virtual IO Server"])
async def test_saved_profile_with_native_mismatch(
    target, monkeypatch, value, environment
):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.entry["Resource"]["PartitionType"] = environment
    target.value, target.fault = str(value), "mismatch"
    kind = "vios" if environment == "Virtual IO Server" else "direct"
    result = await invoke(target, kind, value)
    proof = (
        result["affinity_verification"]
        if kind == "vios"
        else result.affinity_group_step.result
    )
    assert proof["status"] == "saved-configuration-verified"
    assert proof["observed"] == {"native-partition": 2, "native-profile": value}
    assert proof["observation_status"] == {
        "native-partition": "mismatch",
        "native-profile": "matches-request",
    }
    assert "physical placement are unproved" in proof["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,saved",
    [
        ("lpar-read", True),
        ("lpar-value", True),
        ("lpar-header", True),
        ("prof-read", False),
        ("profile-mismatch", False),
        ("lpar-identity", False),
    ],
)
async def test_saved_proof_is_independent(target, monkeypatch, fault, saved):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.fault = fault
    result = await invoke(target, "direct")
    proof = result.affinity_group_step.result
    assert (result.affinity_group_step.status == "ok") is saved
    assert "lpar" in target.events and "prof" in target.events
    assert proof["status"] == (
        "saved-configuration-verified"
        if saved
        else "mismatch"
        if fault == "profile-mismatch"
        else "readback-unavailable"
    )
    assert "PRIVATE" not in str(proof)
    if saved:
        assert proof["observation_status"]["native-partition"] == "readback-unavailable"
        assert proof["observed"]["native-profile"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [None, "skipped", "error", "ok"])
async def test_saved_affinity_followup_barriers(target, monkeypatch, ack):
    from hmcpctl.operations.lpar import affinity_group
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    apply = AsyncMock(
        return_value=None if ack is None else WorkflowStep("apply_profile", ack)
    )
    monkeypatch.setattr(core, "_apply_created_profile", apply)
    result = await invoke(target, apply_profile=True)
    apply.assert_awaited_once()
    assert result.workflow_completed is (ack == "ok")
    assert target.assign.await_count == int(ack == "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "workflow", "vios"])
async def test_affinity_sharing_weight_refusal(target, kind):
    creation = core.LparCreation(
        "part-R1", "AIX/Linux", LparResources(uncapped_weight=64), affinity_group_id=1
    )
    with pytest.raises(ValueError, match="uncapped_weight.*only.*update"):
        if kind == "vios":
            await create_vios(
                target.hmc, SYSTEM, "part-R1", creation.resources, affinity_group_id=1
            )
        elif kind == "direct":
            await core.create_and_stamp_lpar(target.hmc, SYSTEM, creation)
        else:
            await workflows.create_lpar(
                target.hmc, SYSTEM, creation, profile_assignments("dedicated")
            )
    assert target.hmc.mock_calls == [] and target.events == []


@pytest.mark.asyncio
async def test_vios_saved_affinity_result(target, monkeypatch):
    from hmcpctl.operations.lpar import affinity_group

    monkeypatch.setattr(affinity_group, "run_hmc_command", target.command)
    target.fault = "prof-read"
    with pytest.raises(HMCError) as exc:
        await invoke(target, "vios")
    assert "native-partition" in str(exc.value) and "matches-request" in str(exc.value)
    assert "native-profile" in str(exc.value) and "readback-unavailable" in str(
        exc.value
    )
    assert "PRIVATE" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "identity", "profile", "stamp", "timeout"])
async def test_affinity_serial_barrier_union(serial_target, monkeypatch, fault):  # noqa: F811 - imported pytest fixture
    from hmcpctl.operations.lpar import affinity_group

    b = serial_target
    if fault == "stamp" and b.kind == "VirtualIOServer":
        pytest.skip("VIOS has no ownership stamp owner")
    b.fault = fault
    b.hmc.get_vios.return_value = b.local

    async def observe(config, cmd):
        if "affinity_group_id" not in cmd:
            return await b.command(config, cmd)
        words = shlex.split(cmd)
        fields = words[words.index("-F") + 1].split(",")
        values = {
            "lpar_id": "9",
            "lpar_name": "new",
            "name": "default_profile" if "prof" in words else "new",
            "lpar_env": {
                "AIX/Linux": "aixlinux",
                "OS400": "os400",
                "Virtual IO Server": "vioserver",
            }[b.environment],
            "affinity_group_id": "1" if "prof" in words else "none",
        }
        return ",".join(fields) + "\n" + ",".join(values[f] for f in fields)

    monkeypatch.setattr(affinity_group, "run_hmc_command", observe)
    assign = AsyncMock(return_value=AssignmentResult(True, False, ()))
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", assign)
    options = {} if b.kind == "VirtualIOServer" else {"apply_profile": True}
    if fault == "timeout" or (b.kind == "VirtualIOServer" and fault):
        with pytest.raises(HMCError):
            await invoke_serial(b, affinity_group_id=1, **options)
        assign.assert_not_awaited()
    else:
        result = await invoke_serial(b, affinity_group_id=1, **options)
        if b.kind == "VirtualIOServer":
            assert (
                result["affinity_verification"]["status"]
                == "saved-configuration-verified"
            )
            assert result["serial_verification"]["configuration"] == "verified"
        else:
            assert result.workflow_completed is (fault is None)
            assert assign.await_count == int(fault is None)
            assert ("apply" in b.events) is (fault is None)
    assert b.events.count("controller") == 1
    assert b.events.count("create") == 1
    if fault in {"identity", "timeout"}:
        assert "stamp" not in b.events


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dedicated", "sriov"])
async def test_affinity_serial_controller_assignment_barrier(assignment_target, kind):
    from hmcpctl.documents.lpar_serial import VirtualSerialAdapter

    b = assignment_target
    b.replies = ["1"]
    creation = replace(
        assignment_creation("C08"),
        max_virtual_slots=8,
        virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
    )
    with pytest.raises(ValueError, match="controller mode"):
        await workflows.create_lpar(b.hmc, SYSTEM, creation, profile_assignments(kind))
    assert b.events == ["authority:1"]
    b.assign.assert_not_awaited()
