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

from .lpar import _REST_SHARING_MODES, SHARING_MODES, SharingMode

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
        "ElectronicErrorReportingEnabled",
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
    ("RelatedIOAdapter", "EthernetBackingDevice"),
    ("RelatedIOAdapter", "HostChannelAdapter"),
    ("RelatedIOAdapter", "HostEthernetAdapterLogicalPort"),
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
# Fixed behavior facts from the retained served profile schemas (ADR0242).
# Unknown annotated fields remain opaque; contradictions of captured fields refuse.
CAPTURED_BEHAVIOR = {
    "AssociatedIOSlot": {
        "BusGroupingRequired": "CUD",
        "Description": "CUD",
        "FeatureCodes": "ROO",
        "IOBusID": "ROR",
        "IORDevices": "ROO",
        "IOUnitPhysicalLocation": "ROR",
        "PCAdapterID": "ROO",
        "PCIClass": "ROO",
        "PCIDeviceID": "ROO",
        "PCIManufacturerID": "ROO",
        "PCIRevisionID": "ROO",
        "PCISubsystemDeviceID": "ROO",
        "PCISubsystemVendorID": "ROO",
        "PCIVendorID": "ROO",
        "ParentSlotDynamicReconfigurationConnectorIndex": "ROO",
        "PartitionID": "ROO",
        "PartitionName": "ROO",
        "PartitionType": "ROO",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "PartnerSlotDynamicReconfigurationConnectorIndex": "ROR",
        "PartnerSlotDynamicReconfigurationConnectorName": "CUD",
        "RelatedIBMiIOSlot": "CUD",
        "RelatedIOAdapter": "CUD",
        "SRIOVCapableDevice": "ROO",
        "SRIOVCapableSlot": "ROO",
        "SRIOVLogicalPortsLimit": "ROO",
        "SlotDynamicReconfigurationConnectorIndex": "ROR",
        "SlotDynamicReconfigurationConnectorName": "CUD",
        "SlotPhysicalLocationCode": "ROR",
        "TwinaxialAttached": "ROO",
        "TwinaxialCapable": "ROO",
        "VitalProductDataModel": "ROO",
        "VitalProductDataSerialNumber": "ROO",
        "VitalProductDataStale": "ROO",
        "VitalProductDataType": "ROO",
    },
    "Capability": {
        "HighPercentage": "CUD",
        "LowPercentage": "CUD",
        "MediumPercentage": "CUD",
    },
    "DedicatedProcessorConfiguration": {
        "DesiredProcessors": "CUD",
        "MaximumProcessors": "CUD",
        "MinimumProcessors": "CUD",
    },
    "Details": {
        "AllowedOperatingSystemMACAddresses": "CUD",
        "AllowedVLANIDs": "CUD",
        "AutoPriorityFailover": "CUD",
        "LparName": "CUD",
        "MACAddress": "CUD",
        "PortVLANID": "CUD",
        "PortVLANIDPriority": "CUD",
    },
    "EthernetBackingDevice": {
        "AdapterID": "ROR",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "IPInterface": "CUD",
        "LogicalPartitionAssignmentCapable": "ROO",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "PhysicalLocation": "ROR",
        "UniqueDeviceID": "ROR",
    },
    "HostChannelAdapter": {
        "AdapterID": "ROR",
        "AllocationAllowed": "CUR",
        "AssignedUUIDs": "CUD",
        "CableType": "ROR",
        "Capability": "CUR",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "IsFunctional": "ROR",
        "LogicalPartitionAssignmentCapable": "ROO",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "PhysicalLocation": "ROR",
        "PhysicalLocationCode": "ROR",
        "UUIDListSequential": "ROR",
        "UnassignedGUIDs": "CUD",
        "UniqueDeviceID": "ROR",
    },
    "HostEthernetAdapterLogicalPort": {
        "AdapterID": "ROR",
        "AllowedOSMACAddresses": "CUD",
        "AllowedVLANIDs": "CUD",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "HEALogicalPortPhysicalLocation": "ROR",
        "IPInterface": "CUD",
        "LogicalPartitionAssignmentCapable": "ROO",
        "LogicalPortID": "CUD",
        "MACAddress": "CUD",
        "MACAddressDirectives": "CUD",
        "PartitionLink": "CUR",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "PhysicalLocation": "ROR",
        "PhysicalPortID": "ROR",
        "PortGroupID": "CUD",
        "PortState": "ROR",
        "UniqueDeviceID": "ROR",
    },
    "IOAdapter": {
        "AdapterID": "ROR",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "LogicalPartitionAssignmentCapable": "ROO",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "PhysicalLocation": "ROR",
        "UniqueDeviceID": "ROR",
    },
    "IOConfigurationInstance": {
        "HighSpeedLinkOpticonnectPool": "CUD",
        "HostChannelAdapters": "ROR",
        "MaximumVirtualIOSlots": "CUD",
        "PoolIDs": "CUD",
        "ProfileIOSlots": "CUD",
        "ProfileVirtualIOAdapters": "CUD",
        "TaggedIO": "CUD",
        "VirtualOpticonnectPool": "CUD",
    },
    "IPInterface": {
        "Gateway": "CUD",
        "HostName": "CUD",
        "IPAddress": "CUD",
        "IPV6Prefix": "CUD",
        "InterfaceName": "CUD",
        "State": "CUD",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "SubnetMask": "CUD",
    },
    "LogicalPartitionProfile": {
        "AffinityGroupID": "CUD",
        "AssignAllResources": "COD",
        "AssociatedPartition": "ROR",
        "AutoStart": "CUD",
        "BootMode": "CUD",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "ConnectionMonitoringEnabled": "CUD",
        "DesiredProcessorCompatibilityMode": "CUD",
        "ElectronicErrorReportingEnabled": "CUD",
        "HostEthernetAdapterLogicalPorts": "CUD",
        "IOConfigurationInstance": "CUD",
        "KVMReservedMemory": "CUD",
        "PowerControlPartitions": "CUD",
        "ProcessorAttributes": "CUR",
        "ProfileMemory": "CUR",
        "ProfileName": "CUR",
        "ProfileSRIOVEthernetLogicalPorts": "CUD",
        "ProfileSRIOVRoCELogicalPorts": "CUD",
        "ProfileType": "ROR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "RedundantErrorPathReportingEnabled": "CUD",
        "SettingID": "ROO",
        "SwitchNetworkInterfaceDeviceID": "CUD",
    },
    "PhysicalFibreChannelAdapter": {
        "AdapterID": "ROR",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "LogicalPartitionAssignmentCapable": "ROO",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "PhysicalFibreChannelPorts": "CUD",
        "PhysicalLocation": "ROR",
        "UniqueDeviceID": "ROR",
    },
    "PhysicalFibreChannelPort": {
        "AvailablePorts": "ROR",
        "Label": "CUD",
        "LocationCode": "ROR",
        "PhysicalVolumes": "CUD",
        "PortName": "CUR",
        "TotalPorts": "ROR",
        "UniqueDeviceID": "ROR",
        "WWNN": "ROO",
        "WWPN": "CUR",
    },
    "ProcessorAttributes": {
        "DedicatedProcessorConfiguration": "CUD",
        "HasDedicatedProcessors": "CUD",
        "SharedProcessorConfiguration": "CUD",
        "SharingMode": "CUD",  # live-vocabulary: allow captured schema behavior annotation, not a field value
    },
    "ProfileClientNetworkAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "AllowedOperatingSystemMACAddresses": "CUD",
        "IsQualityOfServicePriorityEnabled": "CUD",
        "IsRequired": "CUD",
        "IsTaggedVLANSupported": "CUA",
        "LocalPartitionID": "CUR",
        "MaCAddressValue": "CUD",
        "PortVLANID": "CUR",
        "QualityOfServicePriority": "CUD",
        "TaggedVLANIDs": "CUA",
        "VirtualSlotNumber": "CUR",
        "VirtualSwitchID": "ROR",
        "VirtualSwitchInterfaceManagerID": "CUD",
        "VirtualSwitchInterfaceType": "CUD",
        "VirtualSwitchInterfaceTypeVersion": "CUD",
        "VirtualSwitchName": "CUD",
    },
    "ProfileIOSlot": {"AssociatedIOSlot": "CUD", "IsRequired": "CUD", "PoolID": "CUD"},
    "ProfileMemory": {
        "ActiveMemoryExpansionEnabled": "CUD",
        "ActiveMemorySharingEnabled": "CUD",
        "BarrierSynchronizationRegisterArrayCount": "CUD",
        "DesiredEntitledMemory": "CUD",
        "DesiredHugePageCount": "CUD",
        "DesiredMemory": "CUD",
        "DesiredPhysicalPageTableRatio": "CUD",
        "ExpansionFactor": "CUD",
        "HardwarePageTableRatio": "CUD",
        "ManualEntitledModeEnabled": "ROR",
        "MaximumHugePageCount": "CUD",
        "MaximumMemory": "CUD",
        "MemoryWeight": "CUD",
        "MinimumHugePageCount": "CUD",
        "MinimumMemory": "CUD",
        "PrimaryPagingServicePartition": "CUD",
        "SecondaryPagingServicePartition": "CUD",
    },
    "ProfileSRIOVEthernetLogicalPort": {
        "AdapterID": "CUR",
        "AllowedMACAddresses": "CUD",
        "AllowedVLANs": "CUD",
        "AssociatedProfileVETHBackupDevice": "COD",
        "AssociatedProfileVNICBackupDevice": "COD",
        "BackupDeviceSlotID": "COD",
        "BackupDeviceType": "COD",
        "ConfigurationID": "CUR",
        "ConfiguredCapacity": "CUR",
        "ConfiguredCapacityPercentage": "CUR",
        "ConfiguredMaxCapacity": "CUR",
        "HCNID": "ROR",
        "IEEE8021QAllowablePriorities": "CUD",
        "IEEE8021QPriority": "CUD",
        "IsDebug": "CUR",
        "IsDiagnostic": "CUR",
        "IsHugeDMA": "CUR",
        "IsPromiscous": "CUR",
        "LocationCode": "ROR",
        "LogicalPortID": "CUR",
        "MACAddress": "CUD",
        "MACAddressFlags": "CUD",
        "Migratable": "COD",
        "NumberOfAllowedVLANs": "CUD",
        "PhysicalPortID": "CUR",
        "PortVLANID": "CUR",
        "TuningBufferID": "CUR",
    },
    "ProfileSRIOVRoCELogicalPort": {
        "AdapterID": "CUR",
        "AllowedMACAddresses": "CUD",
        "AllowedVLANs": "CUD",
        "AssociatedProfileVETHBackupDevice": "COD",
        "AssociatedProfileVNICBackupDevice": "COD",
        "BackupDeviceSlotID": "COD",
        "BackupDeviceType": "COD",
        "ConfigurationID": "CUR",
        "ConfiguredCapacity": "CUR",
        "ConfiguredCapacityPercentage": "CUR",
        "ConfiguredMaxCapacity": "CUR",
        "HCNID": "ROR",
        "IEEE8021QAllowablePriorities": "CUD",
        "IEEE8021QPriority": "CUD",
        "IsDebug": "CUR",
        "IsDiagnostic": "CUR",
        "IsHugeDMA": "CUR",
        "IsPromiscous": "CUR",
        "LocationCode": "ROR",
        "LogicalPortID": "CUR",
        "MACAddress": "CUD",
        "MACAddressFlags": "CUD",
        "Migratable": "COD",
        "NumberOfAllowedVLANs": "CUD",
        "PhysicalPortID": "CUR",
        "PortVLANID": "CUR",
        "TuningBufferID": "CUR",
    },
    "ProfileSRIOVVirtualNICBackupDevice": {
        "AdapterID": "COD",
        "CurrentCapacityPercentage": "COD",
        "FailOverPriority": "COD",
        "MaxCapacityPercentage": "COD",
        "PhysicalPortID": "COD",
        "VIOSID": "COD",
        "VIOSName": "COD",
    },
    "ProfileTrunkAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "AllowedOperatingSystemMACAddresses": "CUD",
        "InterfaceName": "CUD",
        "IsQualityOfServicePriorityEnabled": "CUD",
        "IsRequired": "CUD",
        "IsTaggedVLANSupported": "CUA",
        "LocalPartitionID": "CUR",
        "MaCAddressValue": "CUD",
        "PortVLANID": "CUR",
        "QualityOfServicePriority": "CUD",
        "TaggedVLANIDs": "CUA",
        "TrunkPriority": "CUD",
        "VirtualSlotNumber": "CUR",
        "VirtualSwitchID": "ROR",
        "VirtualSwitchInterfaceManagerID": "CUD",
        "VirtualSwitchInterfaceType": "CUD",
        "VirtualSwitchInterfaceTypeVersion": "CUD",
        "VirtualSwitchName": "CUD",
    },
    "ProfileVirtualFibreChannelClientAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "ConnectingPartitionID": "CUR",
        "ConnectingVirtualSlotNumber": "CUR",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "VirtualSlotNumber": "CUR",
        "WWPNs": "CUR",
    },
    "ProfileVirtualFibreChannelServerAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "ConnectingPartitionID": "CUR",
        "ConnectingVirtualSlotNumber": "CUR",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "VirtualSlotNumber": "CUR",
    },
    "ProfileVirtualNICDedicated": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "AssociatedBackingDevices": "CUR",
        "Details": "CUR",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "VirtualSlotNumber": "CUR",
    },
    "ProfileVirtualNICSRIOVBackingDevice": {
        "AssociatedVirtualIOServerID": "CUR",
        "CurrentCapacityPercentage": "ROR",
        "DesiredCapacityPercentage": "COD",
        "DesiredMaxCapacityPercentage": "ROR",
        "DeviceType": "CUR",
        "FailOverPriority": "CUR",
        "MaxCapacityPercentage": "COD",
        "RelatedSRIOVAdapterID": "COR",
        "RelatedSRIOVPhysicalPortID": "COR",
    },
    "ProfileVirtualNICShared": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "AssociatedSwitchID": "CUR",
        "Details": "CUR",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "VirtualSlotNumber": "CUR",
    },
    "ProfileVirtualSCSIClientAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "BackingDeviceName": "CUD",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "RemoteBackingDeviceName": "CUD",
        "RemotePartitionID": "CUR",
        "RemoteSlotNumber": "CUA",
        "VirtualSlotNumber": "CUR",
    },
    "ProfileVirtualSCSIServerAdapter": {
        "AdapterType": "CUR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "BackingDeviceName": "CUD",
        "IsRequired": "CUD",
        "LocalPartitionID": "CUR",
        "RemoteBackingDeviceName": "CUD",
        "RemotePartitionID": "CUR",
        "RemoteSlotNumber": "CUA",
        "VirtualSlotNumber": "CUR",
    },
    "RelatedIBMiIOSlot": {
        "AlternateLoadSourceAttached": "ROR",
        "ConsoleCapable": "ROR",
        "DirectOperationsConsoleCapable": "ROR",
        "IOP": "ROR",
        "IOPInfoStale": "ROR",
        "IOPoolID": "ROR",
        "LANConsoleCapable": "ROR",
        "LoadSourceAttached": "ROR",
        "LoadSourceCapable": "ROR",
        "OperationsConsoleAttached": "ROR",
        "OperationsConsoleCapable": "ROR",
    },
    "SRIOVAdapter": {
        "AdapterID": "ROR",
        "AdapterMode": "CUD",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "AdapterState": "ROR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "ConvergedEthernetPhysicalPorts": "ROR",
        "CurrentAdapterConfigurationOption": "ROO",
        "DefaultAdapterConfigurationOption": "ROO",
        "Description": "CUD",
        "DeviceName": "ROR",
        "DeviceType": "ROR",
        "DynamicPartitionAssignmentCapable": "ROR",
        "DynamicReconfigurationConnectorName": "CUD",
        "EthernetLogicalPorts": "ROR",
        "EthernetPhysicalPorts": "ROR",
        "FibreChannelOverEthernetLogicalPorts": "ROR",
        "IsFunctional": "ROR",
        "LogicalPartitionAssignmentCapable": "ROO",
        "MaximumHugeDMALogicalPorts": "ROR",
        "MaximumLogicalPortsSupported": "ROR",
        "PartnerAdapterID": "ROO",
        "PartnerDynamicReconfigurationConnectorName": "CUD",
        "Personality": "ROR",  # live-vocabulary: allow captured schema behavior annotation, not a field value
        "PhysicalLocation": "ROR",
        "PossibleAdapterConfigurationOptions": "ROO",
        "SRIOVAdapterID": "COD",
        "SRIOVRoCELogicalPorts": "ROR",
        "SRIOVRoCEPhysicalPorts": "ROR",
        "UnconfiguredLogicalPorts": "ROR",
        "UniqueDeviceID": "ROR",
    },
    "SharedProcessorConfiguration": {
        "DesiredProcessingUnits": "CUD",
        "DesiredVirtualProcessors": "CUD",
        "MaximumProcessingUnits": "CUD",
        "MaximumVirtualProcessors": "CUD",
        "MinimumProcessingUnits": "CUD",
        "MinimumVirtualProcessors": "CUD",
        "SharedProcessorPoolID": "CUD",
        "SharedProcessorPoolName": "CUD",
        "UncappedWeight": "CUD",
    },
    "TaggedIO": {
        "AlternateConsole": "CUD",
        "AlternateLoadSource": "CUD",
        "Console": "CUR",
        "LoadSource": "CUR",
        "OperationsConsole": "CUD",
    },
}
CAPTURED_BEHAVIOR["LogicalPartitionProfile"]["PartitionPlacement"] = "CUD"
EXPECTED_BEHAVIOR = {
    (parent, field): behavior
    for parent, fields in CAPTURED_BEHAVIOR.items()
    for field, behavior in fields.items()
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
    name: StrictStr | None = Field(
        default=None,
        description="New saved-profile name; omitted preserves the fetched name.",
    )
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
    ) = Field(
        default=None,
        description="Saved-profile boot mode;this is not an activation-job token.",
    )
    min_memory: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Minimum saved memory in MiB; omitted preserves the fetched value.",
    )
    desired_memory: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Desired saved memory in MiB; omitted preserves the fetched value.",
    )
    max_memory: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Maximum saved memory in MiB; omitted preserves the fetched value.",
    )
    min_processors: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Minimum whole dedicated or virtual processor count,according to the saved mode; omitted preserves the fetched value.",
    )
    desired_processors: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Desired whole dedicated or virtual processor count,according to the saved mode; omitted preserves the fetched value.",
    )
    max_processors: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Maximum whole dedicated or virtual processor count,according to the saved mode; omitted preserves the fetched value.",
    )
    min_units: StrictFloat | None = Field(
        default=None,
        gt=0,
        description="Minimum shared processing units; dedicated profiles refuse this field; omitted preserves the fetched value.",
    )
    desired_units: StrictFloat | None = Field(
        default=None,
        gt=0,
        description="Desired shared processing units; dedicated profiles refuse this field; omitted preserves the fetched value.",
    )
    max_units: StrictFloat | None = Field(
        default=None,
        gt=0,
        description="Maximum shared processing units; dedicated profiles refuse this field; omitted preserves the fetched value.",
    )
    sharing_mode: SharingMode | None = Field(
        default=None,
        description="Saved sharing mode compatible with the existing dedicated/shared processor mode.",
    )


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


def _patch_paths(source: ET.Element) -> tuple[str, dict[str, str]]:
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
    return dedicated, mapping


def requested_profile_values(
    source: ET.Element, patch: ProfilePatch
) -> dict[str, str | None]:
    """Read every requested scalar through the same finite typed-field mapping."""
    _, mapping = _patch_paths(source)
    result = {}
    for key in patch.model_dump(exclude_none=True):
        field = one(source, mapping[key])
        if len(field):
            raise ValueError("requested profile field is not scalar")
        result[mapping[key]] = field.text
    return result


def apply_profile_patch(source: ET.Element, patch: ProfilePatch) -> ET.Element:
    values = patch.model_dump(exclude_none=True)
    if not values:
        raise ValueError("profile patch requires at least one typed value")
    result = deepcopy(source)
    dedicated, mapping = _patch_paths(source)
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
