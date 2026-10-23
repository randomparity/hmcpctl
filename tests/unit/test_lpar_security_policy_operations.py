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
