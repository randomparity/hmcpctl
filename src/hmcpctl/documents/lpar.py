from __future__ import annotations

import math
import xml.etree.ElementTree as ET  # nosec B405 - reads an element the caller parsed with defusedxml
from dataclasses import dataclass, field, fields
from typing import Literal, get_args

from pydantic import ConfigDict, StrictBool, StrictInt

from ..xmlutil import escapes_string_arguments
from .common import UOM_NS, document_envelope

# The types a LogicalPartition create accepts. V10R3 answers a LogicalPartition PUT typed
# Virtual IO Server with 500 REST0140 (#1179); a VIOS is created through its own collection.
PARTITION_TYPES: tuple[PartitionType, ...] = ("AIX/Linux", "OS400")
_VIOS_PARTITION_TYPE = "Virtual IO Server"
# The creatable values of KeylockPosition.Enum in the V10R3 schema (#1161 P39).
KEYLOCK_POSITIONS = ("normal", "manual")
PartitionType = Literal["AIX/Linux", "OS400"]
Keylock = Literal["normal", "manual"]
SharingMode = Literal[
    "capped",
    "uncapped",
    "keep_idle_procs",
    "share_idle_procs",
    "share_idle_procs_active",
    "share_idle_procs_always",
]
SHARING_MODES = frozenset(get_args(SharingMode))
# LogicalPartitionProcessorSharingMode.Enum in the live V10R3 schema; "proces" is the HMC's own
# spelling (#1164).
_REST_SHARING_MODES: dict[str, str] = {
    "capped": "capped",
    "uncapped": "uncapped",
    "keep_idle_procs": "keep idle procs",
    "share_idle_procs": "sre idle proces",
    "share_idle_procs_active": "sre idle procs active",
    "share_idle_procs_always": "sre idle procs always",
}


def validate_partition_type(partition_type: str) -> PartitionType:
    """Return *partition_type* if a LogicalPartition create can make it, else refuse."""
    if partition_type == _VIOS_PARTITION_TYPE:
        raise ValueError(
            "partition_type 'Virtual IO Server' cannot be created as a LogicalPartition: "
            "the HMC refuses it (HTTP 500 REST0140). Create a Virtual I/O Server through "
            "the VIOS path instead (MCP tool hmc_create_vios, operations.vios.create_vios). "
            "Nothing was created."
        )
    if partition_type not in PARTITION_TYPES:
        raise ValueError(
            f"partition_type must be one of: {', '.join(PARTITION_TYPES)}; got "
            f"{partition_type!r}. Nothing was created."
        )
    return partition_type


def validate_keylock(keylock: str | None) -> None:
    """Refuse a create keylock outside the V10R3 ``KeylockPosition`` enumeration."""
    if keylock is not None and keylock not in KEYLOCK_POSITIONS:
        raise ValueError(
            f"keylock must be one of: {', '.join(KEYLOCK_POSITIONS)}; got {keylock!r}. "
            "Nothing was created."
        )


def lpar_envelope(body: str) -> str:
    return document_envelope("LogicalPartition", body)


@dataclass(frozen=True)
class LparSettings:
    """Optional C01 settings; None preserves HMC defaults/current configuration.

    A running time_ref partition can govern hypervisor/service-processor time.
    These configure capabilities, not a guest clock or migration/restart readiness.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")
    allow_perf_collection: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Allow performance data collection; omitted retains the HMC default/current value."
        },
    )
    lpar_avail_priority: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Partition availability priority, integer 0–255; zero is explicit."
        },
    )
    time_ref: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Whether the partition is a time reference; a running reference can govern hypervisor/service-processor time."
        },
    )
    simplified_remote_restart_capable: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Allow remote restart without reserved storage on AIX/Linux or IBM i; does not prove restart readiness."
        },
    )
    migration_disabled: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Disable partition migration on AIX/Linux or IBM i; does not perform a migration."
        },
    )

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            if item.name == "lpar_avail_priority":
                if type(value) is not int or not 0 <= value <= 255:
                    raise ValueError(
                        "lpar_avail_priority must be an integer from 0 to 255"
                    )
            elif type(value) is not bool:
                raise ValueError(f"{item.name} must be a boolean")

    def pairs(self) -> list[tuple[str, object]]:
        """Supplied values in the documented 0/1 boolean grammar."""
        return [
            (item.name, int(value))
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        ]


@dataclass(frozen=True)
class LparReportingSettings:
    """C02 reporting fields; None omits, False/zero/"none" are explicit.

    Work-group IDs have no numeric range in the cited command sections; HMC
    admission decides whether an otherwise well-formed integer is supported.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")
    ipl_source: Literal["a", "b", "c", "d"] | None = field(
        default=None,
        metadata={
            "description": "IBM i IPL source a, b, c or d; partition update only. Omitted preserves the current value."
        },
    )
    os400_restricted_io_mode: StrictBool | None = field(
        default=None,
        metadata={
            "description": "IBM i restricted I/O mode; strict boolean for creation/update, omitted preserves defaults/current value."
        },
    )
    work_group_id: StrictInt | Literal["none"] | None = field(
        default=None,
        metadata={
            "description": "EWLM integer work-group ID or none for no EWLM; no numeric range is supplied by the cited contract. Zero is explicit; omitted preserves defaults/current value."
        },
    )
    electronic_err_reporting: StrictBool | None = field(
        default=None,
        metadata={
            "description": "IBM i electronic error reporting; strict boolean for creation/update, omitted preserves defaults/current value."
        },
    )
    redundant_err_path_reporting: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Redundant error-path reporting; strict boolean, partition creation only. Refused on partition update; omitted retains the HMC default."
        },
    )

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            if item.name == "ipl_source":
                valid = type(value) is str and value in {"a", "b", "c", "d"}
            elif item.name == "work_group_id":
                valid = type(value) is int or (type(value) is str and value == "none")
            else:
                valid = type(value) is bool
            if not valid:
                raise ValueError(
                    f"Invalid {item.name}: use the documented typed reporting value"
                )

    def pairs(self) -> list[tuple[str, object]]:
        """Return supplied fields in command order, preserving sentinel and zero."""
        return [
            (item.name, int(value) if type(value) is bool else value)
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        ]

    def requires_ibmi(self) -> bool:
        return any(
            getattr(self, name) is not None
            for name in (
                "ipl_source",
                "os400_restricted_io_mode",
                "electronic_err_reporting",
            )
        )

    def validate_context(
        self, context: Literal["create", "update"], partition_type: str | None = None
    ) -> None:
        unsupported = (
            "ipl_source" if context == "create" else "redundant_err_path_reporting"
        )
        if getattr(self, unsupported) is not None:
            raise ValueError(f"{unsupported} is not supported in partition {context}")
        if (
            partition_type is not None
            and self.requires_ibmi()
            and partition_type != "OS400"
        ):
            raise ValueError(
                "IPL source, restricted I/O and electronic reporting require IBM i (OS400)"
            )


@dataclass(frozen=True)
class LparResources:
    min_memory: int | None = field(
        default=None, metadata={"description": "Minimum memory in MiB."}
    )
    desired_memory: int | None = field(
        default=None, metadata={"description": "Desired memory in MiB."}
    )
    max_memory: int | None = field(
        default=None, metadata={"description": "Maximum memory in MiB."}
    )
    dedicated: bool | None = field(
        default=None,
        metadata={
            "description": "Whether processors are dedicated rather than shared. On a "
            "modify it must match the partition's current mode; a switch is refused."
        },
    )
    min_procs: float | None = field(
        default=None,
        metadata={
            "description": "Minimum whole CPUs when dedicated, or processing units when shared."
        },
    )
    desired_procs: float | None = field(
        default=None,
        metadata={
            "description": "Desired whole CPUs when dedicated, or processing units when shared."
        },
    )
    max_procs: float | None = field(
        default=None,
        metadata={
            "description": "Maximum whole CPUs when dedicated, or processing units when shared."
        },
    )
    min_vcpus: int | None = field(
        default=None,
        metadata={
            "description": "Minimum virtual processor count for a shared partition."
        },
    )
    desired_vcpus: int | None = field(
        default=None,
        metadata={
            "description": "Desired virtual processor count for a shared partition."
        },
    )
    max_vcpus: int | None = field(
        default=None,
        metadata={
            "description": "Maximum virtual processor count for a shared partition."
        },
    )
    sharing_mode: SharingMode | None = field(
        default=None, metadata={"description": "HMC processor sharing mode."}
    )
    uncapped: bool | None = field(
        default=None,
        metadata={
            "description": "Whether a shared partition may consume spare processing capacity."
        },
    )


def _memory_config(resources: LparResources) -> str:
    fields = (
        ("DesiredMemory", resources.desired_memory),
        ("MaximumMemory", resources.max_memory),
        ("MinimumMemory", resources.min_memory),
    )
    if all(value is None for _, value in fields):
        return ""
    parts = [
        '  <PartitionMemoryConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">',
        "    <Metadata><Atom/></Metadata>",
    ]
    parts.extend(
        f'    <{name} kb="CUD" kxe="false">{value}</{name}>'
        for name, value in fields
        if value is not None
    )
    return "\n".join([*parts, "  </PartitionMemoryConfiguration>"])


def _validate_sharing_mode(value: SharingMode | None) -> None:
    if value is not None and (not isinstance(value, str) or value not in SHARING_MODES):
        raise ValueError(
            f"sharing_mode must be one of: {', '.join(sorted(SHARING_MODES))}"
        )


def _render_units(value: float) -> str:
    return str(int(value) if isinstance(value, float) and value.is_integer() else value)


def _shared_sharing_mode(resources: LparResources) -> str | None:
    return (
        "uncapped"
        if resources.uncapped is True
        else resources.sharing_mode
        or ("capped" if resources.uncapped is False else None)
    )


def shared_vcpu_defaults(resources: LparResources) -> tuple[int, int, int]:
    """Return a shared create's (min, desired, max) vcpus after the mksyscfg defaults."""
    desired = resources.desired_vcpus or 1
    return resources.min_vcpus or 1, desired, resources.max_vcpus or max(desired, 2)


_SHARED_LEVELS = (
    ("min", "--min-procs", "--min-vcpus"),
    ("desired", "--procs", "--vcpus"),
    ("max", "--max-procs", "--max-vcpus"),
)


def shared_units_over_vcpus(resources: LparResources) -> str | None:
    """Describe explicit shared processing units that exceed their level's vcpus.

    A virtual processor uses at most 1.0 processing unit (#1034). Omitted vcpus
    count as :func:`shared_vcpu_defaults`; omitted units are not checked.
    """
    units = (resources.min_procs, resources.desired_procs, resources.max_procs)
    over = [
        f"{level}_procs={value} exceeds {level}_vcpus={vcpus} "
        f"(lower {procs_opt} or raise {vcpus_opt} on the CLI)"
        for (level, procs_opt, vcpus_opt), value, vcpus in zip(
            _SHARED_LEVELS, units, shared_vcpu_defaults(resources), strict=True
        )
        if value is not None and value > vcpus
    ]
    if not over:
        return None
    return (
        "a virtual processor uses at most 1.0 processing unit: "
        + "; ".join(over)
        + ". Omitted vcpus default to min 1, desired 1, max max(desired, 2)."
    )


def _dedicated_processor_body(resources: LparResources) -> list[str]:
    parts = [
        '    <DedicatedProcessorConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">',
        "      <Metadata><Atom/></Metadata>",
    ]
    for name, value in (
        ("DesiredProcessors", resources.desired_procs),
        ("MaximumProcessors", resources.max_procs),
        ("MinimumProcessors", resources.min_procs),
    ):
        if value is not None:
            parts.append(f'      <{name} kb="CUD" kxe="false">{int(value)}</{name}>')
    parts.extend(
        (
            "    </DedicatedProcessorConfiguration>",
            '    <HasDedicatedProcessors kb="CUD" kxe="false">true</HasDedicatedProcessors>',
        )
    )
    if resources.sharing_mode:
        parts.append(
            '    <SharingMode kb="CUD" kxe="false">'
            f"{_REST_SHARING_MODES[resources.sharing_mode]}</SharingMode>"
        )
    return parts


def _shared_processor_body(resources: LparResources) -> list[str]:
    # The V10R3 create refuses a document that does not state the mode (REST0126 proc_mode).
    parts = [
        '    <HasDedicatedProcessors kb="CUD" kxe="false">false</HasDedicatedProcessors>',
        '    <SharedProcessorConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">',
        "      <Metadata><Atom/></Metadata>",
    ]
    # The SharedProcessorConfiguration sequence of the V10R3 XSD; the create refuses an
    # out-of-order element (#1164).
    for name, value in (
        ("DesiredProcessingUnits", resources.desired_procs),
        ("DesiredVirtualProcessors", resources.desired_vcpus),
        ("MaximumProcessingUnits", resources.max_procs),
        ("MaximumVirtualProcessors", resources.max_vcpus),
        ("MinimumProcessingUnits", resources.min_procs),
        ("MinimumVirtualProcessors", resources.min_vcpus),
    ):
        if value is not None:
            parts.append(
                f'      <{name} kb="CUD" kxe="false">{_render_units(value)}</{name}>'
            )
    if resources.uncapped is False:
        parts.append('      <UncappedWeight kb="CUD" kxe="false">0</UncappedWeight>')
    parts.append("    </SharedProcessorConfiguration>")
    mode = _shared_sharing_mode(resources)
    if mode:
        parts.append(
            f'    <SharingMode kb="CUD" kxe="false">{_REST_SHARING_MODES[mode]}</SharingMode>'
        )
    return parts


def _processor_config(resources: LparResources) -> str:
    _validate_sharing_mode(resources.sharing_mode)
    if not any(
        value is not None
        for value in (
            resources.min_procs,
            resources.desired_procs,
            resources.max_procs,
            resources.min_vcpus,
            resources.desired_vcpus,
            resources.max_vcpus,
        )
    ):
        return ""
    if resources.dedicated is not True and (
        refusal := shared_units_over_vcpus(resources)
    ):
        raise ValueError(refusal)
    body = (
        _dedicated_processor_body
        if resources.dedicated is True
        else _shared_processor_body
    )
    return "\n".join(
        [
            '  <PartitionProcessorConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">',
            "    <Metadata><Atom/></Metadata>",
            *body(resources),
            "  </PartitionProcessorConfiguration>",
        ]
    )


@escapes_string_arguments
def build_lpar_document(
    name: str,
    partition_type: PartitionType = "AIX/Linux",
    partition_id: int | None = None,
    resources: LparResources | None = None,
    keylock: Keylock | None = None,
    max_virtual_slots: int | None = None,
) -> str:
    """Build a LogicalPartition document to PUT (create).

    `name` is required; the rest are optional (the HMC supplies defaults).
    `resources` carries the memory/processor fields; None means no resource
    block is emitted. A modify goes through :func:`partition_updates` instead:
    V10R3 rejects a sparse LogicalPartition POST, and ``PartitionType`` is
    create-only.

    ``OperatingSystemType`` is never sent: the schema marks it read-only
    (``kb="ROR"``) and the HMC sets ``AIX/Linux`` itself (#1179). A Virtual
    I/O Server is built by :func:`build_vios_document`.
    keylock: initial keylock position — ``normal`` or ``manual``.
    max_virtual_slots: maximum number of virtual I/O slots.
    """
    validate_partition_type(partition_type)
    return lpar_envelope(
        _partition_body(
            name,
            partition_type,
            partition_id,
            resources,
            keylock,
            max_virtual_slots,
        )
    )


def _partition_body(
    name: str,
    partition_type: PartitionType | Literal["Virtual IO Server"],
    partition_id: int | None = None,
    resources: LparResources | None = None,
    keylock: Keylock | None = None,
    max_virtual_slots: int | None = None,
) -> str:
    """The children of a partition create document, in schema order."""
    validate_keylock(keylock)

    resources = resources or LparResources()

    # Children follow the BasePartition.Group sequence of the live V10R3 schema: the
    # HMC refuses an out-of-order element with REST0001 (#1164).
    body_parts = ["  <Metadata><Atom/></Metadata>"]
    if keylock is not None:
        body_parts.append(
            f'  <KeylockPosition kb="CUD" kxe="false">{keylock}</KeylockPosition>'
        )
    if partition_id is not None:
        body_parts.append(
            f'  <PartitionID kb="COD" kxe="false">{partition_id}</PartitionID>'
        )
    if max_virtual_slots is not None:
        body_parts.extend(
            (
                '  <PartitionIOConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">',
                "    <Metadata><Atom/></Metadata>",
                (
                    f'    <MaximumVirtualIOSlots kb="CUD" kxe="false">{max_virtual_slots}'
                    "</MaximumVirtualIOSlots>"
                ),
                "  </PartitionIOConfiguration>",
            )
        )
    mem = _memory_config(resources)
    if mem:
        body_parts.append(mem)
    body_parts.append(f'  <PartitionName kb="CUR" kxe="false">{name}</PartitionName>')
    proc = _processor_config(resources)
    if proc:
        body_parts.append(proc)
    body_parts.append(
        f'  <PartitionType kb="COD" kxe="false">{partition_type}</PartitionType>'
    )

    return "\n".join(body_parts)


VIOS_DEFAULT_RESOURCES = LparResources(
    min_memory=512,
    desired_memory=4096,
    max_memory=8192,
    desired_vcpus=2,
    min_vcpus=1,
    max_vcpus=4,
    desired_procs=0.5,
    min_procs=0.1,
    max_procs=1.0,
    uncapped=True,
)


@escapes_string_arguments
def build_vios_document(
    name: str,
    resources: LparResources = VIOS_DEFAULT_RESOURCES,
) -> str:
    """Build a VirtualIOServer document for creating a Virtual IO Server.

    Carries the same children as :func:`build_lpar_document` with
    partition_type='Virtual IO Server' and shared-processor defaults for VIOS
    provisioning, under a ``VirtualIOServer`` root: V10R3 refuses a VIOS sent as
    a LogicalPartition with 500 REST0140 (#1214).
    """
    return document_envelope(
        "VirtualIOServer",
        _partition_body(name, "Virtual IO Server", resources=resources),
    )


_PPC = "PartitionProcessorConfiguration"
_RESOURCE_NUMBERS = (
    "min_memory",
    "desired_memory",
    "max_memory",
    "min_procs",
    "desired_procs",
    "max_procs",
    "min_vcpus",
    "desired_vcpus",
    "max_vcpus",
)
_MODE_SWITCH_REFUSAL = (
    "Refusing to switch the partition between dedicated and shared processors: the "
    "partition read carries no configuration for the other mode, so nothing was written. "
    "Omit `dedicated`, or pass the partition's current mode."
)


def _dedicated_updates(resources: LparResources) -> dict[str, str]:
    if resources.uncapped is not None or any(
        value is not None
        for value in (resources.min_vcpus, resources.desired_vcpus, resources.max_vcpus)
    ):
        raise ValueError(
            "Virtual processor counts and capping apply only to a shared-processor "
            "partition; this partition has dedicated processors. Nothing was written."
        )
    fields = (
        ("DesiredProcessors", resources.desired_procs),
        ("MaximumProcessors", resources.max_procs),
        ("MinimumProcessors", resources.min_procs),
    )
    for name, value in fields:
        if value is not None and not float(value).is_integer():
            raise ValueError(
                f"A dedicated-processor partition takes whole CPUs; {name}={value} is not "
                "a whole number. Nothing was written."
            )
    config = f"{_PPC}/DedicatedProcessorConfiguration"
    return {
        f"{config}/{name}": str(int(value))
        for name, value in fields
        if value is not None
    }


def _shared_updates(resources: LparResources) -> dict[str, str]:
    config = f"{_PPC}/SharedProcessorConfiguration"
    units = (
        ("DesiredProcessingUnits", resources.desired_procs),
        ("MaximumProcessingUnits", resources.max_procs),
        ("MinimumProcessingUnits", resources.min_procs),
    )
    vcpus = (
        ("DesiredVirtualProcessors", resources.desired_vcpus),
        ("MaximumVirtualProcessors", resources.max_vcpus),
        ("MinimumVirtualProcessors", resources.min_vcpus),
    )
    updates = {
        f"{config}/{name}": _render_units(v) for name, v in units if v is not None
    }
    updates |= {f"{config}/{name}": str(v) for name, v in vcpus if v is not None}
    return updates


def _processor_updates(lpar: ET.Element, resources: LparResources) -> dict[str, str]:
    _validate_sharing_mode(resources.sharing_mode)
    current = lpar.findtext(f"{{{UOM_NS}}}{_PPC}/{{{UOM_NS}}}HasDedicatedProcessors")
    dedicated = current == "true" if current is not None else bool(resources.dedicated)
    if resources.dedicated is not None and resources.dedicated != dedicated:
        raise ValueError(_MODE_SWITCH_REFUSAL)
    if dedicated:
        updates = _dedicated_updates(resources)
        mode = resources.sharing_mode
    else:
        updates = _shared_updates(resources)
        mode = _shared_sharing_mode(resources)
    if mode:
        updates[f"{_PPC}/SharingMode"] = _REST_SHARING_MODES[mode]
    if updates:
        updates[f"{_PPC}/HasDedicatedProcessors"] = str(dedicated).lower()
    return updates


def partition_updates(
    lpar: ET.Element,
    *,
    name: str | None = None,
    resources: LparResources | None = None,
) -> dict[str, str]:
    """Map a rename or resource change to the partition elements it sets.

    Returns element text keyed by a slash path relative to *lpar*, the
    ``LogicalPartition`` element of a whole-partition read, for
    ``HMCClient.update_logical_partition``. Processor paths follow the
    partition's current mode (``HasDedicatedProcessors``); a requested
    ``dedicated`` value that differs from it raises ``ValueError``, because
    switching needs a configuration element the read does not carry.
    ``UncappedWeight`` is never written: V10R3 drops it on capping and recreates
    it as 0 on uncapping.
    """
    resources = resources or LparResources()
    for number in _RESOURCE_NUMBERS:
        value = getattr(resources, number)
        if value is not None and not (math.isfinite(value) and value >= 0):
            raise ValueError(
                f"{number}={value} must be a finite, non-negative number. Nothing was written."
            )
    memory = (
        ("DesiredMemory", resources.desired_memory),
        ("MaximumMemory", resources.max_memory),
        ("MinimumMemory", resources.min_memory),
    )
    updates = {} if name is None else {"PartitionName": name}
    updates |= {
        f"PartitionMemoryConfiguration/{field_name}": str(value)
        for field_name, value in memory
        if value is not None
    }
    return updates | _processor_updates(lpar, resources)
