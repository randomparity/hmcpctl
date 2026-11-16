"""V10R3 schema conformance of the UOM write builders (#961).

Expected values come from two sources. The first is the live fixture
tests/storage/vscsi_mapping_v10r3.xml (#940). The second is the table in RECORDED, transcribed
from the live V10R3 values recorded in the #961 body. Neither source is documentation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from defusedxml import ElementTree as DET

from hmcpctl.documents.adapters import (
    build_client_network_adapter_document,
    build_vfc_adapter_document,
    build_vscsi_adapter_document,
)
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import (
    LparResources,
    build_lpar_document,
    build_vios_document,
)
from hmcpctl.documents.storage import (
    build_virtual_disk_element,
    build_virtual_network_document,
    build_virtual_optical_mapping_document,
    build_volume_group_document,
    build_vscsi_mapping_document,
    build_web_file_document,
)
from hmcpctl.xmlutil import localname

STORAGE_FIXTURES = Path(__file__).parents[1] / "storage"
FIXTURE = DET.parse(STORAGE_FIXTURES / "vscsi_mapping_v10r3.xml").getroot()
VOLUME_GROUP = DET.parse(STORAGE_FIXTURES / "volume_group_v10r3.xml").getroot()


def _tree(xml: str):
    return DET.fromstring(xml.encode("utf-8"))


def _first(root, name: str):
    return next(el for el in root.iter() if localname(el.tag) == name)


def _kbx(el) -> dict[str, str]:
    return {k: v for k, v in el.attrib.items() if k in ("kb", "kxe", "schemaVersion")}


def _children(el) -> list[str]:
    return [localname(child.tag) for child in el]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(name in it for name in needle)


def test_vscsi_adapter_matches_fixture() -> None:
    built = _tree(build_vscsi_adapter_document(7, 11, slot_number=4))
    live = _first(FIXTURE, "ClientAdapter")
    assert _is_subsequence(_children(built), _children(live))
    for child in built:
        name = localname(child.tag)
        assert _kbx(child) == _kbx(_first(live, name)), name


LINK = "https://hmc.example.invalid/rest/api/uom/LogicalPartition/lpar-1"
RECORDED = [
    (
        build_client_network_adapter_document(42, 3, 1, True, "020000000001"),
        {
            "VirtualSlotNumber": "COD",
            "VirtualSwitchID": "ROR",
            "PortVLANID": "CUR",
            "MACAddress": "CUR",
        },
    ),
    (build_volume_group_document("vg1", ["hdisk1"]), {"VolumeName": "CUR"}),
    (
        build_vscsi_mapping_document("PhysicalVolume", "hdisk5", LINK),
        {"VolumeName": "CUR"},
    ),
    (
        build_virtual_optical_mapping_document("a.iso", LINK),
        {"MediaName": "CUR"},
    ),
    (
        build_virtual_network_document("n1", 10, 0, tagged=True),
        {"NetworkVLANID": "COD", "VswitchID": "ROR", "TaggedNetwork": "COD"},
    ),
    (
        build_vfc_adapter_document(1, 2, 3),
        {
            "AdapterType": "ROR",  # live-vocabulary: allow a kb code, not an element value
            "VirtualSlotNumber": "COD",
            "ConnectingPartitionID": "CUR",
            "ConnectingVirtualSlotNumber": "CUR",
        },
    ),
]


@pytest.mark.parametrize(
    ("xml", "expected"), RECORDED, ids=["-".join(expected) for _, expected in RECORDED]
)
def test_recorded_kb_values(xml: str, expected: dict[str, str]) -> None:
    root = _tree(xml)
    assert {name: _first(root, name).attrib.get("kb") for name in expected} == expected


def test_virtual_disk_create_matches_fixture() -> None:
    built = _first(_tree(build_virtual_disk_element("lv1", 2048)), "VirtualDisk")
    live = _first(FIXTURE, "VirtualDisk")
    assert _kbx(built) == _kbx(live)
    names = [n for n in _children(built) if n != "Metadata"]
    assert names == ["DiskCapacity", "DiskName"]
    assert _is_subsequence(names, _children(live))
    for name in names:
        assert _kbx(_first(built, name)) == _kbx(_first(live, name)), name


def test_vscsi_mapping_matches_fixture() -> None:
    xml = build_vscsi_mapping_document("VirtualDisk", "vd1", LINK, "vtscsi9")
    built = _first(_tree(xml), "VirtualSCSIMapping")
    live = _first(FIXTURE, "VirtualSCSIMapping")
    assert _kbx(built) == _kbx(live)
    assert _children(built) == [
        "Metadata",
        "AssociatedLogicalPartition",
        "Storage",
        "TargetDevice",
    ]
    assert _is_subsequence(_children(built), _children(live))
    link = _first(built, "AssociatedLogicalPartition")
    assert link.tag == f"{{{UOM_NS}}}AssociatedLogicalPartition"
    assert link.attrib["href"] == LINK
    assert link.attrib["rel"] == "related"
    for name in (
        "AssociatedLogicalPartition",
        "Storage",
        "VirtualDisk",
        "DiskName",
        "TargetDevice",
        "LogicalVolumeVirtualTargetDevice",
        "TargetName",
    ):
        assert _kbx(_first(built, name)) == _kbx(_first(live, name)), name
    assert _children(_first(built, "Storage")) == ["VirtualDisk"]
    assert _first(built, "TargetName").text == "vtscsi9"


@pytest.mark.parametrize(
    ("xml", "storage", "target"),
    [
        (
            build_vscsi_mapping_document("PhysicalVolume", "hdisk5", LINK, "vt1"),
            "PhysicalVolume",
            "PhysicalVolumeVirtualTargetDevice",
        ),
        (
            build_virtual_optical_mapping_document("a.iso", LINK, "vtopt1"),
            "VirtualOpticalMedia",
            "VirtualOpticalTargetDevice",
        ),
    ],
    ids=["physical-volume", "optical"],
)
def test_mapping_wrappers_and_target_device(
    xml: str, storage: str, target: str
) -> None:
    root = _tree(xml)
    target_device = _first(root, "TargetDevice")
    assert _first(root, storage).attrib == {"schemaVersion": "V1_0"}
    assert _children(target_device) == [target]
    assert _first(root, target).attrib == {"schemaVersion": "V1_0"}
    assert not (target_device.text or "").strip()


def test_mapping_without_target_device_omits_it() -> None:
    root = _tree(build_vscsi_mapping_document("VirtualDisk", "vd1", LINK))
    assert _children(_first(root, "VirtualSCSIMapping")) == [
        "Metadata",
        "AssociatedLogicalPartition",
        "Storage",
    ]


def _assert_matches_live(built, live) -> None:
    """Built children, repeats collapsed, are a live subsequence with live kb/kxe/schemaVersion."""
    assert _kbx(built) == _kbx(live), localname(built.tag)
    names = list(dict.fromkeys(_children(built)))
    assert _is_subsequence(names, _children(live)), (localname(built.tag), names)
    for child in built:
        _assert_matches_live(child, _first(live, localname(child.tag)))


def test_volume_group_create_matches_fixture() -> None:
    built = _tree(build_volume_group_document("vg1", ["hdisk1", "hdisk2"]))
    assert _children(_first(built, "PhysicalVolumes")).count("PhysicalVolume") == 2
    _assert_matches_live(built, VOLUME_GROUP)


def test_volume_group_physical_volume_attributes() -> None:
    root = _tree(build_volume_group_document("vg1", ["hdisk1"]))
    assert _first(root, "PhysicalVolume").attrib == {"schemaVersion": "V1_0"}


RESOURCES = LparResources(
    min_memory=512,
    desired_memory=1024,
    max_memory=2048,
    desired_procs=0.5,
    desired_vcpus=1,
)
DEDICATED = LparResources(desired_procs=1, dedicated=True)
BUILT = {
    "vscsi-adapter": build_vscsi_adapter_document(1, 2, 3),
    "vfc-adapter": build_vfc_adapter_document(1, 2, 3),
    "network-adapter": build_client_network_adapter_document(
        1, 2, 0, True, "020000000001"
    ),
    "volume-group": build_volume_group_document("vg1", ["hdisk1"]),
    "virtual-disk": build_virtual_disk_element("lv1", 1024),
    "vscsi-mapping": build_vscsi_mapping_document(
        "PhysicalVolume", "hdisk1", LINK, "vt1"
    ),
    "optical-mapping": build_virtual_optical_mapping_document("a.iso", LINK, "vt2"),
    "virtual-network": build_virtual_network_document("n1", 10, 0, LINK),
    "web-file": build_web_file_document(
        "a.iso", 1, "00000000-0000-0000-0000-000000000001"
    ),
    "lpar-shared": build_lpar_document("p1", resources=RESOURCES),
    "lpar-dedicated": build_lpar_document("p1", resources=DEDICATED),
    "vios": build_vios_document("v1"),
}


@pytest.mark.parametrize("xml", BUILT.values(), ids=BUILT.keys())
def test_metadata_elements_carry_schema_version(xml: str) -> None:
    """V10R3 refuses a create whose PartitionProcessorConfiguration lacks it (#1164).

    The live refusal was ``400 REST0001 Attribute 'schemaVersion' must appear on element
    'PartitionProcessorConfiguration'`` (#1161, P18). The modify fixture in
    tests/lpar/test_lpar_rmw.py, modelled on the live V10R3 read, carries
    ``schemaVersion="V1_0"`` on that element and on both processor configurations.
    """
    missing = [
        localname(el.tag)
        for el in _tree(xml).iter()
        if "Metadata" in _children(el) and "schemaVersion" not in el.attrib
    ]
    assert missing == []


# The create-relevant stretch of BasePartition.Group, and of the processor, shared and IO
# configuration groups, in the order of the XSDs a V10R3 HMC serves (#1161 P28-P30). The
# create refuses an out-of-order element with 400 REST0001.
SCHEMA_SEQUENCES = {
    "LogicalPartition": [
        "Metadata",
        "KeylockPosition",
        "PartitionID",
        "PartitionIOConfiguration",
        "PartitionMemoryConfiguration",
        "PartitionName",
        "PartitionProcessorConfiguration",
        "PartitionProfiles",
        "PartitionState",
        "PartitionType",
    ],
    "PartitionProcessorConfiguration": [
        "Metadata",
        "DedicatedProcessorConfiguration",
        "HasDedicatedProcessors",
        "SharedProcessorConfiguration",
        "SharingMode",
    ],
    "SharedProcessorConfiguration": [
        "Metadata",
        "DesiredProcessingUnits",
        "DesiredVirtualProcessors",
        "MaximumProcessingUnits",
        "MaximumVirtualProcessors",
        "MinimumProcessingUnits",
        "MinimumVirtualProcessors",
        "SharedProcessorPoolID",
        "SharedProcessorPoolName",
        "UncappedWeight",
    ],
    "DedicatedProcessorConfiguration": [
        "Metadata",
        "DesiredProcessors",
        "MaximumProcessors",
        "MinimumProcessors",
    ],
    "PartitionMemoryConfiguration": [
        "Metadata",
        "DesiredMemory",
        "MaximumMemory",
        "MinimumMemory",
    ],
    "PartitionIOConfiguration": ["Metadata", "MaximumVirtualIOSlots"],
}
FULL_SHARED = LparResources(
    min_memory=1,
    desired_memory=2,
    max_memory=3,
    min_procs=0.1,
    desired_procs=0.5,
    max_procs=1.0,
    min_vcpus=1,
    desired_vcpus=1,
    max_vcpus=2,
    uncapped=False,
)
FULL_DEDICATED = LparResources(
    desired_memory=2,
    dedicated=True,
    min_procs=1,
    desired_procs=1,
    max_procs=2,
    sharing_mode="keep_idle_procs",
)


@pytest.mark.parametrize("resources", [FULL_SHARED, FULL_DEDICATED])
def test_create_elements_follow_the_schema_sequence(resources) -> None:
    root = _tree(
        build_lpar_document(
            "p1",
            partition_id=3,
            resources=resources,
            keylock="normal",
            max_virtual_slots=64,
        )
    )
    for el in root.iter():
        sequence = SCHEMA_SEQUENCES.get(localname(el.tag))
        if sequence is not None:
            children = _children(el)
            assert set(children) <= set(sequence), localname(el.tag)
            assert _is_subsequence(children, sequence), localname(el.tag)
