"""Creation owners enforce hardware admission and independent follow-up barriers."""

import asyncio
import itertools
import shlex
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources, LparSettings
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vios,phase",
    [(False, "create"), (True, "create"), (True, "identity"), (True, "read")],
)
@pytest.mark.parametrize("field", ["hardware_mem_encryption", "hardware_mem_expansion"])
@pytest.mark.parametrize("policy", [False, True])
async def test_hardware_exception_chain_is_private(target, vios, phase, field, policy):
    b = target
    b.fault = phase
    if phase == "identity":
        b.hmc.find_vios_by_name.side_effect = [None, HMCError(PRIVATE)]
    options = {field: False}
    if policy:
        options["security_policy"] = LparSecurityPolicy(secure_boot=0)
    with pytest.raises(HMCError) as raised:
        await invoke(b, vios, **options)
    error = raised.value
    expected = (
        "VIOS exists or may exist; creation outcome unknown. Inspect before retrying. No second create or rollback performed"
        if vios and phase == "create"
        else "Native hardware memory creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed."
        if phase == "create"
        else "VIOS exists or may exist; hardware memory access readback-unavailable. Inspect before retrying. No second create or rollback performed"
    )
    assert str(error) == expected
    assert PRIVATE not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()
    b.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["lpar-omitted", "lpar-policy", "vios-policy-native", "vios-policy-identity"],
)
async def test_hardware_exception_chain_preserves_legacy_diagnostics(target, case):
    b = target
    b.fault = "create"
    policy = LparSecurityPolicy(secure_boot=0)
    if case == "vios-policy-identity":
        b.fault = None
        b.hmc.find_vios_by_name.side_effect = [None, HMCError(PRIVATE)]
    with pytest.raises(HMCError) as raised:
        if case == "lpar-omitted":
            await core.create_and_stamp_lpar(
                b.hmc,
                SYSTEM,
                core.LparCreation("part-R1", "AIX/Linux", LparResources()),
            )
        else:
            await invoke(b, case.startswith("vios"), security_policy=policy)
    error = raised.value
    if case == "lpar-omitted":
        assert str(error) == (
            f"Creation outcome unknown for 'part-R1': {PRIVATE}. Inspect whether the partition exists before retrying; ownership stamping has not run."
        )
    else:
        expected = {
            "lpar-policy": "Native policy creation outcome is unknown; the partition may exist. Inspect its scoped identity and profile before retrying; no rollback performed.",
            "vios-policy-native": "VIOS exists or may exist; creation outcome unknown. Inspect before retrying. No second create or rollback performed",
            "vios-policy-identity": "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed",
        }
        assert str(error) == expected[case]
    assert error.__cause__ is not None and error.__context__ is not None
    assert PRIVATE in "".join(traceback.format_exception(error))
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    b.assign.assert_not_awaited()


@pytest.fixture
def identity_conflict(target, monkeypatch):
    """Serve conflicting post-create identities through the real lookup provider."""
    b = target
    b.hmc.list_system_summaries.return_value[0]["SystemName"] = PRIVATE
    original_read = b.hmc._get.side_effect
    b.conflict = "ambiguous"
    b.before_create = False
    b.lookup_failure = None
    for method in (
        "find_partition_by_name",
        "list_logical_partitions",
        "find_vios_by_name",
        "list_vios",
    ):
        monkeypatch.setattr(b.hmc, method, getattr(HMCClient, method).__get__(b.hmc))

    async def read(path, kind, **kwargs):
        if path == f"/rest/api/uom/ManagedSystem/{SYSTEM}/{kind}":
            if "create" not in b.events and not b.before_create:
                return ""
            if b.lookup_failure is not None:
                raise b.lookup_failure
            if b.conflict in {"scalar", "empty", "attributes", "name-list"}:
                resource = {
                    "scalar": f'<{kind} xmlns="{UOM_NS}">{PRIVATE}</{kind}>',
                    "empty": f'<{kind} xmlns="{UOM_NS}"/>',
                    "attributes": f'<{kind} xmlns="{UOM_NS}" detail="{PRIVATE}"/>',
                    "name-list": (
                        f'<{kind} xmlns="{UOM_NS}">'
                        "<PartitionName>part-R1</PartitionName>"
                        f"<PartitionName>{PRIVATE}</PartitionName></{kind}>"
                    ),
                }[b.conflict]
                return (
                    f'<entry xmlns="{ATOM_NS}"><id>{PARTITION}</id>'
                    f"<content>{resource}</content></entry>"
                )
            other = dict(b.entry)
            other["UUID"] = (
                "00000003-abcd-4ef0-8abc-000000000003"
                if b.conflict == "ambiguous"
                else PARTITION
            )
            return (
                f'<feed xmlns="{ATOM_NS}">'
                + xml_entry(kind, b.entry)
                + xml_entry(kind, other)
                + "</feed>"
            )
        return await original_read(path, kind, **kwargs)

    b.hmc._get.side_effect = read
    return b


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp_policy", ["required", "best-effort"])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("field", ["hardware_mem_encryption", "hardware_mem_expansion"])
@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("conflict", ["ambiguous", "duplicate"])
async def test_post_create_identity_conflict_is_bounded(
    identity_conflict, stamp_policy, direct, field, value, conflict
):
    b = identity_conflict
    b.conflict = conflict
    result = None
    error = None
    try:
        result = await invoke(
            b,
            direct=direct,
            stamp_policy=stamp_policy,
            apply_profile=True,
            **{field: value},
        )
    except (HMCError, ValueError) as caught:
        error = caught
    if stamp_policy == "required":
        assert PRIVATE not in "".join(traceback.format_exception(error))
        assert isinstance(error, HMCError)
        assert "creation may have succeeded" in str(error)
        assert "Inspect the contained partition before retrying" in str(error)
        assert error.__cause__ is None and error.__context__ is None
    else:
        assert error is None
        assert result.resource_created and result.lpar is None
        assert result.ownership_stamped is None
        step = (
            result.hardware_memory_step
            if direct
            else next(step for step in result.steps if step.step == "hardware_memory")
        )
        assert step.status == "error"
        assert step.result["status"] == "readback-unavailable"
        assert "inspect the contained partition before retrying" in " ".join(
            result.warnings
        )
        assert PRIVATE not in repr(result)
        if not direct:
            assert not result.workflow_completed
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp_policy", ["required", "best-effort"])
async def test_omitted_hardware_preserves_lookup_value_error(
    identity_conflict, stamp_policy
):
    b = identity_conflict
    with pytest.raises(ValueError, match="Ambiguous LPAR") as raised:
        await core.create_and_stamp_lpar(
            b.hmc,
            SYSTEM,
            core.LparCreation(
                "part-R1", "AIX/Linux", LparResources(), stamp_policy=stamp_policy
            ),
        )
    assert PRIVATE in str(raised.value)
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events


@pytest.mark.asyncio
async def test_pre_create_lookup_conflict_stays_pre_effect(identity_conflict):
    b = identity_conflict
    b.before_create = True
    with pytest.raises(ValueError, match="Ambiguous LPAR") as raised:
        await invoke(b, hardware_mem_encryption=False, apply_profile=True)
    assert "may" not in str(raised.value)
    assert b.events == []
    b.command.assert_not_awaited()
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        "vios",
        "direct-required",
        "direct-best-effort",
        "workflow-required",
        "workflow-best-effort",
    ],
)
@pytest.mark.parametrize(
    "shape", ["scalar", "empty", "attributes", "name-list", "transport"]
)
async def test_unavailable_post_create_lookup_is_bounded(
    identity_conflict, caller, shape
):
    b = identity_conflict
    b.conflict = shape
    if shape == "transport":
        b.lookup_failure = RuntimeError(PRIVATE)
    vios = caller == "vios"
    required = caller.endswith("required")
    options = (
        {}
        if vios
        else {
            "stamp_policy": "required" if required else "best-effort",
            "apply_profile": True,
        }
    )
    error = None
    result = None
    try:
        result = await invoke(
            b,
            vios,
            direct=caller.startswith("direct"),
            hardware_mem_expansion=False,
            **options,
        )
    except (HMCError, AttributeError, RuntimeError) as caught:
        error = caught
    if vios or required:
        assert isinstance(error, HMCError)
        assert "may" in str(error) and "inspect" in str(error).lower()
        if vios:
            assert "readback-unavailable" in str(error)
        assert PRIVATE not in "".join(traceback.format_exception(error))
        assert error.__cause__ is None and error.__context__ is None
    else:
        assert error is None
        assert result.resource_created and result.lpar is None
        assert result.ownership_stamped is None
        step = (
            result.hardware_memory_step
            if caller.startswith("direct")
            else next(step for step in result.steps if step.step == "hardware_memory")
        )
        assert step.status == "error"
        assert step.result["status"] == "readback-unavailable"
        assert "inspect" in " ".join(result.warnings).lower()
        assert PRIVATE not in repr(result)
        if caller.startswith("workflow"):
            assert not result.workflow_completed
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()
    b.hmc.delete_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize(
    "phase",
    [
        "omitted",
        "pre-create",
        "cancel-before",
        "cancel-after",
        "native-failure",
        "native-cancel",
    ],
)
async def test_lookup_boundary_preserves_other_failures(identity_conflict, vios, phase):
    b = identity_conflict
    b.conflict = "scalar"
    b.before_create = phase in {"pre-create", "cancel-before"}
    expected = AttributeError
    options = {"hardware_mem_expansion": False}
    if phase == "omitted":
        options = {"security_policy": LparSecurityPolicy(secure_boot=0)}
    if phase.startswith("cancel"):
        b.lookup_failure = asyncio.CancelledError(PRIVATE)
        expected = asyncio.CancelledError
    elif phase.startswith("native"):
        original_command = b.command.side_effect
        expected = asyncio.CancelledError if phase == "native-cancel" else RuntimeError

        async def fail_native(config, command):
            if command.startswith("mksyscfg"):
                b.events.append("create")
                raise expected(PRIVATE)
            return await original_command(config, command)

        b.command.side_effect = fail_native
    with pytest.raises(expected) as raised:
        await invoke(b, vios, **options)
    assert "may" not in str(raised.value)
    assert b.events.count("create") == int(not b.before_create)
    assert "stamp" not in b.events and "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    b.hmc.create_vios.assert_not_awaited()


@pytest.fixture
def mixed_target(target, monkeypatch):
    """Construct independent native authority and hardware observation inputs."""
    b = target
    original = b.command.side_effect
    b.controller_status = "1"

    async def command(config, cmd):
        if cmd.startswith("lscomgmt"):
            assert cmd == "lscomgmt -m sys-R1 -F is_controller"
            b.events.append("controller")
            return b.controller_status
        if cmd.startswith("lssyscfg -r lpar"):
            b.events.append("settings")
            return "aixlinux,0,127,0,0,0"
        return await original(config, cmd)

    b.command.side_effect = command
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.linux_roles.run_hmc_command", b.command
    )
    b.entry["Resource"]["PowerVMManagementCapable"] = "false"
    return b


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("management", [False, True])
@pytest.mark.parametrize("apply_profile", [None, False])
async def test_mixed_controller_creation_uses_one_profileless_record(
    mixed_target, selected, management, apply_profile
):
    b = mixed_target
    b.entry["Resource"]["PowerVMManagementCapable"] = str(management).lower()
    b.entry["Resource"]["PartitionMemoryConfiguration"] = {
        ENCRYPTION: str(selected).lower(),
        EXPANSION: str(not selected).lower(),
    }
    result = await invoke(
        b,
        settings=LparSettings(powervm_mgmt_capable=management),
        hardware_mem_encryption=selected,
        hardware_mem_expansion=not selected,
        apply_profile=apply_profile,
    )
    creates = [
        shlex.split(call.args[1])
        for call in b.command.await_args_list
        if call.args[1].startswith("mksyscfg")
    ]
    assert len(creates) == 1
    attrs = dict(pair.split("=", 1) for pair in creates[0][-1].split(","))
    assert attrs["powervm_mgmt_capable"] == str(int(management))
    assert attrs["hardware_mem_encryption"] == str(int(selected))
    assert attrs["hardware_mem_expansion"] == str(int(not selected))
    assert "profile_name" not in attrs and "sync_curr_profile" not in attrs
    assert result.workflow_completed and result.ownership_stamped
    assert next(s for s in result.steps if s.step == "hardware_memory").status == "ok"
    assert next(s for s in result.steps if s.step == "settings").status == "ok"
    assert b.events.index("controller") < b.events.index("create")
    assert b.events.index("ManagedSystem") < b.events.index("create")
    assert "apply" not in b.events
    assert not any("-r prof" in call.args[1] for call in b.command.await_args_list)
    b.hmc.create_logical_partition.assert_not_awaited()
    b.assign.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["authority", "apply", "capability", "tuple", "malformed"]
)
async def test_mixed_creation_keeps_independent_pre_effect_admissions(
    mixed_target, failure
):
    b = mixed_target
    if failure == "authority":
        b.controller_status = "null"
    elif failure == "capability":
        b.system["Resource"]["AssociatedSystemCapabilities"][
            "HardwareMemoryEncryptionCapable"
        ] = "false"
    elif failure == "tuple":
        b.system["Resource"][TUPLE_FIELDS[1]] = "true"
    elif failure == "malformed":
        b.system["Resource"][TUPLE_FIELDS[0]] = {"child": "true"}
    with pytest.raises(ValueError):
        await invoke(
            b,
            settings=LparSettings(powervm_mgmt_capable=False),
            hardware_mem_encryption=False,
            apply_profile=failure == "apply",
        )
    assert not {"create", "stamp", "apply"} & set(b.events)
    b.assign.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "missing", "read", "role-readback"])
async def test_mixed_current_readback_failure_blocks_followups(mixed_target, failure):
    b = mixed_target
    if failure == "mismatch":
        b.entry["Resource"]["PartitionMemoryConfiguration"][EXPANSION] = "true"
    elif failure == "missing":
        b.entry["Resource"]["PartitionMemoryConfiguration"].pop(EXPANSION)
    elif failure == "read":
        b.fault = "read"
    else:
        b.entry["Resource"].pop("PowerVMManagementCapable")
    result = await invoke(
        b,
        settings=LparSettings(powervm_mgmt_capable=False),
        hardware_mem_expansion=False,
        apply_profile=False,
    )
    assert result.resource_created and not result.workflow_completed
    step = next(
        s
        for s in result.steps
        if s.step == ("settings" if failure == "role-readback" else "hardware_memory")
    )
    assert step.status == "error"
    assert PRIVATE not in repr(result)
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    b.assign.assert_not_awaited()
    b.hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["create", "identity"])
@pytest.mark.parametrize("controller", [False, True])
async def test_mixed_selected_hardware_retains_chain_free_failure(
    mixed_target, failure, controller
):
    b = mixed_target
    if failure == "create":
        b.fault = "create"
    else:
        b.hmc.find_partition_by_name.side_effect = [None, ValueError(PRIVATE)]
    with pytest.raises(HMCError) as error:
        await invoke(
            b,
            settings=LparSettings(powervm_mgmt_capable=False)
            if controller
            else LparSettings(),
            hardware_mem_expansion=False,
            apply_profile=False,
            stamp_policy="required",
        )
    assert error.value.__cause__ is None and error.value.__context__ is None
    if failure == "create":
        assert ("profile" in str(error.value)) is (not controller)
    assert "Inspect its scoped identity" in str(error.value) or failure == "identity"
    assert PRIVATE not in "".join(traceback.format_exception(error.value))
    assert b.events.count("create") == 1
    assert "apply" not in b.events
    b.assign.assert_not_awaited()
