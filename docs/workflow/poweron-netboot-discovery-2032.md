# AIX/Linux network boot on HMC V10R3 SP1060

**Verdict: inconclusive for build 2408210051.** The HMC identified the LogicalPartition
PowerOn operation. Its templates advertised zero parameters. The retrieved schemas
describe partition resources and their types. They establish neither the accepted AIX/Linux
network-boot request nor which companions the job requires. [Discovery issue #2032](https://github.com/randomparity/hmcpctl/issues/2032)

## What the HMC returned

Authenticated GETs on 2026-11-19 returned `ManagementConsole/VersionInfo` with Version 10,
Release 3, Maintenance 0, Minor 3, ServicePackName 1060 and BuildLevel 2408210051.
The selected partition reported `PartitionType=AIX/Linux` and `PartitionState=not activated`.
No activation job was submitted.

| GET under `/rest/api/uom/` | Accept | Observation |
| --- | --- | --- |
| `LogicalPartition/{L}/do/PowerOn` | `application/vnd.ibm.powervm.web+xml; type=JobRequest` | 200. Group `LogicalPartition`, operation `PowerOn`, JobRequest schema version `V1_0`, one JobParameters container, zero JobParameter children. |
| `ManagedSystem/{S}/LogicalPartition/{L}/do/PowerOn` | `*/*` and `application/atom+xml` | Both returned 200 with the same operation, group, version and empty parameter list. The typed web media request returned 406. |

Neither successful template supplied an XML schema-location hint. An [earlier template
survey](https://github.com/randomparity/hmcpctl/issues/868#issuecomment-6853090002) also found
empty lists on V10R3 M1060 and V11R2 1120. Separately, a V10R3 current-configuration request
with `OperationType=activate` failed with `INVALID_PARAMETER`. Omitting it allowed ordinary
activation. That experiment tested one value and configuration. [ADR 0161](../adr/0161-lpar-poweron-activation-parameters.md)

## Schema evidence

GETs used `Accept: */*` under `/rest/api/web/schema/`. The `LogicalPartition.xsd` body
was 67,178 bytes (SHA-256 `8f7a0e219fe9195551b9249e4dc3a21a55caaf545dd49ee8b4efde2073c3974c`).
Starting there, the traversal inspected every XSD include in each nonempty schema, deduplicating
paths across cycles. The firmware uses source paths such as `../uom/BasePartition.xsd` and
`../uom/inc/Types.xsd`. These resolved through the public schema mount as
`BasePartition.xsd` and `inc/Types.xsd`. Of 125 distinct names encountered including the root,
118 yielded XSDs. Those contained 337 include declarations and no imports.

Seven dependencies returned HTTP 200 with empty bodies: `Empty.xsd`, `ietf/AtomLink.xsd`,
and `common/inc/{CommonTypes,CommonMetaData,CommonPatterns,CommonEnumerations,CommonLists}.xsd`.
Alternate root and `inc/` spellings for the common files also yielded empty bodies.
A representative literal relative path for `BasePartition.xsd` returned 404.
The seven unresolved dependencies limit schema coverage.

Separate probes of `{Job,JobRequest,JobParameter,JobParameters,RequestedOperation,JobResponse,PowerOn,PowerOn_LogicalPartition}.xsd`
and `web/{JobRequest,JobParameter,RequestedOperation,JobResponse}.xsd` all returned HTTP 200
with zero bytes. An earlier V10R3 read recorded the same HTTP status and empty body for an unknown schema name.
[Observed schema route](../api-patterns.md#media-types-per-endpoint)

A case-insensitive search across all 118 XML bodies found no `netboot`, `OperationType`,
`RequestedOperation` or `JobParameter`. Inspection of matching declarations puts
`IPAddress`, `Gateway` and `SubnetMask` on `IPInterface`, `SlotPhysicalLocationCode` on
`IOSlot`, and `BootMode` and `KeylockPosition` on `BasePartition`.
Their resource occurrence constraints apply to those resources. The PowerOn-related enums
in `inc/Enumerations.xsd` describe power policy or hardware settings.

## What remains unresolved

| Candidates in the IBM reference | What the build evidence leaves open for AIX/Linux PowerOn |
| --- | --- |
| `OperationType=netboot` | Acceptance of the selector, including whether a profile is required. |
| `IPAddress`, `Gateway`, `ServerIPAddress`, `SubnetMask` | The first three descriptions concern IBM i. The IPv4 mask description points to a CLI option. Accepted address names, IP families, types and requiredness for AIX/Linux are unknown. |
| `SlotPhysicalLocationCode`, `NetbootType`, `BootImageFileName` | Adapter selection, supported install types and the image-name rule on this build. IBM describes an IPv6-only image-name requirement and scopes the NFS default to IBM i. |
| `BOOTPRetries`, `TFTPRetries`, `Timeout` | IBM describes retry ranges of 0–9, defaults of 5, and a timeout default of 3,600,000 ms. The firmware reads supply no corresponding acceptance, requiredness or defaults for this operation. |
| `LogicalPartitionProfile`, `LogicalPartitionProfileUUID`, `LogicalPartitionProfileName`; `bootmode` / `BootMode`; `keylock` / `KeylockPosition`; `novsi` / `UseVirtualStationInterface` | Accepted profile identifier, spelling and companion rules during netboot. The Power10 table calls `LogicalPartitionProfile` a name. The Power11 table calls it a UUID and adds `LogicalPartitionProfileName`. Activation examples leave the network-boot combination open. |

The local source comparison used `docs/refs/hmc-rest-api-p10/jobs/logicalpartition-jobs/039-poweron_logicalpartition-job.md:30`
and `docs/refs/hmc-rest-api-p11/jobs/logicalpartition-jobs/039-poweron_logicalpartition-job.md:30`,
including parameter rows through lines 71 and 72 respectively and the following samples.
[IBM Power10 reference](https://www.ibm.com/docs/en/power10/5765-VHP?topic=jobs-poweron-logicalpartition-job)
and [IBM Power11 reference](https://www.ibm.com/docs/en/power11/9824-22A?topic=jobs-poweron-logicalpartition-job)

A build-specific IBM operation definition naming the AIX/Linux fields, types, conditional
requirements and defaults would resolve the contract. A sanitized, verified successful
same-build request would establish one accepted combination. Requiredness and defaults
would still need authoritative rules or controlled omission evidence. [#868](https://github.com/randomparity/hmcpctl/issues/868)
owns production exposure once that evidence exists. [#2033](https://github.com/randomparity/hmcpctl/issues/2033)
owns verification that the selected adapter boots the intended image.

## Inspected XSD inventory

Each stem below has the suffix `.xsd` and the indicated directory under the schema mount.

**Root** (91)

AdapterConfigurationOption, AdapterConfigurationOptionCollection, BasePartition,
BootListInformation, EthernetBackingDevice, Groups, GroupsCollection,
HardwareAcceleratorQoSType, HardwareAcceleratorQoSTypeCollection, HostChannelAdapter,
HostChannelAdapterBandwidthCapability, HostChannelAdapterCollection,
HostEthernetAdapterLogicalPort, HostEthernetAdapterLogicalPortCollection, IBMiIOSlot,
IBMiProfileTaggedIO, IOAdapter, IORDevice, IORDeviceCollection, IOSlot, IPInterface,
LogicalPartition, LogicalPartitionCapabilities, LogicalPartitionDedicatedProcessorConfiguration,
LogicalPartitionIOConfiguration, LogicalPartitionMemoryConfiguration,
LogicalPartitionProcessorConfiguration, LogicalPartitionProfileDedicatedProcessorConfiguration,
LogicalPartitionProfileIOConfiguration, LogicalPartitionProfileMemoryConfiguration,
LogicalPartitionProfileProcessorConfiguration,
LogicalPartitionProfileSharedProcessorConfiguration,
LogicalPartitionSharedProcessorConfiguration, LogicalUnit, LogicalUnitMigration,
LogicalVolumeVirtualTargetDevice, PartitionPersistentMemoryConfiguration,
PhysicalFibreChannelAdapter, PhysicalFibreChannelPort, PhysicalFibreChannelPortCollection,
PhysicalVolume, PhysicalVolumeCollection, PhysicalVolumeVirtualTargetDevice,
PortVFConfigurationOption, PortVFConfigurationOptionCollection, ProfileClientNetworkAdapter,
ProfileIOSlot, ProfileIOSlotCollection, ProfileTrunkAdapter, ProfileVirtualEthernetAdapter,
ProfileVirtualFibreChannelAdapter, ProfileVirtualFibreChannelClientAdapter,
ProfileVirtualFibreChannelServerAdapter, ProfileVirtualIOAdapter,
ProfileVirtualNICBackingDevice, ProfileVirtualNICDedicated, ProfileVirtualNICDetails,
ProfileVirtualNICSRIOVBackingDevice, ProfileVirtualNICShared, ProfileVirtualSCSIAdapter,
ProfileVirtualSCSIClientAdapter, ProfileVirtualSCSIServerAdapter, SRIOVAdapter,
SRIOVConvergedNetworkAdapterPhysicalPort, SRIOVConvergedNetworkAdapterPhysicalPortCollection,
SRIOVEthernetPhysicalPort, SRIOVEthernetPhysicalPortCollection, SRIOVPhysicalPort,
SRIOVRoCEPhysicalPort, SRIOVRoCEPhysicalPortCollection, SRIOVUnconfiguredLogicalPort,
SRIOVUnconfiguredLogicalPortCollection, SharedFileSystemFile,
SharedFileSystemFileVirtualTargetDevice, SharedStoragePoolLogicalUnitVirtualTargetDevice,
VacatingMigrationTier, VirtualDisk, VirtualEthernetAdapter, VirtualFibreChannelAdapter,
VirtualIOAdapter, VirtualNICBackingDevice, VirtualNICDedicated, VirtualNICDetails,
VirtualNICSRIOVBackingDevice, VirtualNICShared, VirtualOpticalMedia,
VirtualOpticalMediaCollection, VirtualOpticalTargetDevice, VirtualSCSIAdapter,
VirtualSCSIStorage, VirtualTargetDevice.

**inc/** (13)

AuthorizedKeyCollection, ChoiceCollections, Enumerations, Globals, IOAdapterChoiceCollection,
Lists, Patterns, Primitives, ProfileVirtualNICBackingDeviceChoiceCollection, Types, Unions,
VirtualNICBackingDeviceChoiceCollection, VirtualTargetDeviceChoiceCollection.

**links/** (14)

ClientNetworkAdapter_Links, Group_Links, LogicalPartitionProfile_Links,
SRIOVEthernetLogicalPort_Links, SRIOVFibreChannelOverEthernetLogicalPort_Links,
SRIOVRoCELogicalPort_Links, Task_Links, Tier_Links, TrunkAdapters_Links,
VirtualFibreChannelClientAdapter_Links, VirtualNICDedicated_Links,
VirtualSCSIClientAdapter_Links, VirtualSCSIServerAdapter_Links, VirtualSwitch_Links.
