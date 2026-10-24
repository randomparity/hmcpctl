"""Creation owners enforce hardware admission and independent follow-up barriers."""

import itertools
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import core, workflows
from hmcpctl.operations.lpar.assignments import AssignmentResult, LparPcieAssignments
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import ATOM_NS

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
PARTITION = "00000002-abcd-4ef0-8abc-000000000002"
ENCRYPTION = "MemoryEncryptionHardwareAccessEnabled"
EXPANSION = "MemoryExpansionHardwareAccessEnabled"
TUPLE_FIELDS = (
    "IsClassicHMCManagement",
    "IsHMCPowerVMManagementController",
    "IsPowerVMManagementController",
)
PRIVATE = "private-transport-marker"


def xml_entry(kind, entry):
    root = Element(f"{{{ATOM_NS}}}entry")
    SubElement(root, f"{{{ATOM_NS}}}id").text = entry.get("UUID", "")
    content = SubElement(root, f"{{{ATOM_NS}}}content")
    resource = SubElement(content, f"{{{UOM_NS}}}{kind}")

    def add(parent, name, value):
        node = SubElement(parent, f"{{{UOM_NS}}}{name}")
        if isinstance(value, dict):
            for key, item in value.items():
                add(node, key, item)
        else:
            node.text = value

    for name, value in entry["Resource"].items():
        add(resource, name, value)
    return tostring(root, encoding="unicode")


@pytest.fixture
def target(monkeypatch):
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    entry = {
        "UUID": PARTITION,
        "Resource": {
            "PartitionName": "part-R1",
            "PartitionType": "AIX/Linux",
            "PartitionState": "not activated",
            "PendingSecureBoot": "0",
            "PartitionMemoryConfiguration": {ENCRYPTION: "true", EXPANSION: "false"},
        },
    }
    system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            **dict(zip(TUPLE_FIELDS, ("true", "false", "false"), strict=True)),
            "AssociatedSystemCapabilities": {
                "HardwareMemoryEncryptionCapable": "true",
                "HardwareMemoryCompressionCapable": "true",
                "PartitionSecureBootCapable": "true",
            },
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
        },
    }
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "sys-R1", "State": "operating"}
    ]
    hmc.get_managed_system.return_value = system
    hmc.get_logical_partition.return_value = entry
    hmc.get_uom.return_value = entry
    hmc.get_console_info.return_value = {"Resource": {}}
    hmc.list_logical_partitions.return_value = [entry]
    hmc.list_vios.return_value = [entry]
    hmc.find_partition_by_name.side_effect = [None, entry]
    hmc.find_vios_by_name.side_effect = [None, entry]
    hmc.create_logical_partition.return_value = entry
    hmc.create_vios.return_value = entry
    b = SimpleNamespace(hmc=hmc, entry=entry, system=system, events=[], fault=None)

    async def read(path, kind, *, uuid_path_arguments):
        assert path == f"/rest/api/uom/{kind}/{uuid_path_arguments['uuid']}"
        b.events.append(kind)
        if kind != "ManagedSystem" and b.fault == "read":
            raise HMCError(PRIVATE)
        return xml_entry(kind, system if kind == "ManagedSystem" else entry)

    async def command(config, cmd):
        if cmd.startswith("lssyscfg -r sys"):
            return f"{SYSTEM},sys-R1"
        action = "create" if cmd.startswith("mksyscfg") else "apply"
        if "description=" in cmd:
            action = "stamp"
        b.events.append(action)
        if b.fault == action:
            raise HMCCLIError(PRIVATE)
        return ""

    hmc._get.side_effect = read
    b.command = AsyncMock(side_effect=command)
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", b.command)
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", b.command)
    b.assign = AsyncMock(return_value=AssignmentResult(True, False, ()))
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", b.assign)
    return b


def creation(**options):
    return core.LparCreation(
        "part-R1", "AIX/Linux", LparResources(desired_memory=1024), **options
    )


async def invoke(b, vios=False, *, direct=False, **options):
    if vios:
        b.entry["Resource"]["PartitionType"] = "Virtual IO Server"
        return await create_vios(
            b.hmc, SYSTEM, "part-R1", LparResources(desired_memory=1024), **options
        )
    if direct:
        return await core.create_and_stamp_lpar(b.hmc, SYSTEM, creation(**options))
    return await workflows.create_lpar(
        b.hmc, SYSTEM, creation(**options), LparPcieAssignments()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encryption,expansion", tuple(itertools.product((None, False, True), repeat=2))
)
@pytest.mark.parametrize("apply_profile", [None, False, True])
async def test_verified_readback_precedes_optional_apply(
    target, encryption, expansion, apply_profile
):
    b = target
    memory = b.entry["Resource"]["PartitionMemoryConfiguration"]
    memory.update(
        {
            ENCRYPTION: str(bool(encryption)).lower(),
            EXPANSION: str(bool(expansion)).lower(),
        }
    )
    result = await invoke(
        b,
        hardware_mem_encryption=encryption,
        hardware_mem_expansion=expansion,
        apply_profile=apply_profile,
    )
    requested = encryption is not None or expansion is not None
    hardware = [step for step in result.steps if step.step == "hardware_memory"]
    assert len(hardware) == int(requested)
    assert result.workflow_completed and result.ownership_stamped is True
    if requested:
        assert hardware[0].result["status"] == "verified"
        assert b.events.count("create") == 1
        assert b.events.index("stamp") < b.events.index("LogicalPartition")
        if apply_profile:
            assert b.events.index("LogicalPartition") < b.events.index("apply")
            names = [step.step for step in result.steps]
            assert names.index("hardware_memory") < names.index("apply_profile")
        record = next(
            shlex.split(call.args[1])[-1]
            for call in b.command.await_args_list
            if call.args[1].startswith("mksyscfg")
        )
        for name, value in (
            ("hardware_mem_encryption", encryption),
            ("hardware_mem_expansion", expansion),
        ):
            assert (f"{name}=" in record) == (value is not None)
            if value is not None:
                assert f"{name}={int(value)}" in record
        b.hmc.create_logical_partition.assert_not_awaited()
    assert b.events.count("apply") == int(requested and apply_profile is True)
    b.assign.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "create",
        "read",
        "mismatch",
        "missing",
        "foreign",
        "state",
        "kind",
        "name",
        "uuid",
        "identity-read",
        "no-entry",
    ],
)
async def test_failures_never_apply_or_retry(target, failure):
    b = target
    if failure in {"create", "read"}:
        b.fault = failure
    elif failure == "mismatch":
        b.entry["Resource"]["PartitionMemoryConfiguration"][ENCRYPTION] = "false"
    elif failure == "missing":
        b.entry["Resource"]["PartitionMemoryConfiguration"].pop(ENCRYPTION)
    elif failure == "foreign":
        b.hmc.list_logical_partitions.return_value = []
    elif failure in {"state", "kind", "name"}:
        key = {
            "state": "PartitionState",
            "kind": "PartitionType",
            "name": "PartitionName",
        }[failure]
        b.entry["Resource"][key] = "unexpected"
    elif failure == "uuid":
        b.entry["UUID"] = "invalid"
    else:
        b.hmc.find_partition_by_name.side_effect = [
            None,
            HMCError(PRIVATE) if failure == "identity-read" else None,
        ]
    if failure == "create":
        with pytest.raises(HMCError, match="unknown") as error:
            await invoke(b, hardware_mem_encryption=True, apply_profile=True)
        assert PRIVATE not in str(error.value)
    else:
        result = await invoke(b, hardware_mem_encryption=True, apply_profile=True)
        hardware = next(step for step in result.steps if step.step == "hardware_memory")
        assert hardware.status == "error"
        assert hardware.result["status"] == (
            "mismatch" if failure == "mismatch" else "readback-unavailable"
        )
        assert PRIVATE not in repr(result)
        assert not result.workflow_completed
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp_policy", ["required", "best-effort"])
@pytest.mark.parametrize("failure", ["stamp", "read", "identity-read", "uuid"])
async def test_workflow_blocks_assignments_after_hardware_or_ownership_failure(
    target, stamp_policy, failure
):
    b = target
    b.fault = failure
    if failure == "identity-read":
        b.hmc.find_partition_by_name.side_effect = [None, HMCError(PRIVATE)]
    if failure == "uuid":
        b.entry["UUID"] = "invalid"
    if stamp_policy == "required" and failure != "read":
        with pytest.raises(HMCError) as error:
            await invoke(
                b,
                hardware_mem_expansion=False,
                stamp_policy=stamp_policy,
                apply_profile=True,
            )
        assert PRIVATE not in str(error.value)
    else:
        result = await invoke(
            b,
            hardware_mem_expansion=False,
            stamp_policy=stamp_policy,
            apply_profile=True,
        )
        assert not result.workflow_completed
        assert result.resource_created
    b.assign.assert_not_awaited()
    assert "apply" not in b.events
    assert b.events.count("create") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["workflow", "lpar", "vios"])
@pytest.mark.parametrize("field", ["hardware_mem_encryption", "hardware_mem_expansion"])
@pytest.mark.parametrize("value", [0, 1, "false", PRIVATE, {}, []])
async def test_bad_input_or_keylock_refuses_before_io(target, owner, field, value):
    with pytest.raises(TypeError) as error:
        await invoke(target, owner == "vios", direct=owner == "lpar", **{field: value})
    assert PRIVATE not in str(error.value)
    assert target.hmc.mock_calls == []
    target.command.assert_not_awaited()
    target.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["hardware_mem_encryption", "hardware_mem_expansion"])
@pytest.mark.parametrize("value", [False, True])
async def test_explicit_keylock_refuses_before_io(target, field, value):
    with pytest.raises(ValueError, match="keylock"):
        await invoke(target, keylock="normal", **{field: value})
    assert target.hmc.mock_calls == []
    target.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
async def test_omitted_hardware_retains_rest_and_no_hardware_reads(target, vios):
    result = await invoke(target, vios)
    target.hmc._get.assert_not_awaited()
    assert "create" not in target.events
    if vios:
        assert result is target.entry
        target.hmc.create_vios.assert_awaited_once()
        assert "stamp" not in target.events
    else:
        assert all(step.step != "hardware_memory" for step in result.steps)
        target.hmc.create_logical_partition.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize("phase", ["admission", "readback"])
async def test_existing_security_policy_barrier_is_retained(target, vios, phase):
    b = target
    if phase == "admission":
        b.system["Resource"]["AssociatedSystemCapabilities"][
            "PartitionSecureBootCapable"
        ] = "false"
    else:
        b.entry["Resource"]["PendingSecureBoot"] = "1"
    options = {
        "hardware_mem_encryption": True,
        "security_policy": LparSecurityPolicy(secure_boot=0),
    }
    if phase == "admission" or vios:
        with pytest.raises(ValueError if phase == "admission" else HMCError):
            await invoke(b, vios, **options)
    else:
        result = await invoke(b, apply_profile=True, **options)
        assert not result.workflow_completed
        assert (
            next(step for step in result.steps if step.step == "hardware_memory").status
            == "ok"
        )
        assert (
            next(step for step in result.steps if step.step == "security_policy").status
            == "error"
        )
    assert b.events.count("create") == int(phase != "admission")
    assert "apply" not in b.events
    b.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "create",
        "read",
        "mismatch",
        "foreign",
        "identity-read",
        "no-entry",
        "uuid",
    ],
)
async def test_vios_existing_owner_and_truthful_partial_result(target, failure):
    b = target
    b.fault = failure
    if failure == "mismatch":
        b.entry["Resource"]["PartitionMemoryConfiguration"][EXPANSION] = "true"
    if failure == "foreign":
        b.hmc.list_vios.return_value = []
    if failure in {"identity-read", "no-entry"}:
        b.hmc.find_vios_by_name.side_effect = [
            None,
            HMCError(PRIVATE) if failure == "identity-read" else None,
        ]
    if failure == "uuid":
        b.entry["UUID"] = "invalid"
    if failure:
        with pytest.raises(HMCError, match="exists or may exist") as error:
            await invoke(b, True, hardware_mem_expansion=False)
        assert PRIVATE not in str(error.value)
        if failure != "create":
            assert (
                "mismatch" if failure == "mismatch" else "readback-unavailable"
            ) in str(error.value)
    else:
        assert await invoke(b, True, hardware_mem_expansion=False) is b.entry
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
    b.hmc.create_vios.assert_not_awaited()
    b.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize(
    "observed",
    [
        None,
        ("false", "false", "false"),
        ("true", "true", "false"),
        ("true", "false", "true"),
        ("yes", "false", "false"),
    ],
)
async def test_tuple_refusal_precedes_creation(target, vios, observed):
    b = target
    for key in TUPLE_FIELDS:
        b.system["Resource"].pop(key)
    if observed:
        b.system["Resource"].update(zip(TUPLE_FIELDS, observed, strict=True))
    with pytest.raises(ValueError, match="selected-system") as error:
        await invoke(b, vios, hardware_mem_encryption=False)
    assert "may exist" not in str(error.value)
    assert not {"create", "stamp", "apply"} & set(b.events)
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    assert b.events == ["ManagedSystem"]


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize(
    "failure", ["duplicate", "capacity", "capability", "default-capacity"]
)
async def test_existing_creation_admission_is_retained(target, vios, failure):
    b = target
    resources = LparResources(desired_memory=1024)
    if failure == "duplicate":
        b.hmc.find_partition_by_name.side_effect = [b.entry]
        b.hmc.find_vios_by_name.side_effect = [b.entry]
    elif failure == "capability":
        b.system["Resource"]["AssociatedSystemCapabilities"][
            "HardwareMemoryEncryptionCapable"
        ] = "false"
    else:
        b.system["Resource"]["AssociatedSystemMemoryConfiguration"][
            "ConfigurableSystemMemory"
        ] = "512" if failure == "capacity" else "2048"
        if failure == "default-capacity":
            resources = LparResources(desired_procs=0.1)
    with pytest.raises(ValueError):
        if vios:
            await create_vios(
                b.hmc, SYSTEM, "part-R1", resources, hardware_mem_encryption=False
            )
        else:
            await core.create_and_stamp_lpar(
                b.hmc,
                SYSTEM,
                core.LparCreation(
                    "part-R1", "AIX/Linux", resources, hardware_mem_encryption=False
                ),
            )
    assert not {"create", "stamp", "apply"} & set(b.events)
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["OS400", "Virtual IO Server"])
@pytest.mark.parametrize(
    "encryption,expansion", tuple(itertools.product((None, False, True), repeat=2))
)
async def test_environment_paths_keep_false_and_omission(
    target, environment, encryption, expansion
):
    b = target
    b.entry["Resource"]["PartitionType"] = environment
    b.entry["Resource"]["PartitionMemoryConfiguration"] = {
        ENCRYPTION: str(bool(encryption)).lower(),
        EXPANSION: str(bool(expansion)).lower(),
    }
    options = {
        "hardware_mem_encryption": encryption,
        "hardware_mem_expansion": expansion,
    }
    if environment == "Virtual IO Server":
        assert await invoke(b, True, **options) is b.entry
        assert "stamp" not in b.events
    else:
        result = await core.create_and_stamp_lpar(
            b.hmc,
            SYSTEM,
            core.LparCreation(
                "part-R1", environment, LparResources(desired_memory=1024), **options
            ),
        )
        assert result.ownership_stamped is True
        assert (
            result.hardware_memory_step is None
            or result.hardware_memory_step.status == "ok"
        )
    requested = encryption is not None or expansion is not None
    assert b.events.count("create") == int(requested)
    assert b.hmc._get.await_count == 2 * int(requested)
    if requested:
        record = next(
            shlex.split(call.args[1])[-1]
            for call in b.command.await_args_list
            if call.args[1].startswith("mksyscfg")
        )
        for key, value in options.items():
            assert (f"{key}=" in record) == (value is not None)
            if value is not None:
                assert f"{key}={int(value)}" in record


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
async def test_malformed_created_identity_blocks_stamping_and_followups(target, vios):
    b = target
    b.entry["Resource"] = [PRIVATE]
    if vios:
        with pytest.raises(HMCError, match="readback-unavailable") as error:
            await create_vios(
                b.hmc,
                SYSTEM,
                "part-R1",
                LparResources(desired_memory=1024),
                hardware_mem_expansion=False,
            )
        assert PRIVATE not in str(error.value)
    else:
        result = await invoke(b, hardware_mem_expansion=False, apply_profile=True)
        assert not result.workflow_completed
        step = next(step for step in result.steps if step.step == "hardware_memory")
        assert step.result["status"] == "readback-unavailable"
        assert PRIVATE not in repr(step)
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
    b.assign.assert_not_awaited()
