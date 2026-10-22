"""Profile projection/readback contracts based on the captured UOM grammar."""

import xml.etree.ElementTree as ET
from copy import deepcopy

import pytest

from hmcpctl.documents.partition_profile import (
    ProfilePatch,
    apply_profile_patch,
    clone_profile_document,
    compare_profiles,
    parse_profile,
)

U = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
A = "http://www.w3.org/2005/Atom"


def profile(adapter="ProfileTrunkAdapter"):
    root = parse_profile(f'''<LogicalPartitionProfile xmlns="{U}" schemaVersion="V1_0">
      <Metadata><Atom><AtomID>identity-source</AtomID><AtomCreated>before</AtomCreated></Atom></Metadata>
      <AssociatedPartition kb="ROR" href="https://hmc.test/rest/api/uom/LogicalPartition/11111111-1111-4111-8111-111111111111"/>
      <IOConfigurationInstance kb="CUD"><Metadata><Atom/></Metadata>
        <ProfileVirtualIOAdapters kb="CUD"><ProfileVirtualIOAdapterSubclass><{adapter}>
          <Metadata><Atom/></Metadata><VirtualSlotNumber kb="CUR">3</VirtualSlotNumber>
          <IsRequired kb="CUD">false</IsRequired>
        </{adapter}></ProfileVirtualIOAdapterSubclass></ProfileVirtualIOAdapters>
        <VirtualOpticonnectPool kb="CUD">false</VirtualOpticonnectPool>
      </IOConfigurationInstance>
      <ProcessorAttributes kb="CUR"><Metadata><Atom/></Metadata><HasDedicatedProcessors kb="CUR">false</HasDedicatedProcessors>
        <SharedProcessorConfiguration kb="CUR"><Metadata><Atom/></Metadata>
          <MinimumVirtualProcessors kb="CUR">1</MinimumVirtualProcessors><DesiredVirtualProcessors kb="CUR">2</DesiredVirtualProcessors><MaximumVirtualProcessors kb="CUR">4</MaximumVirtualProcessors>
          <MinimumProcessingUnits kb="CUR">0.05</MinimumProcessingUnits><DesiredProcessingUnits kb="CUR">0.1</DesiredProcessingUnits><MaximumProcessingUnits kb="CUR">1.0</MaximumProcessingUnits>
          <UncappedWeight kb="CUD">128</UncappedWeight>
        </SharedProcessorConfiguration><SharingMode kb="CUR">capped</SharingMode>
      </ProcessorAttributes>
      <ProfileMemory kb="CUR"><Metadata><Atom/></Metadata><MinimumMemory kb="CUD">512</MinimumMemory><DesiredMemory kb="CUD">768</DesiredMemory><MaximumMemory kb="CUD">1024</MaximumMemory>
        <DesiredEntitledMemory kb="CUD">AUTO</DesiredEntitledMemory><DesiredHugePageCount kb="CUD">0</DesiredHugePageCount><MaximumHugePageCount kb="CUD">0</MaximumHugePageCount><MinimumHugePageCount kb="CUD">0</MinimumHugePageCount><MemoryWeight kb="CUD">128</MemoryWeight><ManualEntitledModeEnabled kb="ROR">false</ManualEntitledModeEnabled>
      </ProfileMemory><ProfileName kb="CUR">source</ProfileName><ProfileType kb="ROR">REG_LPAR_PROFILE_TYPE</ProfileType><SettingID kb="ROO">1</SettingID>
    </LogicalPartitionProfile>''')
    if adapter in {"ProfileVirtualNICDedicated", "ProfileVirtualNICShared"}:
        node = field(
            root,
            f"IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass/{adapter}",
        )
        ET.SubElement(node, "{" + U + "}Details", {"kb": "CUR"})
        if adapter == "ProfileVirtualNICDedicated":
            ET.SubElement(node, "{" + U + "}AssociatedBackingDevices", {"kb": "CUR"})
    return root


def field(root, path):
    return root.find("/".join("{" + U + "}" + p for p in path.split("/")))


def test_typed_patch_preserves_every_omitted_field_and_attribute():
    before = profile()
    after = apply_profile_patch(before, ProfilePatch(desired_memory=1024))
    expected = deepcopy(before)
    field(expected, "ProfileMemory/DesiredMemory").text = "1024"
    assert compare_profiles(expected, after).equal
    assert field(before, "ProfileMemory/DesiredMemory").text == "768"
    assert field(after, "ProfileMemory/ManualEntitledModeEnabled").text == "false"


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"desired_memory": True},
        {"desired_memory": 0},
        {"desired_units": float("nan")},
        {"surprise": 1},
    ],
)
def test_invalid_or_empty_patch(values):
    with pytest.raises(ValueError):
        apply_profile_patch(profile(), ProfilePatch(**values))


@pytest.mark.parametrize(
    "values",
    [
        {"min_memory": 900},
        {"max_memory": 600},
        {"min_processors": 3},
        {"desired_units": 2},
        {"sharing_mode": "keep_idle_procs"},
    ],
)
def test_validate_effective_ranges_and_mode_with_existing_values(values):
    with pytest.raises(ValueError):
        apply_profile_patch(profile(), ProfilePatch(**values))


def test_missing_or_ambiguous_patch_element_refuses_without_injection():
    root = profile()
    p = field(root, "ProfileMemory")
    p.remove(field(root, "ProfileMemory/DesiredMemory"))
    with pytest.raises(ValueError):
        apply_profile_patch(root, ProfilePatch(desired_memory=1024))
    root = profile()
    p = field(root, "ProfileMemory")
    p.append(deepcopy(field(root, "ProfileMemory/DesiredMemory")))
    with pytest.raises(ValueError):
        apply_profile_patch(root, ProfilePatch(desired_memory=1024))


@pytest.mark.parametrize(
    "adapter",
    [
        "ProfileVirtualFibreChannelClientAdapter",
        "ProfileVirtualFibreChannelServerAdapter",
        "ProfileVirtualSCSIClientAdapter",
        "ProfileVirtualSCSIServerAdapter",
        "ProfileClientNetworkAdapter",
        "ProfileTrunkAdapter",
        "ProfileVirtualNICDedicated",
        "ProfileVirtualNICShared",
    ],
)
def test_all_eight_clone_choices_preserve_nested_semantic_ids(adapter):
    source = profile(adapter)
    copy = clone_profile_document(source, "recovery")
    assert field(copy, "ProfileName").text == "recovery"
    assert (
        field(copy, "SettingID") is None and field(copy, "AssociatedPartition") is None
    )
    assert field(copy, "ProfileMemory/ManualEntitledModeEnabled") is None
    assert field(copy, "ProfileMemory/DesiredHugePageCount").text == "0"
    assert (
        field(
            copy,
            f"IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass/{adapter}/VirtualSlotNumber",
        ).text
        == "3"
    )


def test_readonly_ancestor_dominates_descendant_and_unknown_annotations_refuse():
    source = profile()
    parent = ET.SubElement(
        field(source, "IOConfigurationInstance"),
        "{" + U + "}HostChannelAdapters",
        {"kb": "ROR"},
    )
    ET.SubElement(parent, "{" + U + "}SensitiveChild", {"kb": "CUD"}).text = "unchanged"
    assert (
        field(
            clone_profile_document(source, "copy"),
            "IOConfigurationInstance/HostChannelAdapters",
        )
        is None
    )
    for token in ["", "NEW", "CUD CUR"]:
        root = profile()
        field(root, "ProfileMemory/DesiredMemory").set("kb", token)
        with pytest.raises(ValueError):
            clone_profile_document(root, "copy")
    root = profile()
    field(root, "ProfileMemory/DesiredMemory").attrib.pop("kb")
    with pytest.raises(ValueError):
        clone_profile_document(root, "copy")


def test_empty_choice_wrapper_retained_multiple_or_unknown_choices_refused():
    root = profile()
    wrapper = field(
        root,
        "IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass",
    )
    wrapper.clear()
    assert (
        field(
            clone_profile_document(root, "copy"),
            "IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass",
        )
        is not None
    )
    for name in ["ProfileTrunkAdapter", "UnknownAdapter"]:
        root = profile()
        wrapper = field(
            root,
            "IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass",
        )
        wrapper.append(ET.Element("{" + U + "}" + name))
        with pytest.raises(ValueError):
            clone_profile_document(root, "copy")


def test_root_exception_masks_only_four_scalar_texts():
    before = profile()
    after = deepcopy(before)
    for path in [
        "ProfileName",
        "SettingID",
        "Metadata/Atom/AtomID",
        "Metadata/Atom/AtomCreated",
    ]:
        field(after, path).text = "different"
    assert compare_profiles(before, after, recovery=True).equal
    assert not compare_profiles(before, after).equal
    field(after, "SettingID").set("extra", "value")
    assert not compare_profiles(before, after, recovery=True).equal
    after = deepcopy(before)
    field(
        after,
        "IOConfigurationInstance/ProfileVirtualIOAdapters/ProfileVirtualIOAdapterSubclass/ProfileTrunkAdapter/VirtualSlotNumber",
    ).text = "4"
    assert not compare_profiles(before, after, recovery=True).equal


def test_additions_absence_empty_and_diagnostic_limits_never_fake_equality():
    source = profile()
    after = deepcopy(source)
    for i in range(110):
        ET.SubElement(after, "{" + U + "}Extra" + str(i), {"kb": "CUD"}).text = "0"
    comparison = compare_profiles(source, after)
    assert not comparison.equal and comparison.complete and comparison.details_truncated
    assert len(comparison.differences) <= 100
    assert any(d.kind == "added" for d in comparison.differences)
    empty = deepcopy(source)
    field(empty, "ProfileMemory/DesiredHugePageCount").text = None
    assert not compare_profiles(source, empty).equal


@pytest.mark.parametrize(
    "raw",
    [
        "<LogicalPartitionProfile/>",
        '<!DOCTYPE x [<!ENTITY x "value">]><x>&x;</x>',
        f'<entry xmlns="{A}"><content><LogicalPartitionProfile xmlns="{U}"/><LogicalPartitionProfile xmlns="{U}"/></content></entry>',
        f'<LogicalPartitionProfile xmlns="{U}"><ProfileName>x</ProfileName><ProfileName>y</ProfileName></LogicalPartitionProfile>',
    ],
)
def test_malformed_or_ambiguous_native_profile_refused(raw):
    with pytest.raises(ValueError):
        parse_profile(raw)


def test_nested_atom_identity_is_preserved_and_never_masked():
    root = profile()
    atom = field(root, "IOConfigurationInstance/Metadata/Atom")
    ET.SubElement(atom, "{" + U + "}AtomID").text = "nested-id"
    ET.SubElement(atom, "{" + U + "}AtomCreated").text = "nested-created"
    clone = clone_profile_document(root, "recovery")
    assert (
        field(clone, "IOConfigurationInstance/Metadata/Atom/AtomID").text == "nested-id"
    )
    altered = deepcopy(root)
    field(altered, "IOConfigurationInstance/Metadata/Atom/AtomID").text = "different"
    assert not compare_profiles(root, altered, recovery=True).equal


def test_unknown_metadata_shape_refuses_before_copy():
    root = profile()
    ET.SubElement(
        field(root, "Metadata/Atom"), "{" + U + "}UncapturedIdentity"
    ).text = "value"
    with pytest.raises(ValueError):
        clone_profile_document(root, "copy")


def test_nested_sriov_backup_configuration_is_retained_with_semantic_ids():
    root = profile()
    ports = ET.SubElement(
        root, "{" + U + "}ProfileSRIOVEthernetLogicalPorts", {"kb": "CUD"}
    )
    port = ET.SubElement(ports, "{" + U + "}ProfileSRIOVEthernetLogicalPort")
    backups = ET.SubElement(
        port, "{" + U + "}AssociatedProfileVNICBackupDevice", {"kb": "COD"}
    )
    backup = ET.SubElement(backups, "{" + U + "}ProfileSRIOVVirtualNICBackupDevice")
    for name, value in [
        ("VIOSID", "2"),
        ("AdapterID", "1"),
        ("PhysicalPortID", "0"),
        ("FailOverPriority", "10"),
    ]:
        ET.SubElement(backup, "{" + U + "}" + name, {"kb": "COD"}).text = value
    copy = clone_profile_document(root, "recovery")
    for name in ["VIOSID", "AdapterID", "PhysicalPortID", "FailOverPriority"]:
        # Fixed UOM field path; the entropy detector mistakes schema names for a token.
        path = f"ProfileSRIOVEthernetLogicalPorts/ProfileSRIOVEthernetLogicalPort/AssociatedProfileVNICBackupDevice/ProfileSRIOVVirtualNICBackupDevice/{name}"  # pragma: allowlist secret
        assert field(copy, path).text == field(root, path).text


def test_physical_io_configuration_retains_writable_volume_content_and_drops_readonly_fields():
    root = profile()
    slots = ET.SubElement(
        field(root, "IOConfigurationInstance"),
        "{" + U + "}ProfileIOSlots",
        {"kb": "CUD"},
    )
    slot = ET.SubElement(slots, "{" + U + "}ProfileIOSlot")
    associated = ET.SubElement(slot, "{" + U + "}AssociatedIOSlot", {"kb": "CUD"})
    related = ET.SubElement(associated, "{" + U + "}RelatedIOAdapter", {"kb": "CUD"})
    adapter = ET.SubElement(related, "{" + U + "}PhysicalFibreChannelAdapter")
    ports = ET.SubElement(
        adapter, "{" + U + "}PhysicalFibreChannelPorts", {"kb": "CUD"}
    )
    port = ET.SubElement(ports, "{" + U + "}PhysicalFibreChannelPort")
    ET.SubElement(port, "{" + U + "}PortName", {"kb": "CUR"}).text = "synthetic-port"
    ET.SubElement(port, "{" + U + "}AvailablePorts", {"kb": "ROR"}).text = "8"
    volumes = ET.SubElement(port, "{" + U + "}PhysicalVolumes", {"kb": "CUD"})
    volume = ET.SubElement(volumes, "{" + U + "}PhysicalVolume")
    ET.SubElement(
        volume, "{" + U + "}UnknownSavedConfiguration", {"kb": "CUD"}
    ).text = "opaque-value"
    copy = clone_profile_document(root, "recovery")
    copied_ports = list(copy.iter("{" + U + "}PhysicalFibreChannelPort"))
    assert len(copied_ports) == 1
    assert copied_ports[0].find("{" + U + "}AvailablePorts") is None
    assert (
        next(copy.iter("{" + U + "}UnknownSavedConfiguration")).text == "opaque-value"
    )
    assert (
        field(
            root,
            "IOConfigurationInstance/ProfileIOSlots/ProfileIOSlot/AssociatedIOSlot/RelatedIOAdapter/PhysicalFibreChannelAdapter/PhysicalFibreChannelPorts/PhysicalFibreChannelPort/AvailablePorts",
        )
        is not None
    )
