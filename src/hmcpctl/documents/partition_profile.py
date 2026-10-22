"""Typed saved-profile edits and bounded, loss-aware recovery-copy projection."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # nosec B405 - serializes defusedxml-parsed elements
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

from .lpar import _REST_SHARING_MODES, SHARING_MODES

UOM = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
ATOM = "http://www.w3.org/2005/Atom"
N = "{" + UOM + "}"
ADAPTERS = frozenset(
    {
        "ProfileVirtualFibreChannelClientAdapter",
        "ProfileVirtualFibreChannelServerAdapter",
        "ProfileVirtualSCSIClientAdapter",
        "ProfileVirtualSCSIServerAdapter",
        "ProfileClientNetworkAdapter",
        "ProfileTrunkAdapter",
        "ProfileVirtualNICDedicated",
        "ProfileVirtualNICShared",
    }
)
ROOT_FIELDS = frozenset(
    [
        "Metadata",
        "AffinityGroupID",
        "AssignAllResources",
        "AssociatedPartition",
        "AutoStart",
        "BootMode",
        "ConnectionMonitoringEnabled",
        "DesiredProcessorCompatibilityMode",
        "HostEthernetAdapterLogicalPorts",
        "IOConfigurationInstance",
        "PowerControlPartitions",
        "ProcessorAttributes",
        "ProfileMemory",
        "ProfileName",
        "ProfileType",
        "RedundantErrorPathReportingEnabled",
        "SettingID",
        "SwitchNetworkInterfaceDeviceID",
        "ProfileSRIOVEthernetLogicalPorts",
        "ProfileSRIOVRoCELogicalPorts",
        "KVMReservedMemory",
        "PartitionPlacement",
    ]
)
CREATE = frozenset({"CUD", "CUR", "COD", "CUA", "COR"})
READONLY = frozenset({"ROR", "ROO", "UOD"})
# Unannotated collection/choice edges in the served profile grammar. This does not
# exempt ordinary fields from behavior admission, or turn a child into a create grant.
WRAPPERS = {
    ("ProfileIOSlots", "ProfileIOSlot"),
    ("RelatedIOAdapter", "IOAdapter"),
    ("RelatedIOAdapter", "PhysicalFibreChannelAdapter"),
    ("RelatedIOAdapter", "SRIOVAdapter"),
    ("ProfileVirtualIOAdapters", "ProfileVirtualIOAdapterSubclass"),
    *{("ProfileVirtualIOAdapterSubclass", a) for a in ADAPTERS},
    ("ProfileSRIOVEthernetLogicalPorts", "ProfileSRIOVEthernetLogicalPort"),
    ("ProfileSRIOVRoCELogicalPorts", "ProfileSRIOVRoCELogicalPort"),
    ("HostEthernetAdapterLogicalPorts", "HostEthernetAdapterLogicalPort"),
    ("AssociatedBackingDevices", "ProfileVirtualNICBackingDeviceChoice"),
    ("ProfileVirtualNICBackingDeviceChoice", "ProfileVirtualNICSRIOVBackingDevice"),
    ("AssociatedProfileVNICBackupDevice", "ProfileSRIOVVirtualNICBackupDevice"),
    ("PhysicalFibreChannelPorts", "PhysicalFibreChannelPort"),
    ("PhysicalVolumes", "PhysicalVolume"),
    ("PowerControlPartitions", "link"),
}
EXPECTED_BEHAVIOR = {
    ("LogicalPartitionProfile", "ProfileName"): "CUR",
    ("LogicalPartitionProfile", "SettingID"): "ROO",
    ("LogicalPartitionProfile", "AssociatedPartition"): "ROR",
    ("LogicalPartitionProfile", "ProfileType"): "ROR",
    ("LogicalPartitionProfile", "AssignAllResources"): "COD",
    ("ProfileMemory", "ManualEntitledModeEnabled"): "ROR",
    **{
        ("ProfileMemory", k): "CUD"
        for k in ("MinimumMemory", "DesiredMemory", "MaximumMemory")
    },
    **{
        ("ProfileMemory", k): "CUD"
        for k in (
            "DesiredEntitledMemory",
            "DesiredHugePageCount",
            "MaximumHugePageCount",
            "MinimumHugePageCount",
            "MemoryWeight",
        )
    },
    ("IOConfigurationInstance", "VirtualOpticonnectPool"): "CUD",
    ("SharedProcessorConfiguration", "UncappedWeight"): "CUD",
}
RECOVERY_TEXTS = {
    ("LogicalPartitionProfile", "ProfileName"),
    ("LogicalPartitionProfile", "SettingID"),
    ("LogicalPartitionProfile", "Metadata", "Atom", "AtomID"),
    ("LogicalPartitionProfile", "Metadata", "Atom", "AtomCreated"),
}


class ProfilePatch(BaseModel):
    """A bounded saved-profile patch; omitted fields are never injected."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: StrictStr | None = None
    boot_mode: (
        Literal[
            "Normal",
            "Diagnostic_With_Default_Boot_List",
            "Diagnostic_With_Stored_Boot_List",
            "Open_Firmware",
            "System_Management_Services",
            "Default",
        ]
        | None
    ) = None
    min_memory: StrictInt | None = Field(default=None, gt=0)
    desired_memory: StrictInt | None = Field(default=None, gt=0)
    max_memory: StrictInt | None = Field(default=None, gt=0)
    min_processors: StrictInt | None = Field(default=None, gt=0)
    desired_processors: StrictInt | None = Field(default=None, gt=0)
    max_processors: StrictInt | None = Field(default=None, gt=0)
    min_units: StrictFloat | None = Field(default=None, gt=0)
    desired_units: StrictFloat | None = Field(default=None, gt=0)
    max_units: StrictFloat | None = Field(default=None, gt=0)
    sharing_mode: StrictStr | None = None


@dataclass(frozen=True)
class ProfileDifference:
    path: str
    kind: Literal["added", "removed", "changed"]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


@dataclass(frozen=True)
class ProfileComparison:
    equal: bool
    complete: bool
    details_truncated: bool
    differences: tuple[ProfileDifference, ...]


def profile_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > 5 * 1024 * 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(
            "profile name must be nonblank bounded text without control characters"
        )
    return value


def one(root: ET.Element, path: str) -> ET.Element:
    nodes = [root]
    for part in path.split("/"):
        nodes = [c for n in nodes for c in n if c.tag == N + part]
        if len(nodes) != 1:
            raise ValueError(
                f"profile requires exactly one {path}; nothing was written"
            )
    return nodes[0]


def parse_profile(raw: str) -> ET.Element:
    if len(raw.encode()) > 32 * 1024 * 1024:
        raise ValueError("profile XML exceeds the response bound")
    try:
        root = DET.fromstring(raw)
    except (DET.ParseError, DefusedXmlException) as error:
        raise ValueError("profile response is not safe XML") from error
    if root.tag == N + "LogicalPartitionProfile":
        profile = root
    elif root.tag == "{" + ATOM + "}entry":
        nodes = root.findall("{" + ATOM + "}content/" + N + "LogicalPartitionProfile")
        if len(nodes) != 1:
            raise ValueError("profile entry must contain exactly one profile")
        profile = nodes[0]
    else:
        raise ValueError("expected a UOM profile or one Atom profile entry")
    one(profile, "ProfileName")
    if len({c.tag for c in profile}) != len(profile):
        raise ValueError("duplicate saved-profile root field")
    stack = [(profile, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 128 or count > 100000:
            raise ValueError(
                "profile nesting or element count exceeds comparison bounds"
            )
        stack.extend((child, depth + 1) for child in node)
    return profile


def _records(root: ET.Element, recovery: bool) -> dict[str, dict[str, Any]]:
    records = {}
    stack: list[tuple[ET.Element, str, tuple[str, ...], int]] = [(root, "", (), 0)]
    while stack:
        node, path, names, depth = stack.pop()
        if len(records) >= 100000 or depth > 128:
            raise ValueError("profile comparison exceeds element/depth bound")
        name = node.tag.removeprefix(N)
        names = (*names, name)
        path += "/" + node.tag + "[1]" if not path else ""
        text = node.text or ""
        if len(node) and not text.strip():
            text = ""
        if recovery and names in RECOVERY_TEXTS and not len(node):
            text = "<copy-root-scalar>"
        records[path] = {
            "text": text,
            "attributes": dict(sorted(node.attrib.items())),
            "children": [c.tag for c in node],
            "tail": node.tail if node.tail and node.tail.strip() else "",
        }
        ordinals: dict[str, int] = {}
        children = []
        for child in node:
            ordinals[child.tag] = ordinals.get(child.tag, 0) + 1
            children.append(
                (
                    child,
                    path + "/" + child.tag + f"[{ordinals[child.tag]}]",
                    names,
                    depth + 1,
                )
            )
        stack.extend(reversed(children))
    return records


def compare_profiles(
    before: ET.Element, after: ET.Element, *, recovery: bool = False
) -> ProfileComparison:
    try:
        left, right = _records(before, recovery), _records(after, recovery)
    except ValueError:
        return ProfileComparison(False, False, True, ())
    differences = []
    size = 0
    changed = False
    truncated = False
    for path in dict.fromkeys((*left, *right)):
        a, b = left.get(path), right.get(path)
        if a == b:
            continue
        changed = True
        kind: Literal["added", "removed", "changed"] = (
            "added" if a is None else "removed" if b is None else "changed"
        )
        item = ProfileDifference(path, kind, a, b)
        encoded = len(
            json.dumps({"path": path, "kind": kind, "before": a, "after": b}).encode()
        )
        if len(differences) >= 100 or size + encoded > 32768:
            truncated = True
        else:
            differences.append(item)
            size += encoded
    return ProfileComparison(not changed, True, truncated, tuple(differences))


def _validate_choices(node: ET.Element) -> None:
    if node.tag == N + "ProfileVirtualIOAdapterSubclass" and (
        len(node) > 1 or any(c.tag not in {N + a for a in ADAPTERS} for c in node)
    ):
        raise ValueError(
            "profile adapter wrapper requires zero or one supported alternative"
        )
    if node.tag in {N + "ProfileVirtualNICDedicated", N + "ProfileVirtualNICShared"}:
        one(node, "Details")
        if node.tag == N + "ProfileVirtualNICDedicated":
            one(node, "AssociatedBackingDevices")


def clone_profile_document(source: ET.Element, name: str) -> ET.Element:
    profile_name(name)
    if source.tag != N + "LogicalPartitionProfile":
        raise ValueError("recovery source must be a UOM profile")
    root = deepcopy(source)
    if any(c.tag not in {N + f for f in ROOT_FIELDS} for c in root):
        raise ValueError("uncaptured profile root field; recovery copy refused")
    one(root, "ProfileName").text = name
    one(root, "SettingID")
    one(root, "Metadata/Atom/AtomID")
    one(root, "Metadata/Atom/AtomCreated")

    def project(parent: ET.Element, depth: int) -> None:
        if depth > 128:
            raise ValueError("profile projection nesting exceeds bound")
        _validate_choices(parent)
        for child in list(parent):
            if not child.tag.startswith(N):
                raise ValueError("unknown profile configuration namespace")
            local = child.tag.removeprefix(N)
            if local == "Metadata":
                atom = one(child, "Atom")
                if (
                    len(child) != 1
                    or any(
                        c.tag not in {N + "AtomID", N + "AtomCreated"} or len(c)
                        for c in atom
                    )
                    or len({c.tag for c in atom}) != len(atom)
                ):
                    raise ValueError(
                        "uncaptured profile metadata shape; recovery copy refused"
                    )
                if parent is root:
                    atom = one(child, "Atom")
                    for key in ("AtomID", "AtomCreated"):
                        atom.remove(one(atom, key))
                continue
            token = child.get("kb")
            edge = (parent.tag.removeprefix(N), local)
            expected = EXPECTED_BEHAVIOR.get(edge)
            if expected is not None and token != expected:
                raise ValueError(
                    "profile behavior conflicts with captured field annotation"
                )
            if token in READONLY:
                parent.remove(child)
            elif token in CREATE or (token is None and edge in WRAPPERS):
                project(child, depth + 1)
            else:
                raise ValueError(
                    "profile behavior annotation missing or unknown; recovery copy refused"
                )

    project(root, 0)
    return root


def apply_profile_patch(source: ET.Element, patch: ProfilePatch) -> ET.Element:
    values = patch.model_dump(exclude_none=True)
    if not values:
        raise ValueError("profile patch requires at least one typed value")
    result = deepcopy(source)
    mapping = {
        "name": "ProfileName",
        "boot_mode": "BootMode",
        "sharing_mode": "ProcessorAttributes/SharingMode",
    }
    mapping.update(
        {
            k + "_memory": "ProfileMemory/" + v + "Memory"
            for k, v in [("min", "Minimum"), ("desired", "Desired"), ("max", "Maximum")]
        }
    )
    dedicated = one(source, "ProcessorAttributes/HasDedicatedProcessors").text
    if dedicated not in {"true", "false"}:
        raise ValueError("profile processor mode is unavailable or malformed")
    cpu = (
        "DedicatedProcessorConfiguration"
        if dedicated == "true"
        else "SharedProcessorConfiguration"
    )
    suffix = "Processors" if dedicated == "true" else "VirtualProcessors"
    for key, word in [("min", "Minimum"), ("desired", "Desired"), ("max", "Maximum")]:
        mapping[key + "_processors"] = f"ProcessorAttributes/{cpu}/{word}{suffix}"
        mapping[key + "_units"] = (
            f"ProcessorAttributes/SharedProcessorConfiguration/{word}ProcessingUnits"
        )
    for key, value in values.items():
        if key == "name":
            value = profile_name(value)
        if key == "sharing_mode":
            if (
                value not in SHARING_MODES
                or (dedicated == "false" and value not in {"capped", "uncapped"})
                or (dedicated == "true" and value in {"capped", "uncapped"})
            ):
                raise ValueError("sharing mode conflicts with the saved processor mode")
            value = _REST_SHARING_MODES[value]
        if key.endswith("_units") and dedicated == "true":
            raise ValueError(
                "shared processing units cannot patch dedicated processors"
            )
        element = one(result, mapping[key])
        if element.get("kb") not in {"CUD", "CUR", "CUA"}:
            raise ValueError("typed profile field is not updateable")
        element.text = str(value)
    groups = [
        ("min_memory", "desired_memory", "max_memory"),
        ("min_processors", "desired_processors", "max_processors"),
    ]
    if dedicated == "false":
        groups.append(("min_units", "desired_units", "max_units"))
    for group in groups:
        if not any(k in values for k in group):
            continue
        try:
            numbers = [Decimal(one(result, mapping[k]).text or "") for k in group]
        except InvalidOperation as error:
            raise ValueError("saved resource range is malformed") from error
        if (
            not all(n.is_finite() and n > 0 for n in numbers)
            or not numbers[0] <= numbers[1] <= numbers[2]
        ):
            raise ValueError("saved profile requires positive min <= desired <= max")
    if dedicated == "false" and any(
        k.endswith(("_units", "_processors")) for k in values
    ):
        for key in ("min", "desired", "max"):
            if Decimal(one(result, mapping[key + "_units"]).text or "") > Decimal(
                one(result, mapping[key + "_processors"]).text or ""
            ):
                raise ValueError("processing units cannot exceed virtual processors")
    return result
