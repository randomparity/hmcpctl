"""Tests for the LogicalPartition create builder and the modify field mapping."""

import ast
import pathlib
import xml.etree.ElementTree as ET

import pytest

from hmcpctl.documents import common
from hmcpctl.documents.access import AUTHENTICATION_TYPES, build_hmc_user_document
from hmcpctl.documents.adapters import build_client_network_adapter_document
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.logon import build_logon_request_document
from hmcpctl.documents.lpar import (
    PARTITION_TYPES,
    SHARING_MODES,
    LparResources,
    build_lpar_document,
    partition_updates,
)
from hmcpctl.documents.remote_access import build_remote_access_document

_EMPTY_LPAR = ET.fromstring(
    '<LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"/>'
)


def test_document_package_initializer_imports_nothing():
    """Every import of a builder module reads this file, so it must not re-export (#1495).

    ADR 0127 adds each package initializer on an import's resolution path to the
    operation's closure; a re-export here would put every builder in every closure.
    """
    initializer = pathlib.Path(common.__file__).with_name("__init__.py")
    tree = ast.parse(initializer.read_text(encoding="utf-8"))

    assert [type(node).__name__ for node in tree.body] == ["Expr"]


@pytest.mark.parametrize(
    "name",
    [
        "AUTHENTICATION_TYPES",
        "AuthenticationType",
        "ATOM_NS",
        "DET",
        "ET",
        "KEYLOCK_POSITIONS",
        "LparResources",
        "MEM_MIRRORING_MODES",
        "PARTITION_TYPES",
        "POWER_OFF_POLICIES",
        "POWER_ON_LPAR_START_POLICIES",
        "SHARING_MODES",
        "STORAGE_KINDS",
        "StorageKind",
        "WEB_NS",
        "_dedicated_processor_body",
        "_memory_config",
        "_processor_config",
        "_shared_processor_body",
        "_validate_sharing_mode",
        "escapes_string_arguments",
        "lpar_envelope",
    ],
)
def test_common_has_no_domain_or_xml_primitive_exports(name):
    assert not hasattr(common, name)


def test_minimal_create_document():
    xml = build_lpar_document(name="test")
    assert "<PartitionName" in xml and "test" in xml
    assert "AIX/Linux" in xml
    # No memory/proc blocks when nothing is requested
    assert "PartitionMemoryConfiguration" not in xml
    assert "PartitionProcessorConfiguration" not in xml


def test_memory_config():
    xml = build_lpar_document(
        name="m",
        resources=LparResources(min_memory=256, desired_memory=512, max_memory=1024),
    )
    assert "<DesiredMemory" in xml and "512" in xml
    assert "<MaximumMemory" in xml and "1024" in xml
    assert "<MinimumMemory" in xml and "256" in xml


def test_shared_processor_config():
    xml = build_lpar_document(
        name="s",
        resources=LparResources(
            desired_procs=0.5,
            max_procs=2.0,
            desired_vcpus=1,
            max_vcpus=2,
            dedicated=False,
            uncapped=True,
        ),
    )
    assert "<HasDedicatedProcessors" in xml and "false" in xml
    assert "SharedProcessorConfiguration" in xml
    assert "DesiredProcessingUnits" in xml and "0.5" in xml
    assert "DesiredVirtualProcessors" in xml
    assert "uncapped" in xml


def test_shared_processor_config_states_the_mode():
    """V10R3 refuses a shared create without the mode: REST0126 proc_mode (#1161 P38)."""
    xml = build_lpar_document(
        name="m",
        resources=LparResources(desired_procs=0.5, desired_vcpus=1),
    )
    assert "SharedProcessorConfiguration" in xml
    assert "DesiredProcessingUnits" in xml and "0.5" in xml
    assert ">false</HasDedicatedProcessors>" in xml
    assert "SharingMode" not in xml
    assert "UncappedWeight" not in xml


def test_dedicated_processor_config():
    xml = build_lpar_document(
        name="d",
        resources=LparResources(
            dedicated=True, min_procs=2, desired_procs=2, max_procs=4
        ),
    )
    assert "DedicatedProcessorConfiguration" in xml
    assert "<DesiredProcessors" in xml and "2" in xml
    assert "<MaximumProcessors" in xml and "4" in xml
    assert "true" in xml  # HasDedicatedProcessors


@pytest.mark.parametrize(
    ("resources", "match"),
    [
        (
            LparResources(max_procs=4.0, max_vcpus=2),
            r"max_procs=4\.0 exceeds max_vcpus=2.*--max-procs",
        ),
        (
            LparResources(desired_procs=1.5),
            r"desired_procs=1\.5 exceeds desired_vcpus=1.*--vcpus",
        ),
        (
            LparResources(
                min_procs=1.5, desired_procs=2.0, desired_vcpus=2, max_vcpus=4
            ),
            r"min_procs=1\.5 exceeds min_vcpus=1.*--min-procs",
        ),
    ],
)
def test_build_refuses_units_above_vcpus(resources, match):
    """A virtual processor uses at most 1.0 processing unit (#1034)."""
    with pytest.raises(ValueError, match=match):
        build_lpar_document(name="s", resources=resources)


@pytest.mark.parametrize(
    ("resources", "rendered"),
    [
        (
            LparResources(
                min_procs=1.0,
                desired_procs=2.0,
                max_procs=4.0,
                min_vcpus=1,
                desired_vcpus=2,
                max_vcpus=4,
            ),
            ">2</DesiredProcessingUnits>",
        ),
        (
            LparResources(dedicated=True, desired_procs=4.0, desired_vcpus=1),
            ">4</DesiredProcessors>",
        ),
    ],
)
def test_build_accepts_units_within_vcpus_and_dedicated(resources, rendered):
    assert rendered in build_lpar_document(name="s", resources=resources)


@pytest.mark.parametrize(
    ("dedicated", "desired_procs"), [(True, 1), (False, 1), (None, None)]
)
def test_invalid_sharing_mode_is_rejected_before_xml(dedicated, desired_procs):
    illegal = "uncapped</SharingMode><Injected>true</Injected>"

    with pytest.raises(ValueError) as exc_info:
        build_lpar_document(
            name="bad",
            resources=LparResources(
                dedicated=dedicated,
                desired_procs=desired_procs,
                sharing_mode=illegal,
            ),
        )

    message = str(exc_info.value)
    assert "sharing_mode" in message
    assert all(value in message for value in SHARING_MODES)
    assert illegal not in message


@pytest.mark.parametrize("malformed", [["uncapped"], {"mode": "uncapped"}])
@pytest.mark.parametrize(
    "builder",
    [
        lambda resources: build_lpar_document(name="bad", resources=resources),
        lambda resources: partition_updates(_EMPTY_LPAR, resources=resources),
    ],
)
def test_malformed_sharing_mode_type_raises_actionable_value_error(malformed, builder):
    with pytest.raises(ValueError) as exc_info:
        builder(LparResources(desired_procs=1, sharing_mode=malformed))

    message = str(exc_info.value)
    assert "sharing_mode" in message
    assert all(value in message for value in SHARING_MODES)


# LogicalPartitionProcessorSharingMode.Enum in the live V10R3 schema (#1161 P31);
# "proces" is the HMC's own spelling.
REST_SHARING_MODES = {
    "capped": "capped",
    "uncapped": "uncapped",
    "keep_idle_procs": "keep idle procs",
    "share_idle_procs": "sre idle proces",
    "share_idle_procs_active": "sre idle procs active",
    "share_idle_procs_always": "sre idle procs always",
}


@pytest.mark.parametrize("sharing_mode", sorted(SHARING_MODES))
def test_all_sharing_modes_serialize_in_rest_spelling(sharing_mode):
    dedicated = sharing_mode not in {"capped", "uncapped"}
    xml = build_lpar_document(
        name="ok",
        resources=LparResources(
            dedicated=dedicated, desired_procs=1, sharing_mode=sharing_mode
        ),
    )

    assert f">{REST_SHARING_MODES[sharing_mode]}</SharingMode>" in xml


def test_partition_id_and_type():
    xml = build_lpar_document(name="x", partition_id=25, partition_type="OS400")
    assert "<PartitionID" in xml and "25" in xml
    assert "OS400" in xml


@pytest.mark.parametrize(
    ("partition_type", "message"),
    [("Windows", "partition_type must be one of"), ("Virtual IO Server", "REST0140")],
)
def test_invalid_partition_type(partition_type, message):
    with pytest.raises(ValueError, match=message):
        build_lpar_document(name="bad", partition_type=partition_type)


def test_invalid_keylock():
    with pytest.raises(ValueError, match="keylock"):
        build_lpar_document(name="bad", keylock="turbo")


def test_all_partition_types_accepted():
    assert PARTITION_TYPES == ("AIX/Linux", "OS400")
    for pt in PARTITION_TYPES:
        build_lpar_document(name="ok", partition_type=pt)


def test_all_authentication_types_serialize_in_the_hmcs_lower_case():
    """The HMC lists and accepts only lower case (#632, live 2026-10-06)."""
    for authentication_type in AUTHENTICATION_TYPES:
        xml = build_hmc_user_document(
            user_id="operator", authentication_type=authentication_type
        )
        assert f">{authentication_type.lower()}</AuthenticationType>" in xml


def test_invalid_authentication_type_is_rejected():
    with pytest.raises(ValueError, match="authentication_type"):
        build_hmc_user_document(user_id="operator", authentication_type="radius")


def test_operating_system_type_is_never_sent():
    """OperatingSystemType is read-only and needs a ksv; the HMC sets AIX/Linux (#1161 P39)."""
    xml = build_lpar_document(name="mypart", partition_type="OS400")
    assert "OperatingSystemType" not in xml


def test_keylock_emitted():
    xml = build_lpar_document(name="mypart", keylock="normal")
    assert "<KeylockPosition" in xml
    assert "normal" in xml


def test_max_virtual_slots_emitted():
    """The slots live in PartitionIOConfiguration, which V10R3 honors (#1161 P39)."""
    root = ET.fromstring(
        build_lpar_document(name="mypart", max_virtual_slots=64).encode("utf-8")
    )
    io = root.find(f"{{{UOM_NS}}}PartitionIOConfiguration")
    assert io is not None and io.get("schemaVersion") == "V1_0"
    assert io.findtext(f"{{{UOM_NS}}}MaximumVirtualIOSlots") == "64"


def test_all_three_new_fields_together():
    xml = build_lpar_document(
        name="mypart",
        keylock="manual",
        max_virtual_slots=32,
    )
    assert "OperatingSystemType" not in xml
    assert "<KeylockPosition" in xml and "manual" in xml
    assert "<MaximumVirtualIOSlots" in xml and "32" in xml


def test_new_fields_default_to_none_backward_compat():
    xml = build_lpar_document(name="mypart")
    assert "OperatingSystemType" not in xml
    assert "KeylockPosition" not in xml
    assert "PartitionIOConfiguration" not in xml


@pytest.mark.parametrize("keylock", ["auto", "unknown", "norm"])
def test_keylock_outside_the_schema_is_refused(keylock):
    """V10R3's KeylockPosition.Enum is manual, normal, unknown; unknown is not creatable."""
    with pytest.raises(ValueError, match="keylock must be one of: normal, manual"):
        build_lpar_document(name="mypart", keylock=keylock)


# Output captured before #1495 moved these builders between modules; the move must
# leave every byte of the documents unchanged.
_ENVELOPE = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_UOM = 'xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"'
_CUD = 'ksv="V1_17_0" kb="CUD" kxe="false"'


def test_logon_request_document_is_unchanged():
    xml = build_logon_request_document(
        user="operator",
        password="s3cret",  # pragma: allowlist secret
    )

    assert xml == (
        _ENVELOPE
        + '<LogonRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/'
        'mc/2012_10/" schemaVersion="V1_0">\n'
        "  <Metadata>\n    <Atom/>\n  </Metadata>\n"
        '  <UserID kb="CUR" kxe="false">operator</UserID>\n'
        '  <Password kb="CUR" kxe="false">s3cret</Password>\n'
        "</LogonRequest>\n"
    )


def test_user_profile_document_is_unchanged():
    xml = build_hmc_user_document(
        user_id="operator",
        password="p",
        description="d",
        authentication_type="LDAP",
        associated_task_role="hmcviewer",
        associated_resource_roles=["AllSystemResources"],
        session_timeout=5,
    )

    assert xml == (
        _ENVELOPE + f'<UserProfile {_UOM} schemaVersion="V1_0">\n'
        "  <Metadata><Atom/></Metadata>\n"
        '  <UserID ksv="V1_17_0" kb="COR" kxe="false">operator</UserID>\n'
        f"  <UserDescription {_CUD}>d</UserDescription>\n"
        f"  <AuthenticationType {_CUD}>ldap</AuthenticationType>\n"
        f"  <UserProfilePassword {_CUD}>p</UserProfilePassword>\n"
        f"  <AssociatedTaskRole {_CUD}>hmcviewer</AssociatedTaskRole>\n"
        f'  <AssociatedResourceRoles {_CUD} schemaVersion="V1_0"><Metadata><Atom/>'
        f"</Metadata><AssociatedResourceRole {_CUD}>AllSystemResources"
        "</AssociatedResourceRole></AssociatedResourceRoles>\n"
        f"  <SessionTimeout {_CUD}>5</SessionTimeout>\n"
        "</UserProfile>\n"
    )


def test_remote_access_document_is_unchanged():
    xml = build_remote_access_document({"LdapEnabled": True}, ["KerberosEnabled"])

    assert xml == (
        _ENVELOPE + f'<ManagementConsole {_UOM} schemaVersion="V1_0">\n'
        "  <Metadata><Atom/></Metadata>\n"
        '  <LdapConfiguration schemaVersion="V1_0">\n'
        '    <LdapEnabled kb="CUR" kxe="false">true</LdapEnabled>\n'
        "  </LdapConfiguration>\n"
        '  <KerberosConfiguration schemaVersion="V1_0">\n'
        '    <KerberosEnabled kb="CUR" kxe="false"/>\n'
        "  </KerberosConfiguration>\n"
        "</ManagementConsole>\n"
    )


_NETWORK_LEGACY_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<ClientNetworkAdapter xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/" schemaVersion="V1_0">\n  <Metadata><Atom/></Metadata>\n  <VirtualSlotNumber kb="COD" kxe="false">3</VirtualSlotNumber>\n  <MACAddress kb="CUR" kxe="false">020000000001</MACAddress>\n  <PortVLANID kb="CUR" kxe="false">100</PortVLANID>\n  <TaggedVLANSupported kb="CUA" kxe="false">true</TaggedVLANSupported>\n  <VirtualSwitchID kb="ROR" kxe="false">1</VirtualSwitchID>\n</ClientNetworkAdapter>\n'


def test_network_creation_omission_preserves_legacy_bytes():
    assert (
        build_client_network_adapter_document(100, 3, 1, True, "020000000001")
        == _NETWORK_LEGACY_XML
    )
    assert (
        build_client_network_adapter_document(
            100,
            3,
            1,
            True,
            "020000000001",
            addl_vlan_ids=None,
            allowed_os_mac_addrs=None,
            qos_priority=None,
            vsi_manager_id=None,
            vsi_type_id=None,
            vsi_type_version=None,
        )
        == _NETWORK_LEGACY_XML
    )


def test_network_creation_six_fields_use_schema_order_and_explicit_zero():
    root = ET.fromstring(
        build_client_network_adapter_document(
            100,
            3,
            1,
            True,
            "020000000001",
            addl_vlan_ids=[1, 4094],
            allowed_os_mac_addrs=["0200000000ab"],
            qos_priority=0,
            vsi_manager_id=1,
            vsi_type_id=2,
            vsi_type_version=3,
        )
    )
    names = [e.tag.rsplit("}", 1)[-1] for e in root]
    assert names == [
        "Metadata",
        "VirtualSlotNumber",
        "AllowedOperatingSystemMACAddresses",
        "MACAddress",
        "PortVLANID",
        "QualityOfServicePriority",
        "QualityOfServicePriorityEnabled",
        "TaggedVLANIDs",
        "TaggedVLANSupported",
        "VirtualStationInterfaceManagerID",
        "VirtualStationInterfaceTypeID",
        "VirtualSwitchID",
        "VirtualStationInterfaceTypeVersion",
    ]
    by_name = {e.tag.rsplit("}", 1)[-1]: e for e in root}
    assert by_name["TaggedVLANIDs"].text == "1 4094"
    assert by_name["TaggedVLANIDs"].get("kb") == "CUA"
    assert by_name["AllowedOperatingSystemMACAddresses"].text == "0200000000AB"
    assert by_name["QualityOfServicePriority"].text == "0"
    assert by_name["QualityOfServicePriorityEnabled"].text == "true"
    for field in [
        "AllowedOperatingSystemMACAddresses",
        "QualityOfServicePriority",
        "QualityOfServicePriorityEnabled",
        "VirtualStationInterfaceManagerID",
        "VirtualStationInterfaceTypeID",
        "VirtualStationInterfaceTypeVersion",
    ]:
        assert by_name[field].get("kb") == "CUD"
        assert by_name[field].get("kxe") == "false"


@pytest.mark.parametrize("policy, expected", [("all", "ALL"), ("none", "NONE")])
def test_network_creation_mac_policies_and_disabled_qos(policy, expected):
    root = ET.fromstring(
        build_client_network_adapter_document(
            100,
            allowed_os_mac_addrs=policy,
            qos_priority="none",
            addl_vlan_ids=[],
        )
    )
    values = {e.tag.rsplit("}", 1)[-1]: e.text for e in root}
    assert values["AllowedOperatingSystemMACAddresses"] == expected
    assert values["QualityOfServicePriorityEnabled"] == "false"
    assert "QualityOfServicePriority" not in values
    assert "TaggedVLANIDs" in values


@pytest.mark.parametrize(
    "kwargs",
    [
        {"addl_vlan_ids": "1"},
        {"addl_vlan_ids": [None]},
        {"addl_vlan_ids": [True]},
        {"addl_vlan_ids": [0]},
        {"addl_vlan_ids": [4095]},
        {"addl_vlan_ids": [1.5]},
        {"addl_vlan_ids": list(range(1, 22))},
        {"allowed_os_mac_addrs": "ALL"},
        {"allowed_os_mac_addrs": []},
        {"allowed_os_mac_addrs": [1]},
        {"allowed_os_mac_addrs": ["02:00:00:00:00:01"]},
        {"allowed_os_mac_addrs": ["020000000001"] * 5},
        {"qos_priority": True},
        {"qos_priority": -1},
        {"qos_priority": 8},
        {"qos_priority": "0"},
        {"qos_priority": 0.5},
        {"vsi_manager_id": True},
        {"vsi_type_id": "1"},
        {"vsi_type_version": 2**31},
        {"vsi_manager_id": -(2**31) - 1},
    ],
)
def test_network_creation_rejects_invalid_selected_fields(kwargs):
    with pytest.raises(ValueError):
        build_client_network_adapter_document(100, tagged=True, **kwargs)


def test_network_creation_requires_explicit_tagging_for_additional_vlans():
    with pytest.raises(ValueError, match="tagged"):
        build_client_network_adapter_document(100, addl_vlan_ids=[2])


@pytest.mark.parametrize("value", [-(2**31), 2**31 - 1])
def test_network_creation_retains_vsi_representational_limits_for_hmc_validation(value):
    xml = build_client_network_adapter_document(100, vsi_type_id=value)
    root = ET.fromstring(xml)
    assert root.find(f"{{{UOM_NS}}}VirtualStationInterfaceTypeID").text == str(value)


def test_network_creation_retains_escaping_boundary_for_unsupported_tuple():
    with pytest.raises(TypeError, match="escaping boundary"):
        build_client_network_adapter_document(100, tagged=True, addl_vlan_ids=(1,))
