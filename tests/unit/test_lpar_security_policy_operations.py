"""Selected-system and partition evidence through the real HMC XML parser."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.security_policy import (
    admit_security_policy,
    parse_security_policy,
    read_security_policy_capabilities,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live"
SYSTEM = "0000000e-abcd-4ef0-8abc-00000000000e"
PARTITION = "00000012-abcd-4ef0-8abc-000000000012"


def capture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["body"]


def changed_xml(body, replacements):
    root = ET.fromstring(body)
    for name, value in replacements.items():
        element = next((e for e in root.iter() if e.tag.split("}")[-1] == name), None)
        if element is not None:
            if value is None:
                parent = next(p for p in root.iter() if element in list(p))
                parent.remove(element)
            else:
                element.text = value
        elif value is not None:
            resource = next(
                e
                for e in root.iter()
                if e.tag.split("}")[-1] in {"LogicalPartition", "VirtualIOServer"}
            )
            ET.SubElement(resource, name).text = value
    return ET.tostring(root, encoding="unicode")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,kind,environment,state",
    [
        ("rest-lpar-entry", "LogicalPartition", "AIX/Linux", "not activated"),
        ("rest-vios-entry", "VirtualIOServer", "Virtual IO Server", "running"),
    ],
)
async def test_real_parser_capability_state_and_pending_current(
    mock_hmc, name, kind, environment, state
):
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=capture("rest-ms-entry-mtms-pair"))
    )
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(
            200,
            text="<ManagementConsole><VersionInfo><Version>10</Version><Release>3</Release><ServicePackName>1060</ServicePackName></VersionInfo></ManagementConsole>",
        )
    )
    body = changed_xml(
        capture(name), {"PendingSecureBoot": "2", "CurrentSecureBoot": "0"}
    )
    mock_hmc.get(f"/rest/api/uom/{kind}/{PARTITION}").mock(
        return_value=httpx.Response(200, text=body)
    )
    async with HMCClient(make_config()) as hmc:
        caps = await read_security_policy_capabilities(hmc, SYSTEM)
        entry = await hmc.get_uom(kind, PARTITION)
    assert caps.secure_boot is True and caps.keystore is True
    assert caps.dynamic_key is False and caps.power11 is False
    assert (
        caps.minimum_kbytes,
        caps.maximum_kbytes,
        caps.hmc_version,
        caps.firmware_level,
    ) == (4, 64, (10, 3, 1060), 950)
    policy = parse_security_policy(entry["Resource"])
    assert (policy.environment, policy.state) == (environment, state)
    assert policy.settings.secure_boot == 2 and policy.current_secure_boot == 0
    assert policy.settings.keystore_signed_updates is None
    assert policy.settings.linux_dynamic_key_secure_boot is None
    assert policy.settings.keystore_signed_updates_without_verification is None
    admit_security_policy(
        LparSecurityPolicy(secure_boot=1), caps, environment=environment, before=policy
    )
    with pytest.raises(ValueError, match="capability"):
        admit_security_policy(
            LparSecurityPolicy(keystore_signed_updates=False),
            caps,
            environment=environment,
            before=policy,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PendingSecureBoot", "10"),
        ("PendingSecureBoot", "01"),
        ("CurrentSecureBoot", "false"),
        ("KeyStoreSize", "-1"),
        ("KeyStoreSize", "1.0"),
        ("KeyStoreSignedUpdatesEnabled", "False"),
        ("LinuxDynamicKeySecureBootEnabled", "1"),
        ("KeyStoreSignedUpdatesWithoutVerificationAllowed", "sensitive-value"),
        ("PartitionState", None),
        ("PartitionType", None),
    ],
)
async def test_present_malformed_policy_refuses_without_reflection(
    mock_hmc, field, value
):
    body = changed_xml(capture("rest-lpar-entry"), {field: value})
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{PARTITION}").mock(
        return_value=httpx.Response(200, text=body)
    )
    async with HMCClient(make_config()) as hmc:
        entry = await hmc.get_logical_partition(PARTITION)
    with pytest.raises(ValueError) as error:
        parse_security_policy(entry["Resource"])
    assert "sensitive-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionSecureBootCapable", None),
        ("PartitionSecureBootCapable", "True"),
        ("PartitionSecureBootCapable", "1"),
        ("PartitionKeyStoreCapable", "false"),
        ("MinimumKeyStoreSize", "bad"),
        ("MaximumkeyStoreSize", "0"),
    ],
)
async def test_missing_or_malformed_advertisement_refuses(mock_hmc, field, value):
    body = changed_xml(capture("rest-ms-entry-mtms-pair"), {field: value})
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=body)
    )
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(
            200,
            text="<ManagementConsole><VersionInfo><Version>10</Version><Release>3</Release><ServicePackName>1060</ServicePackName></VersionInfo></ManagementConsole>",
        )
    )
    async with HMCClient(make_config()) as hmc:
        caps = await read_security_policy_capabilities(hmc, SYSTEM)
    settings = (
        LparSecurityPolicy(secure_boot=1)
        if "SecureBoot" in field
        else LparSecurityPolicy(keystore_kbytes=4)
    )
    with pytest.raises(ValueError):
        admit_security_policy(settings, caps, environment="AIX/Linux")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "need,modes,expected",
    [
        (False, ["POWER11"], False),
        (True, ["POWER10"], False),
        (True, ["POWER11"], True),
        (True, ["power11"], False),
    ],
)
async def test_processor_modes_only_when_needed(
    mock_hmc, monkeypatch, need, modes, expected
):
    reader = AsyncMock(return_value=modes)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.security_policy.get_proc_compat_modes", reader
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=capture("rest-ms-entry-mtms-pair"))
    )
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text="<ManagementConsole/>")
    )
    async with HMCClient(make_config()) as hmc:
        caps = await read_security_policy_capabilities(hmc, SYSTEM, need_power11=need)
    assert caps.power11 is expected
    assert reader.await_count == int(need)
    if need:
        assert reader.await_args.args[1] == "sys-R1"


def test_absent_policy_stays_unknown():
    resource = {"PartitionType": "AIX/Linux", "PartitionState": "running"}
    result = parse_security_policy(resource)
    assert result.settings == LparSecurityPolicy()
    assert result.current_secure_boot is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "false"])
async def test_real_parser_dynamic_boolean_leaves(mock_hmc, value):
    body = changed_xml(
        capture("rest-lpar-entry"),
        {
            "KeyStoreSignedUpdatesEnabled": value,
            "LinuxDynamicKeySecureBootEnabled": value,
            "KeyStoreSignedUpdatesWithoutVerificationAllowed": value,
        },
    )
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{PARTITION}").mock(
        return_value=httpx.Response(200, text=body)
    )
    async with HMCClient(make_config()) as hmc:
        entry = await hmc.get_logical_partition(PARTITION)
    settings = parse_security_policy(entry["Resource"]).settings
    assert settings.keystore_signed_updates is (value == "true")
    assert settings.linux_dynamic_key_secure_boot is (value == "true")
    assert settings.keystore_signed_updates_without_verification is (value == "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "firmware,expected",
    [
        ("RK1110_FW1110.10 (100)", 1110),
        ("RB1120_fw1120.10 (198)", 1120),
        ("FW1110", None),
        ("untrusted-value", None),
        (None, None),
    ],
)
async def test_firmware_is_conservative_selected_system_evidence(
    mock_hmc, firmware, expected
):
    body = changed_xml(capture("rest-ms-entry-mtms-pair"), {"SystemFirmware": firmware})
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=body)
    )
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text="<ManagementConsole/>")
    )
    async with HMCClient(make_config()) as hmc:
        caps = await read_security_policy_capabilities(hmc, SYSTEM)
    assert caps.firmware_level == expected
    assert caps.hmc_version is None


@pytest.mark.asyncio
async def test_unassociated_flags_cannot_admit_selected_system(mock_hmc):
    root = ET.fromstring(capture("rest-ms-entry-mtms-pair"))
    resource = next(e for e in root.iter() if e.tag.split("}")[-1] == "ManagedSystem")
    caps = next(
        e for e in root.iter() if e.tag.split("}")[-1] == "AssociatedSystemCapabilities"
    )
    secure_boot = next(
        e for e in caps if e.tag.split("}")[-1] == "PartitionSecureBootCapable"
    )
    caps.remove(secure_boot)
    resource.append(secure_boot)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=ET.tostring(root, encoding="unicode"))
    )
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text="<ManagementConsole/>")
    )
    async with HMCClient(make_config()) as hmc:
        evidence = await read_security_policy_capabilities(hmc, SYSTEM)
    assert evidence.secure_boot is False
    with pytest.raises(ValueError, match="capability"):
        admit_security_policy(
            LparSecurityPolicy(secure_boot=0), evidence, environment="AIX/Linux"
        )


@pytest.fixture
def policy_target(monkeypatch):
    from hmcpctl.config import HMCConfig

    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    resource = {
        "PartitionName": "part-R1",
        "PartitionType": "AIX/Linux",
        "PartitionState": "not activated",
        "PendingSecureBoot": "0",
        "CurrentSecureBoot": "0",
        "KeyStoreSize": "4",
        "KeyStoreSignedUpdatesEnabled": "false",
    }
    entry = {"UUID": PARTITION, "Resource": resource}
    hmc.list_logical_partitions.return_value = [entry]
    hmc.list_vios.return_value = [entry]
    hmc.list_system_summaries.return_value = [{"UUID": SYSTEM, "SystemName": "sys-R1"}]
    hmc.get_logical_partition.return_value = entry
    hmc.get_uom.return_value = entry
    hmc.find_partition_by_name.return_value = entry
    hmc.find_vios_by_name.return_value = entry
    hmc.get_managed_system.return_value = {
        "Resource": {
            "SystemName": "sys-R1",
            "SystemFirmware": "VL950_FW950.10 (100)",
            "AssociatedSystemCapabilities": {
                "PartitionSecureBootCapable": "true",
                "PartitionKeyStoreCapable": "true",
                "PartitionDynamicKeySecureBootCapable": "true",
            },
            "MinimumKeyStoreSize": "4",
            "MaximumkeyStoreSize": "64",
        }
    }
    hmc.get_console_info.return_value = {
        "Resource": {
            "VersionInfo": {
                "Version": "10",
                "Release": "3",
                "ServicePackName": "1060",
            }
        }
    }
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        AsyncMock(return_value=""),
    )
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", command)
    return hmc, entry, command


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize("failure", ["foreign", "ownership", "kind"])
async def test_lpar_and_vios_guards_precede_write(
    policy_target, monkeypatch, vios, failure
):
    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, entry, command = policy_target
    entry["Resource"]["PartitionType"] = "Virtual IO Server" if vios else "AIX/Linux"
    if failure == "foreign":
        hmc.list_vios.return_value = []
        hmc.list_logical_partitions.return_value = []
    elif failure == "ownership":
        monkeypatch.setattr(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            AsyncMock(return_value="[hmcpctl owner:other created:2026-10-23]"),
        )
    else:
        entry["Resource"]["PartitionType"] = (
            "AIX/Linux" if vios else "Virtual IO Server"
        )
    with pytest.raises((ValueError, PermissionError)):
        await configure_lpar_security_policy(
            hmc,
            PARTITION,
            LparSecurityPolicy(secure_boot=1),
            system_name_or_uuid=SYSTEM,
            vios=vios,
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_record_escaping_and_false_zero(policy_target):
    import csv
    import shlex

    from hmcpctl.ssh.lpar import set_lpar_security_policy

    hmc, _, command = policy_target
    await set_lpar_security_policy(
        hmc.config,
        "sys;'x",
        "part;'x",
        LparSecurityPolicy(secure_boot=0, keystore_signed_updates=False),
    )
    args = shlex.split(command.await_args.args[1])
    assert args[:5] == ["chsyscfg", "-r", "lpar", "-m", "sys;'x"]
    assert args[5] == "-i"
    assert next(csv.reader([args[6]])) == [
        "name=part;'x",
        "secure_boot=0",
        "keystore_signed_updates=0",
    ]
    command.assert_awaited_once()
    with pytest.raises(ValueError):
        await set_lpar_security_policy(hmc.config, "sys", "part", LparSecurityPolicy())
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize("drift", [None, "KeyStoreSize", "CurrentSecureBoot"])
async def test_requested_and_preserved_readback(policy_target, vios, drift):
    from copy import deepcopy

    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, before, command = policy_target
    before["Resource"]["PartitionType"] = "Virtual IO Server" if vios else "AIX/Linux"
    after = deepcopy(before)
    after["Resource"]["PendingSecureBoot"] = "1"
    if drift:
        after["Resource"][drift] = "8"

    async def write(*args):
        hmc.get_logical_partition.return_value = after
        hmc.get_uom.return_value = after

    command.side_effect = write
    result = await configure_lpar_security_policy(
        hmc,
        PARTITION,
        LparSecurityPolicy(secure_boot=1),
        system_name_or_uuid=SYSTEM,
        vios=vios,
    )
    assert result.status == ("mismatch" if drift else "verified")
    assert result.changed_fields == ("secure_boot",)
    assert result.observed.settings.linux_dynamic_key_secure_boot is None
    assert result.observed.current_secure_boot == (
        8 if drift == "CurrentSecureBoot" else 0
    )
    command.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_is_not_current(policy_target):
    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, entry, command = policy_target
    entry["Resource"]["PendingSecureBoot"] = "1"
    result = await configure_lpar_security_policy(
        hmc, PARTITION, LparSecurityPolicy(secure_boot=1), system_name_or_uuid=SYSTEM
    )
    assert result.status == "verified"
    assert any("restart" in warning.lower() for warning in result.warnings)
    assert result.changed_fields == ()
    command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,settings",
    [
        ("PendingSecureBoot", LparSecurityPolicy(secure_boot=0)),
        (
            "KeyStoreSignedUpdatesEnabled",
            LparSecurityPolicy(keystore_signed_updates=False),
        ),
    ],
)
async def test_missing_requested_field_cannot_verify(policy_target, field, settings):
    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, entry, command = policy_target

    async def write(*args):
        entry["Resource"].pop(field)

    command.side_effect = write
    result = await configure_lpar_security_policy(
        hmc, PARTITION, settings, system_name_or_uuid=SYSTEM
    )
    assert result.status == "mismatch"
    assert getattr(result.observed.settings, settings.pairs()[0][0]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "read"])
async def test_submission_never_replayed(policy_target, phase):
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, _, command = policy_target
    reads_at_write = []

    async def write(*args):
        reads_at_write.append(hmc.get_logical_partition.await_count)
        if phase == "write":
            raise HMCError("sensitive-host secret-key")
        hmc.get_logical_partition.side_effect = HMCError("sensitive-host secret-key")

    command.side_effect = write
    result = await configure_lpar_security_policy(
        hmc, PARTITION, LparSecurityPolicy(secure_boot=1), system_name_or_uuid=SYSTEM
    )
    assert result.status == (
        "write-outcome-unknown" if phase == "write" else "readback-unavailable"
    )
    command.assert_awaited_once()
    assert hmc.get_logical_partition.await_count == reads_at_write[0] + (
        phase == "read"
    )
    assert result.observed is None
    assert "sensitive-host" not in repr(result) and "secret-key" not in repr(result)


@pytest.mark.asyncio
async def test_sensitive_transport_errors_are_bounded(policy_target):
    from hmcpctl.operations.lpar.security_policy import read_lpar_security_policy

    hmc, entry, command = policy_target
    entry["Resource"]["PendingSecureBoot"] = "sensitive-host secret-key"
    with pytest.raises(ValueError) as error:
        await read_lpar_security_policy(hmc, PARTITION, system_name_or_uuid=SYSTEM)
    assert "sensitive-host" not in str(error.value)
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize(
    "failure", ["uuid", "name", "retarget", "system", "capability"]
)
async def test_identity_and_admission_refresh_precede_write(
    policy_target, vios, failure
):
    from copy import deepcopy

    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, entry, command = policy_target
    entry["Resource"]["PartitionType"] = "Virtual IO Server" if vios else "AIX/Linux"
    bad = deepcopy(entry)
    if failure == "uuid":
        bad["UUID"] = SYSTEM
    elif failure == "name":
        bad["Resource"]["PartitionName"] = "replacement"
    elif failure == "retarget":
        hmc.find_partition_by_name.return_value = {"UUID": SYSTEM}
        hmc.find_vios_by_name.return_value = {"UUID": SYSTEM}
    elif failure == "system":
        hmc.get_managed_system.return_value["Resource"]["SystemName"] = "replacement"
    else:
        hmc.get_managed_system.return_value["Resource"]["AssociatedSystemCapabilities"][
            "PartitionSecureBootCapable"
        ] = "false"
    if vios:
        hmc.get_uom.return_value = bad
    else:
        hmc.get_logical_partition.side_effect = [entry, bad]
    with pytest.raises(ValueError):
        await configure_lpar_security_policy(
            hmc,
            PARTITION,
            LparSecurityPolicy(secure_boot=1),
            system_name_or_uuid=SYSTEM,
            vios=vios,
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"vios": 1}, {"ownership_override": "false"}])
async def test_selector_options_are_strict(policy_target, options):
    from hmcpctl.operations.lpar.security_policy import configure_lpar_security_policy

    hmc, _, command = policy_target
    with pytest.raises(TypeError):
        await configure_lpar_security_policy(
            hmc,
            PARTITION,
            LparSecurityPolicy(secure_boot=1),
            system_name_or_uuid=SYSTEM,
            **options,
        )
    command.assert_not_awaited()
    hmc.list_logical_partitions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vios", [False, True])
async def test_contained_rest_read_preserves_unknowns(policy_target, vios):
    from hmcpctl.operations.lpar.security_policy import read_lpar_security_policy

    hmc, entry, command = policy_target
    entry["Resource"]["PartitionType"] = "Virtual IO Server" if vios else "AIX/Linux"
    result = await read_lpar_security_policy(
        hmc, PARTITION, system_name_or_uuid=SYSTEM, vios=vios
    )
    assert result.settings.secure_boot == 0
    assert result.settings.linux_dynamic_key_secure_boot is None
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("delimiter", [",", '"', "=", "\n"])
async def test_fixed_native_record_refuses_delimiters(policy_target, delimiter):
    from hmcpctl.errors import HMCError
    from hmcpctl.ssh.lpar import set_lpar_security_policy

    hmc, _, command = policy_target
    with pytest.raises(HMCError):
        await set_lpar_security_policy(
            hmc.config,
            "sys",
            f"part{delimiter}extra",
            LparSecurityPolicy(secure_boot=0),
        )
    command.assert_not_awaited()


@pytest.fixture
def creation_target(policy_target, monkeypatch):
    from hmcpctl.operations.lpar import core

    hmc, entry, command = policy_target
    hmc.find_partition_by_name.side_effect = [None, entry]
    entry["Resource"]["PendingSecureBoot"] = "0"
    monkeypatch.setattr(
        core, "resolve_system_cli_name", AsyncMock(return_value="sys-R1")
    )
    monkeypatch.setattr(
        core, "stamp_created_lpar_ownership", AsyncMock(return_value=(True, ()))
    )
    monkeypatch.setattr(core, "require_memory_within_system", AsyncMock())
    return hmc, entry, command


@pytest.mark.asyncio
async def test_creation_policy_composes_once(creation_target, monkeypatch):
    import shlex

    from hmcpctl.documents.lpar import (
        LparReportingSettings,
        LparResources,
        LparSettings,
    )
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.ssh.profiles import ProfileIoSlot

    hmc, _entry, command = creation_target
    monkeypatch.setattr(core, "admit_creation_io", AsyncMock())
    monkeypatch.setattr(
        core,
        "created_io_step",
        AsyncMock(return_value=WorkflowStep("physical_io", "ok")),
    )
    for name in (
        "_created_settings_step",
        "_created_reporting_step",
        "_created_vtpm_step",
    ):
        monkeypatch.setattr(
            core, name, AsyncMock(return_value=WorkflowStep(name, "ok"))
        )
    monkeypatch.setattr(core, "read_vtpm_capabilities", AsyncMock())
    monkeypatch.setattr(core, "admit_vtpm", lambda *a: None)
    creation = core.LparCreation(
        "part-R1",
        "AIX/Linux",
        LparResources(desired_memory=1024),
        physical_io=LparPhysicalIO(io_slots=(ProfileIoSlot("21010010", None, False),)),
        partition_id=7,
        max_virtual_slots=40,
        settings=LparSettings(time_ref=True),
        reporting_settings=LparReportingSettings(work_group_id=0),
        vtpm_settings=LparVtpmSettings(False),
        security_policy=LparSecurityPolicy(
            secure_boot=0, keystore_signed_updates=False
        ),
    )
    result = await core.create_and_stamp_lpar(hmc, SYSTEM, creation)
    command.assert_awaited_once()
    record = shlex.split(command.await_args.args[1])[-1]
    for field in (
        "io_slots=21010010//0",
        "lpar_id=7",
        "max_virtual_slots=40",
        "time_ref=1",
        "work_group_id=0",
        "vtpm_enabled=0",
        "secure_boot=0",
        "keystore_signed_updates=0",
    ):
        assert record.count(field) == 1
    assert result.security_policy_step.status == "ok"
    hmc.create_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_lpar_policy_cpu_only_default_capacity(creation_target, monkeypatch):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.memory_bound import require_memory_within_system

    hmc, _, command = creation_target
    hmc.list_system_summaries.return_value[0]["State"] = "operating"
    hmc.get_managed_system.return_value["Resource"][
        "AssociatedSystemMemoryConfiguration"
    ] = {"ConfigurableSystemMemory": "2048"}
    monkeypatch.setattr(
        core, "require_memory_within_system", require_memory_within_system
    )
    with pytest.raises(ValueError, match="4096.*2048"):
        await core.create_and_stamp_lpar(
            hmc,
            SYSTEM,
            core.LparCreation(
                "part-R1",
                "AIX/Linux",
                LparResources(desired_procs=0.1),
                security_policy=LparSecurityPolicy(secure_boot=0),
            ),
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing", "wrong", "name", "read-uuid", "read-failure", "mismatch"]
)
async def test_created_policy_wrong_uuid_stops_followups(
    creation_target, monkeypatch, failure
):
    from copy import deepcopy

    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core

    hmc, entry, command = creation_target
    if failure == "missing":
        entry.pop("UUID")
    elif failure == "wrong":
        entry["UUID"] = "invalid"
    elif failure == "name":
        entry["Resource"]["PartitionName"] = "replacement"
    elif failure == "read-uuid":
        wrong = deepcopy(entry)
        wrong["UUID"] = SYSTEM
        hmc.get_logical_partition.return_value = wrong
    elif failure == "read-failure":
        hmc.get_logical_partition.side_effect = HMCError("secret-host secret-key")
    else:
        entry["Resource"]["PendingSecureBoot"] = "1"
    apply = AsyncMock()
    monkeypatch.setattr(core, "apply_lpar_profile_via_cli", apply)
    result = await core.create_and_stamp_lpar(
        hmc,
        SYSTEM,
        core.LparCreation(
            "part-R1",
            "AIX/Linux",
            LparResources(desired_memory=1024),
            apply_profile=True,
            security_policy=LparSecurityPolicy(secure_boot=0),
        ),
    )
    assert result.security_policy_step.status == "error"
    assert "secret-host" not in repr(result.security_policy_step)
    apply.assert_not_awaited()
    command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain", ["stamp", "settings", "reporting", "vtpm", "io", "policy", None]
)
@pytest.mark.parametrize("missing", [False, True])
async def test_apply_waits_for_requested_domain_verification(
    creation_target, monkeypatch, domain, missing
):
    from hmcpctl.documents.lpar import (
        LparReportingSettings,
        LparResources,
        LparSettings,
    )
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    hmc, _, _ = creation_target
    events = []

    async def verified(name):
        events.append(name)
        if domain == name and missing:
            return None
        return WorkflowStep(name, "error" if domain == name else "ok")

    for name, helper in [
        ("settings", "_created_settings_step"),
        ("reporting", "_created_reporting_step"),
        ("vtpm", "_created_vtpm_step"),
        ("io", "created_io_step"),
        ("policy", "_created_security_policy_step"),
    ]:

        async def read(*args, name=name):
            return await verified(name)

        monkeypatch.setattr(core, helper, read)
    monkeypatch.setattr(
        core,
        "stamp_created_lpar_ownership",
        AsyncMock(return_value=(domain != "stamp", ())),
    )
    monkeypatch.setattr(core, "read_vtpm_capabilities", AsyncMock())
    monkeypatch.setattr(core, "admit_vtpm", lambda *args: None)
    monkeypatch.setattr(core, "admit_creation_io", AsyncMock())
    apply = AsyncMock(side_effect=lambda *a: events.append("apply"))
    monkeypatch.setattr(core, "apply_lpar_profile_via_cli", apply)
    # The physical-I/O requested property is exercised through its real typed declaration.
    from hmcpctl.ssh.profiles import ProfileIoSlot

    creation = core.LparCreation(
        "part-R1",
        "AIX/Linux",
        LparResources(desired_memory=1024),
        apply_profile=True,
        settings=LparSettings(time_ref=True),
        reporting_settings=LparReportingSettings(work_group_id=0),
        vtpm_settings=LparVtpmSettings(False),
        physical_io=LparPhysicalIO(io_slots=(ProfileIoSlot("21010010", None, False),)),
        security_policy=LparSecurityPolicy(secure_boot=0),
    )
    result = await core.create_and_stamp_lpar(hmc, SYSTEM, creation)
    assert apply.await_count == int(domain is None)
    if domain is None:
        assert events[-1] == "apply"
        assert result.apply_step.status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_empty_policy_preserves_each_route(creation_target, native):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core

    hmc, entry, command = creation_target
    hmc.create_logical_partition.return_value = entry
    result = await core.create_and_stamp_lpar(
        hmc,
        SYSTEM,
        core.LparCreation(
            "part-R1",
            "AIX/Linux",
            LparResources() if native else LparResources(desired_memory=1024),
            security_policy=LparSecurityPolicy(),
        ),
    )
    assert result.security_policy_step is None
    assert command.await_count == int(native)
    assert hmc.create_logical_partition.await_count == int(not native)
    hmc.get_console_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        LparSecurityPolicy(keystore_signed_updates_without_verification=False),
        LparSecurityPolicy(linux_dynamic_key_secure_boot=False),
    ],
)
async def test_creation_policy_invalid_context_refuses_before_create(
    creation_target, policy
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core

    hmc, _, command = creation_target
    with pytest.raises(ValueError):
        await core.create_and_stamp_lpar(
            hmc,
            SYSTEM,
            core.LparCreation(
                "part-R1",
                "OS400",
                LparResources(desired_memory=1024),
                security_policy=policy,
            ),
        )
    command.assert_not_awaited()
    hmc.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "uuid", "missing-policy", "read"])
async def test_vios_creation_uses_contained_immutable_policy_readback(
    policy_target, monkeypatch, failure
):
    from copy import deepcopy

    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.vios import core

    hmc, entry, command = policy_target
    entry["Resource"]["PartitionType"] = "Virtual IO Server"
    hmc.find_vios_by_name.side_effect = [None, entry]
    monkeypatch.setattr(core, "require_memory_within_system", AsyncMock())
    if failure == "uuid":
        wrong = deepcopy(entry)
        wrong["UUID"] = SYSTEM
        hmc.get_uom.return_value = wrong
    elif failure == "missing-policy":
        entry["Resource"].pop("PendingSecureBoot")
    elif failure == "read":
        hmc.get_uom.side_effect = HMCError("secret-host secret-key")
    if failure:
        with pytest.raises(HMCError, match="exists or may exist") as error:
            await core.create_vios(
                hmc,
                SYSTEM,
                "part-R1",
                LparResources(desired_memory=1024),
                security_policy=LparSecurityPolicy(secure_boot=0),
            )
        assert "secret-host" not in str(error.value)
    else:
        assert (
            await core.create_vios(
                hmc,
                SYSTEM,
                "part-R1",
                LparResources(desired_memory=1024),
                security_policy=LparSecurityPolicy(secure_boot=0),
            )
            == entry
        )
    command.assert_awaited_once()
    hmc.get_uom.assert_awaited_once_with("VirtualIOServer", PARTITION)


@pytest.mark.asyncio
async def test_creation_policy_keylock_is_not_silently_lost(creation_target):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core

    hmc, _, command = creation_target
    with pytest.raises(ValueError, match="cannot represent keylock"):
        await core.create_and_stamp_lpar(
            hmc,
            SYSTEM,
            core.LparCreation(
                "part-R1",
                "AIX/Linux",
                LparResources(desired_memory=1024),
                keylock="normal",
                security_policy=LparSecurityPolicy(secure_boot=0),
            ),
        )
    command.assert_not_awaited()
