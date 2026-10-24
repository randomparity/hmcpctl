"""Selected-system Linux role admission and one-shot settings outcomes."""

import json
import shlex
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import configuration as operation
from hmcpctl.operations.lpar import linux_roles as roles
from hmcpctl.ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    create_lpar_via_cli,
    get_lpar_settings,
    set_lpar_settings,
)
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import parse_feed

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
            "Maintenance": "1",
        }
    }
    hmc = SimpleNamespace(
        config=CONFIG,
        get_managed_system=AsyncMock(return_value={"Resource": system}),
        get_console_info=AsyncMock(return_value={"Resource": console}),
    )
    modes = AsyncMock(return_value=["POWER9", "POWER10"])
    monkeypatch.setattr(roles, "get_proc_compat_modes", modes)
    return SimpleNamespace(hmc=hmc, system=system, console=console, modes=modes)


@pytest.mark.asyncio
async def test_capability_source_decoding(source_boundary):
    b = source_boundary
    assert await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True) == CAPS
    b.modes.return_value = ["POWER10", "POWER11"]
    b.system["SystemFirmware"] = {"text": "FW1110_FW1110.00 (1)"}
    assert (await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True)).power11
    for field in ("Version", "Release", "ServicePackName", "Maintenance"):
        old = b.console["VersionInfo"].pop(field)
        assert (
            await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True)
        ).hmc_version is None
        b.console["VersionInfo"][field] = old
    for bad in (None, "bad", "-1", "1.0", True):
        b.console["VersionInfo"]["Maintenance"] = bad
        assert (
            await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True)
        ).hmc_version is None
    b.system["IsClassicHMCManagement"] = "false"
    b.system["IsHMCPowerVMManagementController"] = "true"
    caps = await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=False)
    assert caps.controller_mode and caps.hmc_version is None
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
            await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=False)


@pytest.mark.asyncio
async def test_captured_source_shape(source_boundary):
    b = source_boundary
    b.system.clear()
    b.system.update(captured("rest-ms-entry-mtms-pair.json")["Resource"])
    caps = await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True)
    assert not caps.kvm and not caps.controller_mode and caps.firmware == (950, 0)
    resource = captured("rest-lpar-entry.json")["Resource"]
    evidence = roles.with_role_evidence(BASE, resource, LparSettings(kvm_capable=False))
    assert evidence.kvm_capable is False and evidence.powervm_mgmt_capable is False
    with pytest.raises(ValueError):
        roles.admit_role_update(LparSettings(kvm_capable=True), caps, resource)
    b.hmc.get_console_info.reset_mock()
    b.modes.reset_mock()
    await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=False)
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
    assert b.caps.await_args.kwargs == {"need_kvm": name == "kvm_capable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["error", "name", "unsupported", "firmware"])
async def test_false_kvm_does_not_require_generation_evidence(source_boundary, unknown):
    b = source_boundary
    if unknown == "error":
        b.modes.side_effect = HMCError(MARKER)
    elif unknown == "name":
        b.system.pop("SystemName")
    elif unknown == "unsupported":
        b.modes.return_value = ["POWER9"]
    else:
        b.system["SystemFirmware"] = "bad"
    caps = await roles.read_role_capabilities(b.hmc, SYSTEM, need_kvm=True)
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
