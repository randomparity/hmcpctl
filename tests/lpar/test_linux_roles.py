"""Selected-system Linux role admission and one-shot settings outcomes."""

import json
import shlex
import traceback
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.errors import HMCError, HMCTLSFingerprintError, HMCTransportError
from hmcpctl.operations.lpar import configuration as operation
from hmcpctl.operations.lpar import linux_roles as roles
from hmcpctl.ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    create_lpar_via_cli,
    get_lpar_settings,
    set_lpar_settings,
)
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import leaf_text, parse_feed

SYSTEM = "0000000e-abcd-4ef0-8abc-00000000000e"
PARTITION = "0000000c-abcd-4ef0-8abc-00000000000c"
CONFIG = HMCConfig.from_mapping({})
BASE = LparSettings(False, 0, False, False, False)
CAPS = roles.RoleCapabilities(False, True, (10, 3, 1060, 1), (1060, 10), False)
MARKER = "REMOTE_PRIVATE_MARKER"


def captured(name):
    packet = Path(__file__).parents[1] / "fixtures" / "live" / name
    return parse_feed(json.loads(packet.read_text())["body"])[0]


def eligible_resource():
    """Synthetic eligible variant; the captured hardware is not KVM-capable."""
    resource = deepcopy(captured("rest-lpar-entry.json")["Resource"])
    resource.update(
        PartitionName="owned",
        PendingProcessorCompatibilityMode="POWER10",
    )
    resource["PartitionProcessorConfiguration"].update(
        HasDedicatedProcessors="true", SharingMode="keep idle procs"
    )
    return resource


@pytest.mark.parametrize(
    "path,value",
    [
        (("PartitionType",), "OS400"),
        (("PartitionType",), "Virtual IO Server"),
        (("PartitionType",), None),
        (("PartitionState",), "running"),
        (("PartitionState",), None),
        (("IsOperationInProgress",), "true"),
        (("IsOperationInProgress",), None),
        (("IsOperationInProgress",), "False"),
        (("PendingProcessorCompatibilityMode",), "default"),
        (("PendingProcessorCompatibilityMode",), None),
        (("PartitionProcessorConfiguration", "HasDedicatedProcessors"), "false"),
        (("PartitionProcessorConfiguration", "HasDedicatedProcessors"), None),
        (("PartitionProcessorConfiguration", "SharingMode"), "uncapped"),
        (("PartitionProcessorConfiguration", "SharingMode"), None),
        (("KeyStoreSize",), "4"),
        (("KeyStoreSize",), None),
        (("KeyStoreSize",), "bad"),
        (
            (
                "AssociatedPersistentMemoryConfiguration",
                "CurrentPersistentMemoryVolumes",
            ),
            "1",
        ),
        (
            (
                "AssociatedPersistentMemoryConfiguration",
                "CurrentPersistentMemoryVolumes",
            ),
            None,
        ),
        (
            (
                "AssociatedPersistentMemoryConfiguration",
                "CurrentPersistentMemoryVolumes",
            ),
            "bad",
        ),
    ],
)
def test_prerequisites_refuse(path, value):
    resource = eligible_resource()
    parent = resource
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        roles.admit_role_update(LparSettings(kvm_capable=True), CAPS, resource)


@pytest.mark.parametrize(
    "changes",
    [
        {"kvm": False},
        {"hmc_version": None},
        {"hmc_version": (10, 3, 1060, 0)},
        {"hmc_version": (10, 2, 1060, 1)},
        {"firmware": None},
        {"firmware": (1060, 9)},
        {"power11": True, "firmware": (1060, 10)},
    ],
)
def test_release_refuses(changes):
    with pytest.raises(ValueError):
        roles.admit_role_update(
            LparSettings(kvm_capable=True),
            replace(CAPS, **changes),
            eligible_resource(),
        )
    roles.admit_role_update(LparSettings(kvm_capable=True), CAPS, eligible_resource())
    roles.admit_role_update(
        LparSettings(kvm_capable=True),
        replace(CAPS, power11=True, firmware=(1110, 0)),
        eligible_resource(),
    )


def test_false_intent_and_management_only():
    resource = {"PartitionType": "AIX/Linux", "PartitionState": "running"}
    no_kvm = roles.RoleCapabilities(False, False, None, None, False)
    for value in (False, True):
        settings = LparSettings(powervm_mgmt_capable=value)
        assert roles.roles_requested(settings)
        roles.admit_role_update(settings, no_kvm, resource)
        with pytest.raises(ValueError):
            roles.admit_role_update(settings, no_kvm, {"PartitionType": "OS400"})
    resource.update(PartitionState="not activated", IsOperationInProgress="false")
    roles.admit_role_update(LparSettings(kvm_capable=False), CAPS, resource)
    for caps in (no_kvm, replace(CAPS, hmc_version=(10, 3, 1060, 0))):
        with pytest.raises(ValueError):
            roles.admit_role_update(LparSettings(kvm_capable=False), caps, resource)
    assert not roles.roles_requested(LparSettings(time_ref=False))


@pytest.fixture
def source_boundary(monkeypatch):
    system = deepcopy(captured("rest-ms-entry-mtms-pair.json")["Resource"])
    # Synthetic selected-system variant, never a positive native hardware claim.
    system["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = "true"
    system["SystemFirmware"] = "FW1060_FW1060.10 (10)"
    console = {
        "VersionInfo": {
            "Version": "10",
            "Release": "3",
            "ServicePackName": "1060",
            "Maintenance": "0",
            "Minor": "1",
        }
    }
    hmc = SimpleNamespace(
        config=CONFIG,
        get_managed_system=AsyncMock(return_value={"Resource": system}),
        get_console_info=AsyncMock(return_value={"Resource": console}),
    )
    modes = AsyncMock(return_value=["POWER9", "POWER10"])
    monkeypatch.setattr(roles, "get_proc_compat_modes", modes)
    status = AsyncMock(return_value="null\n")
    monkeypatch.setattr(roles, "run_hmc_command", status, raising=False)
    return SimpleNamespace(
        hmc=hmc, system=system, console=console, modes=modes, status=status
    )


@pytest.mark.asyncio
async def test_capability_source_decoding(source_boundary):
    b = source_boundary
    assert (
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=True, need_management=False
        )
        == CAPS
    )
    b.modes.return_value = ["POWER10", "POWER11"]
    b.system["SystemFirmware"] = {"text": "FW1110_FW1110.00 (1)"}
    assert (
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=True, need_management=False
        )
    ).power11
    for field in ("Version", "Release", "ServicePackName", "Minor"):
        old = b.console["VersionInfo"].pop(field)
        assert (
            await roles.read_role_capabilities(
                b.hmc, SYSTEM, need_kvm=True, need_management=False
            )
        ).hmc_version is None
        b.console["VersionInfo"][field] = old
    for bad in (None, "bad", "-1", "1.0", True):
        b.console["VersionInfo"]["Minor"] = bad
        assert (
            await roles.read_role_capabilities(
                b.hmc, SYSTEM, need_kvm=True, need_management=False
            )
        ).hmc_version is None
    b.system["IsClassicHMCManagement"] = "false"
    b.system["IsHMCPowerVMManagementController"] = "true"
    b.status.return_value = "1\n"
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=False, need_management=False
    )
    assert caps.controller_mode and caps.hmc_version is None
    b.status.return_value = "null\n"
    for classic, controller in (
        (None, "true"),
        ("true", None),
        ("true", "true"),
        ("false", "false"),
        ("bad", "false"),
    ):
        b.system.update(
            IsClassicHMCManagement=classic, IsHMCPowerVMManagementController=controller
        )
        with pytest.raises(ValueError):
            await roles.read_role_capabilities(
                b.hmc, SYSTEM, need_kvm=False, need_management=False
            )


@pytest.mark.asyncio
async def test_captured_console_fix_level(source_boundary):
    b = source_boundary
    console = captured("rest-management-console.json")["Resource"]
    # IBM MF71703/MF71704 identifies build 2408210051 as V10 R3 M1060.3:
    # https://delivery04-mul.dhe.ibm.com/sar/CMA/HMA/0chw6/4/vMF71703.readme.html
    assert leaf_text(console["VersionInfo"]["BuildLevel"]) == "2408210051"
    assert roles._role_console_version(console) == (10, 3, 1060, 3)
    b.hmc.get_console_info.return_value = {"Resource": console}
    b.system.clear()
    b.system.update(captured("rest-ms-entry-mtms-pair.json")["Resource"])
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=True, need_management=False
    )
    assert caps.hmc_version == (10, 3, 1060, 3)
    assert not caps.kvm and caps.firmware == (950, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize(
    "maintenance,minor", [("0", "0"), ("0", "1"), ("0", "3"), ("1", "0")]
)
async def test_source_fix_level_admission(
    source_boundary, maintenance, minor, selected
):
    from hmcpctl.operations.lpar.core import LparCreation

    b = source_boundary
    b.console["VersionInfo"].update(Maintenance=maintenance, Minor=minor)
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=True, need_management=False
    )
    assert caps.hmc_version == (10, 3, 1060, int(minor))
    settings = LparSettings(kvm_capable=selected)
    creation = LparCreation(
        "owned",
        "AIX/Linux",
        LparResources(dedicated=True, desired_procs=2),
        settings=settings,
    )
    if minor == "0":
        with pytest.raises(ValueError, match=r"HMC >=10\.3\.1060\.1"):
            roles.admit_role_update(settings, caps, eligible_resource())
        with pytest.raises(ValueError, match=r"HMC >=10\.3\.1060\.1"):
            roles.admit_role_creation(creation, caps)
    else:
        roles.admit_role_update(settings, caps, eligible_resource())
        roles.admit_role_creation(creation, caps)


@pytest.mark.asyncio
async def test_captured_source_shape(source_boundary):
    b = source_boundary
    b.system.clear()
    b.system.update(captured("rest-ms-entry-mtms-pair.json")["Resource"])
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=True, need_management=False
    )
    assert not caps.kvm and not caps.controller_mode and caps.firmware == (950, 0)
    resource = captured("rest-lpar-entry.json")["Resource"]
    evidence = roles.with_role_evidence(BASE, resource, LparSettings(kvm_capable=False))
    assert evidence.kvm_capable is False and evidence.powervm_mgmt_capable is False
    with pytest.raises(ValueError):
        roles.admit_role_update(LparSettings(kvm_capable=True), caps, resource)
    b.hmc.get_console_info.reset_mock()
    b.modes.reset_mock()
    await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=False, need_management=False
    )
    b.hmc.get_console_info.assert_not_awaited()
    b.modes.assert_not_awaited()


@pytest.fixture
def role_boundary(monkeypatch):
    module = operation
    resource = eligible_resource()
    entry = {"UUID": PARTITION, "Resource": resource}
    hmc = SimpleNamespace(
        config=CONFIG, get_logical_partition=AsyncMock(return_value=entry)
    )
    system = AsyncMock(return_value=SYSTEM)
    partition = AsyncMock(return_value=PARTITION)
    authorize = AsyncMock(return_value=("system", "owned"))
    caps = AsyncMock(return_value=CAPS)
    read = AsyncMock(return_value=BASE)
    write = AsyncMock()
    for name, value in (
        ("resolve_system_uuid", system),
        ("resolve_lpar_uuid", partition),
        ("resolve_and_authorize_lpar_names", authorize),
        ("read_role_capabilities", caps),
        ("get_lpar_settings", read),
        ("set_lpar_settings", write),
    ):
        monkeypatch.setattr(module, name, value)
    return SimpleNamespace(
        hmc=hmc,
        resource=resource,
        system=system,
        partition=partition,
        authorize=authorize,
        caps=caps,
        read=read,
        write=write,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,status",
    [
        ("ok", "verified"),
        ("mismatch", "mismatch"),
        ("collateral", "mismatch"),
        ("c01", "mismatch"),
        ("missing", "readback-unavailable"),
        ("preserved-missing", "readback-unavailable"),
        ("preserved-malformed", "readback-unavailable"),
        ("malformed", "readback-unavailable"),
        ("read", "readback-unavailable"),
        ("c01-read", "readback-unavailable"),
        ("identity", "readback-unavailable"),
        ("name", "readback-unavailable"),
        ("write", "write-outcome-unknown"),
    ],
)
async def test_role_outcomes(role_boundary, mode, status):
    b = role_boundary
    before = deepcopy(b.resource)
    after = deepcopy(before)
    after["KvmCapable"] = "true" if mode != "mismatch" else "false"
    if mode == "collateral":
        after["PowerVMManagementCapable"] = "true"
    if mode == "missing":
        after.pop("KvmCapable")
    if mode == "malformed":
        after["KvmCapable"] = "bad"
    if mode == "preserved-missing":
        after.pop("PowerVMManagementCapable")
    if mode == "preserved-malformed":
        after["PowerVMManagementCapable"] = "bad"
    if mode == "name":
        after["PartitionName"] = "foreign"
    b.hmc.get_logical_partition.side_effect = [
        {"UUID": PARTITION, "Resource": before},
        HMCError(MARKER)
        if mode == "read"
        else {"UUID": SYSTEM if mode == "identity" else PARTITION, "Resource": after},
    ]
    if mode == "c01":
        b.read.side_effect = [BASE, replace(BASE, time_ref=True)]
    if mode == "c01-read":
        b.read.side_effect = [BASE, HMCError(MARKER)]
    if mode == "write":
        b.write.side_effect = HMCError(MARKER)
    result = await operation.configure_lpar_settings(
        b.hmc, "owned", LparSettings(kvm_capable=True), system_name_or_uuid="system"
    )
    assert result.status == status
    assert MARKER not in repr(result)
    b.write.assert_awaited_once_with(
        b.hmc.config, "system", "owned", LparSettings(kvm_capable=True)
    )
    assert all(
        call.args == (PARTITION,)
        for call in b.hmc.get_logical_partition.await_args_list
    )
    b.partition.assert_any_await(b.hmc, "owned", system_name_or_uuid=SYSTEM)
    if status in ("verified", "mismatch"):
        assert result.observed is not None
        assert result.observed.kvm_capable is (mode != "mismatch")
    else:
        assert result.observed is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial", ["containment", "ownership", "identity-race", "admission"]
)
async def test_role_denial_precedes_write(role_boundary, denial):
    b = role_boundary
    if denial == "containment":
        b.partition.side_effect = ValueError("foreign selector")
    elif denial == "ownership":
        b.authorize.side_effect = PermissionError("foreign owner")
    elif denial == "identity-race":
        b.partition.side_effect = [PARTITION, SYSTEM]
    else:
        b.resource["PartitionType"] = "OS400"
    with pytest.raises((ValueError, PermissionError)):
        await operation.configure_lpar_settings(
            b.hmc, PARTITION, LparSettings(kvm_capable=True), system_name_or_uuid=SYSTEM
        )
    b.write.assert_not_awaited()
    if denial != "admission":
        b.hmc.get_logical_partition.assert_not_awaited()
        b.caps.assert_not_awaited()
        b.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [None, "true"])
async def test_unknown_preserved_role_not_fabricated(role_boundary, later):
    b = role_boundary
    b.resource.pop("PowerVMManagementCapable")
    b.resource["KvmCapable"] = "true"
    after = deepcopy(b.resource)
    if later is not None:
        after["PowerVMManagementCapable"] = later
    b.hmc.get_logical_partition.side_effect = [
        {"UUID": PARTITION, "Resource": b.resource},
        {"UUID": PARTITION, "Resource": after},
    ]
    result = await operation.configure_lpar_settings(
        b.hmc, "owned", LparSettings(kvm_capable=True), system_name_or_uuid=SYSTEM
    )
    assert result.status == "verified"
    assert result.observed.powervm_mgmt_capable is (True if later else None)


@pytest.mark.asyncio
async def test_no_role_path_unchanged(role_boundary):
    b = role_boundary
    result = await operation.configure_lpar_settings(
        b.hmc, "owned", LparSettings(time_ref=False), system_name_or_uuid="system"
    )
    assert result.status == "verified"
    b.authorize.assert_awaited_once_with(
        b.hmc, "owned", system_name_or_uuid="system", ownership_override=False
    )
    b.hmc.get_logical_partition.assert_not_awaited()
    b.caps.assert_not_awaited()
    b.system.assert_not_awaited()
    b.partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["powervm_mgmt_capable", "kvm_capable"])
@pytest.mark.parametrize("value", [False, True])
async def test_selected_boolean_write_and_preservation(role_boundary, name, value):
    b = role_boundary
    before = deepcopy(b.resource)
    after = deepcopy(before)
    after[roles.ROLE_FIELDS[name]] = "true" if value else "false"
    b.hmc.get_logical_partition.side_effect = [
        {"UUID": PARTITION, "Resource": before},
        {"UUID": PARTITION, "Resource": after},
    ]
    settings = LparSettings(**{name: value})
    result = await operation.configure_lpar_settings(
        b.hmc, "owned", settings, system_name_or_uuid=SYSTEM
    )
    assert result.status == "verified"
    assert getattr(result.observed, name) is value
    b.write.assert_awaited_once_with(CONFIG, "system", "owned", settings)
    assert b.caps.await_args.kwargs == {
        "need_kvm": name == "kvm_capable",
        "need_management": name == "powervm_mgmt_capable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["error", "name", "unsupported", "firmware"])
async def test_false_kvm_does_not_require_generation_evidence(source_boundary, unknown):
    b = source_boundary
    if unknown == "error":
        b.modes.side_effect = HMCError(MARKER)
    elif unknown == "name":
        b.system.pop("SystemName")
        with pytest.raises(ValueError, match="selected-system name"):
            await roles.read_role_capabilities(
                b.hmc, SYSTEM, need_kvm=True, need_management=False
            )
        b.status.assert_not_awaited()
        return
    elif unknown == "unsupported":
        b.modes.return_value = ["POWER9"]
    else:
        b.system["SystemFirmware"] = "bad"
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=True, need_management=False
    )
    roles.admit_role_update(LparSettings(kvm_capable=False), caps, eligible_resource())
    with pytest.raises(ValueError):
        roles.admit_role_update(
            LparSettings(kvm_capable=True), caps, eligible_resource()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_role_record_is_fixed_and_encoded(monkeypatch, value):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await set_lpar_settings(
        CONFIG,
        "system;quoted",
        "owned;name",
        LparSettings(powervm_mgmt_capable=value, kvm_capable=not value),
    )
    assert shlex.split(run.await_args.args[1]) == [
        "chsyscfg",
        "-r",
        "lpar",
        "-m",
        "system;quoted",
        "-i",
        f"name=owned;name,powervm_mgmt_capable={int(value)},kvm_capable={int(not value)}",
    ]
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad,name", "bad=name", 'bad"name', "bad\nname"])
@pytest.mark.parametrize("operation", ["create", "update", "read"])
async def test_role_record_refuses_selector_dispatch(monkeypatch, name, operation):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    with pytest.raises(HMCCLIError):
        if operation == "create":
            await create_lpar_via_cli(
                CONFIG, "system", name, settings=LparSettings(kvm_capable=True)
            )
        elif operation == "update":
            await set_lpar_settings(
                CONFIG, "system", name, LparSettings(kvm_capable=False)
            )
        else:
            await get_lpar_settings(CONFIG, "system", name)
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, DEFAULT_PROFILE_NAME, "custom;profile"])
@pytest.mark.parametrize("role", [None, "empty", "management", False, True])
async def test_native_same_record_and_profile(monkeypatch, profile, role):
    run = AsyncMock(return_value="created")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    settings = None
    if role == "empty":
        settings = LparSettings()
    elif role == "management":
        settings = LparSettings(powervm_mgmt_capable=True)
    elif role is not None:
        settings = LparSettings(powervm_mgmt_capable=False, kvm_capable=role)
    resources = LparResources(
        min_memory=1024,
        desired_memory=2048,
        max_memory=4096,
        min_procs=0.2,
        desired_procs=0.5,
        max_procs=1.0,
        min_vcpus=1,
        desired_vcpus=2,
        max_vcpus=4,
        uncapped=False,
    )
    result = await create_lpar_via_cli(
        CONFIG,
        "system;quoted",
        "owned;name",
        resources=resources,
        profile_name=profile,
        partition_id=7,
        max_virtual_slots=32,
        settings=settings,
    )
    assert result == "created"
    run.assert_awaited_once()
    words = shlex.split(run.await_args.args[1])
    assert words[:-1] == ["mksyscfg", "-r", "lpar", "-m", "system;quoted", "-i"]
    pairs = [pair.split("=", 1) for pair in words[-1].split(",")]
    record = dict(pairs)
    assert len(pairs) == len(record)
    expected = {
        "name": "owned;name",
        "lpar_env": "aixlinux",
        "lpar_id": "7",
        "max_virtual_slots": "32",
        "min_mem": "1024",
        "desired_mem": "2048",
        "max_mem": "4096",
        "proc_mode": "shared",
        "sharing_mode": "cap",
        "min_proc_units": "0.2",
        "desired_proc_units": "0.5",
        "max_proc_units": "1.0",
        "min_procs": "1",
        "desired_procs": "2",
        "max_procs": "4",
    }
    if profile is not None:
        expected["profile_name"] = profile
    if role == "management":
        expected["powervm_mgmt_capable"] = "1"
    elif role is False or role is True:
        expected |= {"powervm_mgmt_capable": "0", "kvm_capable": str(int(role))}
    if role is True:
        expected["lpar_proc_compat_mode"] = "POWER10"
    assert record == expected


@pytest.mark.asyncio
async def test_native_default_profile_and_all_resources(monkeypatch):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await create_lpar_via_cli(
        CONFIG, "system", "owned", settings=LparSettings(kvm_capable=True)
    )
    run.assert_awaited_once()
    record = shlex.split(run.await_args.args[1])[-1]
    assert dict(pair.split("=", 1) for pair in record.split(",")) == {
        "name": "owned",
        "lpar_env": "aixlinux",
        "profile_name": DEFAULT_PROFILE_NAME,
        "all_resources": "1",
        "kvm_capable": "1",
        "lpar_proc_compat_mode": "POWER10",
    }


@pytest.mark.asyncio
async def test_contained_uuid_case_is_not_an_identity_change(role_boundary):
    b = role_boundary
    b.partition.side_effect = [PARTITION.upper(), PARTITION]
    result = await operation.configure_lpar_settings(
        b.hmc,
        PARTITION.upper(),
        LparSettings(kvm_capable=False),
        system_name_or_uuid=SYSTEM,
    )
    assert result.status == "verified"
    b.write.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "management,kvm", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_combined_roles_submit_once(role_boundary, management, kvm):
    b = role_boundary
    before = deepcopy(b.resource)
    after = deepcopy(before)
    after.update(
        PowerVMManagementCapable=str(management).lower(), KvmCapable=str(kvm).lower()
    )
    b.hmc.get_logical_partition.side_effect = [
        {"UUID": PARTITION, "Resource": before},
        {"UUID": PARTITION, "Resource": after},
    ]
    settings = LparSettings(powervm_mgmt_capable=management, kvm_capable=kvm)
    result = await operation.configure_lpar_settings(
        b.hmc, "owned", settings, system_name_or_uuid=SYSTEM
    )
    assert result.status == "verified"
    b.write.assert_awaited_once_with(CONFIG, "system", "owned", settings)


@pytest.fixture
def creation_boundary(monkeypatch):
    from hmcpctl.operations.lpar import core, workflows

    entry = {"UUID": PARTITION, "Resource": {"PartitionName": "owned"}}
    resource = eligible_resource()
    resource["KvmCapable"] = "true"
    hmc = SimpleNamespace(
        config=CONFIG,
        find_partition_by_name=AsyncMock(side_effect=[None, entry]),
        get_logical_partition=AsyncMock(
            return_value={"UUID": PARTITION, "Resource": resource}
        ),
        create_logical_partition=AsyncMock(),
    )
    caps = AsyncMock(return_value=CAPS)
    create, apply, io, memory = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    compat = AsyncMock(return_value={"profile_mode": "POWER10"})
    stamp = AsyncMock(return_value=(True, ()))
    for name, mock in (
        ("resolve_system_uuid", AsyncMock(return_value=SYSTEM)),
        ("resolve_system_cli_name", AsyncMock(return_value="system")),
        ("read_role_capabilities", caps),
        ("create_lpar_via_cli", create),
        ("apply_lpar_profile_via_cli", apply),
        ("get_lpar_settings", AsyncMock(return_value=BASE)),
        ("get_lpar_proc_compat", compat),
        ("stamp_created_lpar_ownership", stamp),
        ("admit_creation_io", io),
        ("require_memory_within_system", memory),
        ("created_io_step", AsyncMock(return_value=None)),
    ):
        monkeypatch.setattr(core, name, mock, raising=False)
    monkeypatch.setattr(
        workflows, "resolve_system_uuid", AsyncMock(return_value=SYSTEM), raising=False
    )
    monkeypatch.setattr(workflows, "read_role_capabilities", caps, raising=False)
    prevalidate = AsyncMock()
    assign = AsyncMock(return_value=SimpleNamespace(workflow_completed=True, steps=()))
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", prevalidate)
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", assign)
    creation = core.LparCreation(
        "owned",
        "AIX/Linux",
        LparResources(dedicated=True, desired_procs=2),
        settings=LparSettings(kvm_capable=True),
    )
    return SimpleNamespace(
        hmc=hmc,
        creation=creation,
        resource=resource,
        entry=entry,
        caps=caps,
        create=create,
        apply=apply,
        io=io,
        memory=memory,
        compat=compat,
        stamp=stamp,
        assign=assign,
        prevalidate=prevalidate,
    )


@pytest.mark.parametrize(
    "resources",
    [
        LparResources(),
        LparResources(dedicated=True),
        LparResources(desired_memory=1024),
        LparResources(dedicated=False, desired_procs=1),
        LparResources(dedicated=True, desired_procs=1, sharing_mode="share_idle_procs"),
        LparResources(dedicated=True, desired_procs=1.5),
    ],
)
@pytest.mark.asyncio
async def test_creation_does_not_rewrite_cpus(creation_boundary, resources):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    with pytest.raises((ValueError, HMCError)):
        await create_and_stamp_lpar(
            b.hmc, SYSTEM, replace(b.creation, resources=resources)
        )
    b.create.assert_not_awaited()
    b.hmc.create_logical_partition.assert_not_awaited()
    assert b.creation.resources == LparResources(dedicated=True, desired_procs=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "controller,value",
    [(True, True), (True, False), (True, None), (False, True), (False, False)],
)
async def test_controller_apply_rejected_and_classic_preserved(
    creation_boundary, controller, value
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    b.caps.return_value = replace(CAPS, controller_mode=controller)
    creation = replace(b.creation, apply_profile=value)
    if controller and value is True:
        with pytest.raises(ValueError, match="apply.*False|apply.*false"):
            await create_and_stamp_lpar(b.hmc, SYSTEM, creation)
        b.create.assert_not_awaited()
        b.apply.assert_not_awaited()
        return
    result = await create_and_stamp_lpar(b.hmc, SYSTEM, creation)
    assert result.settings_step.status == "ok"
    assert b.create.await_args.kwargs["resources"] is creation.resources
    assert b.create.await_args.kwargs.get("profile_name", DEFAULT_PROFILE_NAME) == (
        None if controller else DEFAULT_PROFILE_NAME
    )
    if controller:
        b.apply.assert_not_awaited()
        b.compat.assert_not_awaited()
        assert not result.warnings
        if value is False:
            assert result.apply_step.status == "skipped"
            assert "current configuration" in result.apply_step.result
    else:
        b.compat.assert_awaited_once_with(
            CONFIG, "system", "owned", DEFAULT_PROFILE_NAME
        )
        assert b.apply.await_count == (1 if value else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["keylock", "physical", "creation-sriov", "keystore"])
async def test_profile_io_admission_before_effects(creation_boundary, kind):
    from decimal import Decimal

    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
    from hmcpctl.operations.lpar.assignments import SriovLogicalPortAssignment
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    b.caps.return_value = replace(CAPS, controller_mode=True)
    changes = {
        "keylock": {"keylock": "normal"},
        "physical": {"physical_io": LparPhysicalIO(console_slot="hmc")},
        "creation-sriov": {
            "creation_sriov": (
                SriovLogicalPortAssignment(
                    DEFAULT_PROFILE_NAME, "1", "0", "27004002", Decimal(2)
                ),
            )
        },
        "keystore": {"security_policy": LparSecurityPolicy(keystore_kbytes=8)},
    }
    with pytest.raises(ValueError):
        await create_and_stamp_lpar(b.hmc, SYSTEM, replace(b.creation, **changes[kind]))
    b.create.assert_not_awaited()
    b.apply.assert_not_awaited()
    b.io.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dedicated", "sriov"])
async def test_profile_assignment_rejected_before_create(creation_boundary, kind):
    from decimal import Decimal

    from hmcpctl.operations.lpar.assignments import (
        DedicatedPcieAssignment,
        LparPcieAssignments,
        SriovLogicalPortAssignment,
    )
    from hmcpctl.operations.lpar.workflows import create_lpar

    b = creation_boundary
    b.caps.return_value = replace(CAPS, controller_mode=True)
    item = (
        DedicatedPcieAssignment(DEFAULT_PROFILE_NAME, "21010010")
        if kind == "dedicated"
        else SriovLogicalPortAssignment(
            DEFAULT_PROFILE_NAME, "1", "0", "27004002", Decimal(2)
        )
    )
    with pytest.raises(ValueError, match="controller mode"):
        await create_lpar(
            b.hmc, SYSTEM, b.creation, LparPcieAssignments(**{kind: (item,)})
        )
    b.create.assert_not_awaited()
    b.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize("mode", ["wrong", "missing", "error"])
async def test_companion_mismatch_is_failed_settings_step(
    creation_boundary, controller, mode
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    b.caps.return_value = replace(CAPS, controller_mode=controller)
    b.resource["PendingProcessorCompatibilityMode"] = (
        MARKER if mode == "wrong" else None
    )
    b.compat.return_value = {"profile_mode": MARKER} if mode == "wrong" else {}
    if mode == "error":
        b.compat.side_effect = HMCError(MARKER)
        if controller:
            b.hmc.get_logical_partition.side_effect = HMCError(MARKER)
    result = await create_and_stamp_lpar(b.hmc, SYSTEM, b.creation)
    assert result.resource_created and result.ownership_stamped
    assert result.settings_step.status == "error"
    assert MARKER not in repr(result)
    b.create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing-role",
        "role-malformed",
        "settings-read",
        "resource",
        "entry",
        "mismatch",
        "read",
        "identity",
        "name",
        "created-identity",
        "created-name",
        "no-body",
    ],
)
async def test_role_failed_creation_stops_assignments(
    creation_boundary, failure, monkeypatch
):
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.workflows import create_lpar

    b = creation_boundary
    if failure == "role-malformed":
        b.resource["KvmCapable"] = MARKER
    elif failure == "settings-read":
        monkeypatch.setattr(
            "hmcpctl.operations.lpar.core.get_lpar_settings",
            AsyncMock(side_effect=HMCError(MARKER)),
        )
    elif failure == "resource":
        b.hmc.get_logical_partition.return_value["Resource"] = MARKER
    elif failure == "entry":
        b.hmc.get_logical_partition.return_value = None
    elif failure == "missing-role":
        b.resource.pop("KvmCapable")
    elif failure == "mismatch":
        b.resource["KvmCapable"] = "false"
    elif failure == "read":
        b.hmc.get_logical_partition.side_effect = HMCError(MARKER)
    elif failure == "identity":
        b.hmc.get_logical_partition.return_value["UUID"] = SYSTEM
    elif failure == "name":
        b.resource["PartitionName"] = MARKER
    elif failure == "created-identity":
        b.entry["UUID"] = MARKER
    elif failure == "created-name":
        b.entry["Resource"]["PartitionName"] = MARKER
    else:
        b.hmc.find_partition_by_name.side_effect = [None, HMCError(MARKER)]
    result = await create_lpar(
        b.hmc, SYSTEM, replace(b.creation, apply_profile=True), LparPcieAssignments()
    )
    assert result.resource_created and not result.workflow_completed
    assert (
        next(step for step in result.steps if step.step == "settings").status == "error"
    )
    b.assign.assert_not_awaited()
    b.apply.assert_not_awaited()
    assert MARKER not in repr(result)


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.asyncio
async def test_role_native_creation_error_is_uncertain_and_safe(
    creation_boundary, selected
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    b.create.side_effect = HMCError(MARKER)
    with pytest.raises(HMCError) as error:
        await create_and_stamp_lpar(
            b.hmc,
            SYSTEM,
            replace(b.creation, settings=LparSettings(powervm_mgmt_capable=selected)),
        )
    assert MARKER not in str(error.value)
    assert MARKER not in "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert "unknown" in str(error.value)
    b.create.assert_awaited_once()
    b.stamp.assert_not_awaited()


@pytest.mark.parametrize("domain", ["settings", "policy", "physical_io"])
@pytest.mark.asyncio
async def test_no_role_native_creation_error_chain_is_unchanged(
    creation_boundary, monkeypatch, domain
):
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.security_policy import SecurityPolicyCapabilities

    b = creation_boundary
    failure = HMCError(MARKER)
    b.create.side_effect = failure
    creation = replace(b.creation, settings=LparSettings(time_ref=False))
    if domain == "policy":
        creation = replace(creation, security_policy=LparSecurityPolicy(secure_boot=0))
        monkeypatch.setattr(
            core,
            "read_security_policy_capabilities",
            AsyncMock(
                return_value=SecurityPolicyCapabilities(
                    True, True, True, 0, 65536, False, (10, 3, 1060), 1060
                )
            ),
        )
    elif domain == "physical_io":
        creation = replace(creation, physical_io=LparPhysicalIO(console_slot="hmc"))
    with pytest.raises(HMCError) as caught:
        await core.create_and_stamp_lpar(b.hmc, SYSTEM, creation)
    if domain == "settings":
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure
        assert (
            f"Native {'policy' if domain == 'policy' else 'I/O'} creation outcome is unknown"
            in str(caught.value)
        )
    assert MARKER in "".join(traceback.format_exception(caught.value))
    b.create.assert_awaited_once()
    b.stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_role_physical_io_is_preserved(creation_boundary, monkeypatch):
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    b = creation_boundary
    io = LparPhysicalIO(console_slot="hmc")
    monkeypatch.setattr(
        core,
        "created_io_step",
        AsyncMock(return_value=WorkflowStep("physical_io", "ok")),
    )
    result = await core.create_and_stamp_lpar(
        b.hmc, SYSTEM, replace(b.creation, physical_io=io, apply_profile=True)
    )
    assert (
        result.settings_step.status
        == result.physical_io_step.status
        == result.apply_step.status
        == "ok"
    )
    assert b.create.await_args.kwargs["physical_io"] is io
    b.apply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,value",
    [
        ("powervm_mgmt_capable", False),
        ("powervm_mgmt_capable", True),
        ("kvm_capable", False),
    ],
)
async def test_creation_selected_false_and_management_need_no_companion(
    creation_boundary, name, value
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    b = creation_boundary
    b.resource[roles.ROLE_FIELDS[name]] = str(value).lower()
    b.resource.pop("PendingProcessorCompatibilityMode")
    settings = LparSettings(**{name: value})
    creation = replace(b.creation, settings=settings, resources=LparResources())
    result = await create_and_stamp_lpar(b.hmc, SYSTEM, creation)
    assert result.settings_step.status == "ok"
    assert getattr(result.settings_step.result, name) is value
    assert b.create.await_args.kwargs["settings"] is settings
    b.compat.assert_not_awaited()
    b.caps.assert_awaited_once_with(
        b.hmc,
        SYSTEM,
        need_kvm=name == "kvm_capable",
        need_management=name == "powervm_mgmt_capable",
    )


@pytest.mark.asyncio
async def test_controller_vnic_not_blanket_rejected(creation_boundary):
    from decimal import Decimal

    from hmcpctl.operations.lpar.assignments import LparPcieAssignments, VnicAssignment
    from hmcpctl.operations.lpar.workflows import create_lpar
    from hmcpctl.operations.virtualization.vnic import VnicBackingSelector

    b = creation_boundary
    b.caps.return_value = replace(CAPS, controller_mode=True)
    backing = VnicBackingSelector("vios", "1", "1", "0", Decimal(2))
    assignments = LparPcieAssignments(vnics=(VnicAssignment(backing, 0),))
    result = await create_lpar(b.hmc, SYSTEM, b.creation, assignments)
    assert result.workflow_completed
    b.prevalidate.assert_awaited_once_with(b.hmc, SYSTEM, assignments)
    b.assign.assert_awaited_once_with(b.hmc, SYSTEM, "owned", assignments)


@pytest.mark.asyncio
async def test_role_creation_verifies_reporting_before_apply(
    creation_boundary, monkeypatch
):
    from hmcpctl.documents.lpar import LparReportingSettings
    from hmcpctl.operations.lpar import core

    b = creation_boundary
    reporting = LparReportingSettings(work_group_id=0)
    read = AsyncMock(return_value=reporting)
    monkeypatch.setattr(core, "get_lpar_reporting_settings", read)
    result = await core.create_and_stamp_lpar(
        b.hmc,
        SYSTEM,
        replace(b.creation, reporting_settings=reporting, apply_profile=True),
    )
    assert result.reporting_step.status == result.apply_step.status == "ok"
    read.assert_awaited_once()
    b.apply.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"kvm": False},
        {"hmc_version": None},
        {"hmc_version": (10, 3, 1060, 0)},
        {"firmware": None},
        {"firmware": (1060, 9)},
        {"power11": True},
    ],
)
def test_creation_capability_prerequisites(changes):
    from hmcpctl.operations.lpar.core import LparCreation

    creation = LparCreation(
        "owned",
        "AIX/Linux",
        LparResources(dedicated=True, desired_procs=2),
        settings=LparSettings(kvm_capable=True),
    )
    with pytest.raises(ValueError):
        roles.admit_role_creation(creation, replace(CAPS, **changes))
    roles.admit_role_creation(creation, CAPS)
    roles.admit_role_creation(creation, replace(CAPS, power11=True, firmware=(1110, 0)))
    with pytest.raises(ValueError):
        roles.admit_role_creation(replace(creation, partition_type="OS400"), CAPS)
    with pytest.raises(ValueError):
        roles.admit_role_creation(
            replace(creation, partition_type="Virtual IO Server"), CAPS
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize(
    "domain", ["reporting", "vtpm", "physical_io", "apply_profile"]
)
async def test_mixed_creation_remote_errors_are_safe_for_roles(
    creation_boundary, monkeypatch, selected, domain
):
    from hmcpctl.documents.lpar import LparReportingSettings
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.operations.lpar import core, physical_io
    from hmcpctl.operations.lpar.vtpm import VtpmCapabilities

    b = creation_boundary
    changes = {"apply_profile": True}
    if not selected:
        changes["settings"] = LparSettings(time_ref=False)
    if domain == "reporting":
        changes["reporting_settings"] = LparReportingSettings(work_group_id=0)
        monkeypatch.setattr(
            core, "get_lpar_reporting_settings", AsyncMock(side_effect=HMCError(MARKER))
        )
    elif domain == "vtpm":
        changes["vtpm_settings"] = LparVtpmSettings(False)
        monkeypatch.setattr(
            core,
            "read_vtpm_capabilities",
            AsyncMock(return_value=VtpmCapabilities(("1.2",), (), False)),
        )
        monkeypatch.setattr(
            core, "get_lpar_vtpm_state", AsyncMock(side_effect=HMCError(MARKER))
        )
    elif domain == "physical_io":
        changes["physical_io"] = LparPhysicalIO(console_slot="hmc")
        monkeypatch.setattr(core, "created_io_step", physical_io.created_io_step)
        monkeypatch.setattr(
            physical_io,
            "read_partition_profile",
            AsyncMock(side_effect=HMCError(MARKER)),
        )
    else:
        b.apply.side_effect = HMCCLIError(MARKER)
    result = await core.create_and_stamp_lpar(
        b.hmc, SYSTEM, replace(b.creation, **changes)
    )
    step = getattr(
        result,
        {
            "reporting": "reporting_step",
            "vtpm": "vtpm_step",
            "physical_io": "physical_io_step",
            "apply_profile": "apply_step",
        }[domain],
    )
    assert result.resource_created and result.ownership_stamped
    assert step.status == "error"
    assert (MARKER in repr(result)) is (not selected)
    if selected:
        assert "inspect" in step.result.lower()
        assert result.lpar is b.entry
        assert result.settings_step.result.kvm_capable is True
    if domain != "apply_profile" and (selected or domain == "physical_io"):
        b.apply.assert_not_awaited()


@pytest.mark.parametrize("stage", ["system", "console"])
@pytest.mark.parametrize(
    "error_type", [HMCError, HMCTransportError, HMCTLSFingerprintError, HMCCLIError]
)
@pytest.mark.asyncio
async def test_role_preflight_error_preserves_type_and_status(
    source_boundary, stage, error_type
):
    b = source_boundary
    original = error_type("Remote evidence failed", 503, f"<Message>{MARKER}</Message>")
    getattr(
        b.hmc, "get_managed_system" if stage == "system" else "get_console_info"
    ).side_effect = original
    with pytest.raises(error_type) as caught:
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=True, need_management=False
        )
    assert type(caught.value) is error_type
    assert caught.value.status_code == 503
    assert caught.value.body is None
    assert MARKER not in str(caught.value)
    assert "Linux role preflight" in str(caught.value)
    assert "no changes were submitted" in str(caught.value)
    assert MARKER in str(original)
    assert original.body is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [False, None])
@pytest.mark.parametrize("selected", [False, True])
async def test_requested_role_apply_withheld_stops_workflow(
    creation_boundary, stamped, selected
):
    """Consume unit-boundary stamp results; None is not a native reachability claim."""
    from decimal import Decimal

    from hmcpctl.operations.lpar.assignments import LparPcieAssignments, VnicAssignment
    from hmcpctl.operations.lpar.workflows import create_lpar
    from hmcpctl.operations.virtualization.vnic import VnicBackingSelector

    b = creation_boundary
    b.stamp.return_value = (stamped, ("ownership stamp unavailable",))
    b.resource["PowerVMManagementCapable"] = str(selected).lower()
    creation = replace(
        b.creation,
        settings=LparSettings(powervm_mgmt_capable=selected),
        apply_profile=True,
    )
    backing = VnicBackingSelector("vios", "1", "1", "0", Decimal(2))
    assignments = LparPcieAssignments(vnics=(VnicAssignment(backing, 0),))
    result = await create_lpar(b.hmc, SYSTEM, creation, assignments)
    assert result.resource_created and not result.workflow_completed
    assert result.ownership_stamped is stamped
    assert result.warnings
    assert (
        next(step for step in result.steps if step.step == "apply_profile").status
        == "skipped"
    )
    b.apply.assert_not_awaited()
    b.assign.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [False, None])
@pytest.mark.parametrize("apply", [False, None])
@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize("selected", [False, True])
async def test_intentional_role_no_apply_preserves_completion(
    creation_boundary, stamped, apply, controller, selected
):
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.workflows import create_lpar

    b = creation_boundary
    b.stamp.return_value = (stamped, ("ownership stamp unavailable",))
    b.caps.return_value = replace(CAPS, controller_mode=controller)
    b.resource["PowerVMManagementCapable"] = str(selected).lower()
    creation = replace(
        b.creation,
        settings=LparSettings(powervm_mgmt_capable=selected),
        apply_profile=apply,
    )
    result = await create_lpar(b.hmc, SYSTEM, creation, LparPcieAssignments())
    assert result.resource_created and result.workflow_completed
    assert result.ownership_stamped is stamped
    b.apply.assert_not_awaited()
    b.assign.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [False, None])
@pytest.mark.parametrize("native", [False, True])
async def test_no_role_best_effort_stamp_behavior_is_unchanged(
    creation_boundary, stamped, native
):
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.workflows import create_lpar

    b = creation_boundary
    b.stamp.return_value = (stamped, ("ownership stamp unavailable",))
    b.hmc.create_logical_partition.return_value = b.entry
    settings = LparSettings(time_ref=False) if native else LparSettings()
    creation = replace(b.creation, settings=settings, apply_profile=True)
    result = await create_lpar(b.hmc, SYSTEM, creation, LparPcieAssignments())
    assert result.resource_created and result.workflow_completed
    if native:
        b.apply.assert_awaited_once()
    else:
        b.apply.assert_not_awaited()
    b.assign.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [HMCError, HMCTransportError, HMCTLSFingerprintError, HMCCLIError]
)
@pytest.mark.parametrize("stage", ["partition", "baseline"])
async def test_configuration_role_preflight_error_type(
    role_boundary, error_type, stage
):
    b = role_boundary
    failure = error_type("Remote evidence failed", 503, f"<Message>{MARKER}</Message>")
    if stage == "partition":
        b.hmc.get_logical_partition.side_effect = failure
    else:
        b.read.side_effect = failure
    with pytest.raises(error_type) as caught:
        await operation.configure_lpar_settings(
            b.hmc,
            "owned",
            LparSettings(powervm_mgmt_capable=False),
            system_name_or_uuid=SYSTEM,
        )
    assert type(caught.value) is error_type and caught.value.status_code == 503
    assert (
        caught.value.body is None
        and caught.value.__cause__ is None
        and caught.value.__suppress_context__
    )
    assert "no changes were submitted" in str(caught.value) and MARKER not in str(
        caught.value
    )
    assert MARKER in str(failure)
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [HMCError, HMCTransportError, HMCTLSFingerprintError, HMCCLIError]
)
async def test_no_role_preflight_error_is_unchanged(role_boundary, error_type):
    b = role_boundary
    failure = error_type(MARKER, 503, f"<Message>{MARKER}</Message>")
    b.read.side_effect = failure
    with pytest.raises(error_type) as caught:
        await operation.configure_lpar_settings(
            b.hmc, "owned", LparSettings(time_ref=False), system_name_or_uuid=SYSTEM
        )
    assert caught.value is failure and MARKER in str(caught.value)
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["system", "partition", "authorization", "containment", "capabilities"]
)
async def test_role_preflight_sibling_read_diagnostics(role_boundary, stage):
    b = role_boundary
    failure = HMCError(MARKER, 503, f"<Message>{MARKER}</Message>")
    boundary = {
        "system": b.system,
        "partition": b.partition,
        "authorization": b.authorize,
        "containment": b.partition,
        "capabilities": b.caps,
    }[stage]
    boundary.side_effect = [PARTITION, failure] if stage == "containment" else failure
    with pytest.raises(HMCError) as caught:
        await operation.configure_lpar_settings(
            b.hmc,
            "owned",
            LparSettings(powervm_mgmt_capable=False),
            system_name_or_uuid=SYSTEM,
        )
    assert caught.value.body is None and caught.value.status_code == 503
    assert MARKER not in str(caught.value) and "no changes were submitted" in str(
        caught.value
    )
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "null\n",
        "0\n",
        "",
        "1\n1\n",
        "true",
        "is_controller=1",
        "1,1",
        "01",
        "\n1\n",
        "1\n\n",
        " 1",
        "1 ",
    ],
)
async def test_management_requires_positive_native_controller(source_boundary, raw):
    b = source_boundary
    b.status.return_value = raw
    with pytest.raises(ValueError, match="controller"):
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=False, need_management=True
        )
    b.hmc.get_console_info.assert_not_awaited()
    b.modes.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_controller_and_classic_grammar_are_separate(source_boundary):
    b = source_boundary
    b.system["SystemName"] = "selected system; quoted"
    b.status.return_value = "1\n"
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=False, need_management=True
    )
    assert caps.controller_mode
    b.status.assert_awaited_once_with(
        CONFIG, "lscomgmt -m 'selected system; quoted' -F is_controller"
    )
    b.status.return_value = "null\n"
    caps = await roles.read_role_capabilities(
        b.hmc, SYSTEM, need_kvm=True, need_management=False
    )
    assert not caps.controller_mode and caps.kvm
    b.status.return_value = "0\n"
    with pytest.raises(ValueError, match="controller"):
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=True, need_management=False
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [HMCError, HMCTransportError, HMCTLSFingerprintError, HMCCLIError]
)
async def test_native_controller_error_is_private(source_boundary, error_type):
    b = source_boundary
    b.status.side_effect = error_type(MARKER, status_code=503, body=MARKER)
    with pytest.raises(error_type) as caught:
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=False, need_management=True
        )
    assert type(caught.value) is error_type
    assert caught.value.status_code == 503
    assert caught.value.body is None
    assert "no changes were submitted" in str(caught.value)
    assert MARKER not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "IsClassicHMCManagement",
        "IsHMCPowerVMManagementController",
        "IsPowerVMManagementController",
    ],
)
@pytest.mark.parametrize("bad", [None, "unknown", "true"])
async def test_native_null_needs_known_classic_tuple(source_boundary, field, bad):
    b = source_boundary
    if field == "IsClassicHMCManagement" and bad == "true":
        bad = "false"
    b.system[field] = bad
    with pytest.raises(ValueError, match="controller"):
        await roles.read_role_capabilities(
            b.hmc, SYSTEM, need_kvm=True, need_management=False
        )
    b.hmc.get_console_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,controller", [("null\n", False), ("1\n", True)])
async def test_shared_creation_authority_has_no_role_prerequisites(
    source_boundary, raw, controller
):
    b = source_boundary
    b.status.return_value = raw
    b.system.pop("AssociatedSystemCapabilities", None)
    resource, observed = await roles.read_creation_authority(
        b.hmc, SYSTEM, need_management=False, context="Affinity group"
    )
    assert resource is b.system and observed is controller
    b.status.assert_awaited_once()
    b.hmc.get_console_info.assert_not_awaited()
    b.modes.assert_not_awaited()
