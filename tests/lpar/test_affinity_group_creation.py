"""Native affinity creation preserves containment and ordered side effects."""

import shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import core, workflows
from hmcpctl.operations.lpar.assignments import AssignmentResult, LparPcieAssignments
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
            if b.fault == "read":
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
            if b.fault == "mismatch" and kind == "lpar":
                values["affinity_group_id"] = "2"
            if b.fault == "profile-mismatch" and kind == "prof":
                values["affinity_group_id"] = "2"
            header = ",".join(fields)
            if b.fault == "quoted-header":
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
        assert result is b.entry
    else:
        step = (
            result.affinity_group_step
            if kind == "direct"
            else next(s for s in result.steps if s.step == "affinity_group")
        )
        assert step.status == "ok" and step.result["status"] == "verified"
        assert set(step.result["observed"]) == {"native-partition", "native-profile"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "read",
        "identity",
        "environment",
        "mismatch",
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
    assert result.workflow_completed
    assert "current" not in str(result) and "runtime" not in str(result)


@pytest.mark.asyncio
async def test_controller_explicit_apply_refused(target):
    target.controller = True
    with pytest.raises(ValueError, match="apply"):
        await invoke(target, apply_profile=True)
    assert "create" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["missing", "wrong-uuid", "wrong-name", "wrong-env", "bad-id", "outside"]
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
    if matches:
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
