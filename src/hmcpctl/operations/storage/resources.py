"""Volume groups, disks and virtual SCSI mappings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hmcpctl.client.client_storage import mapping_lpar_uuid, storage_mapping_id
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profiles.sync import ChangeLocation, read_change_location

from ...documents.storage import StorageKind
from ...errors import HMCError
from ...jobs import JobOutcome, job_identifier, job_outcome
from ...resource_identity import (
    is_uuid,
    resolve_lpar_uuid,
    resolve_vios_uuid,
)


@dataclass(frozen=True)
class StorageMutationResult:
    """Storage facts and diagnostic payload, without extra HMC requests."""

    raw: Any
    job: JobOutcome | None = None
    lpar_uuid: str | None = None
    change_location: ChangeLocation | None = None
    resource: Mapping[str, Any] | None = None

    @classmethod
    def from_raw(
        cls,
        raw: Any,
        *,
        lpar_uuid: str | None = None,
        change_location: ChangeLocation | None = None,
    ) -> StorageMutationResult:
        source = raw if isinstance(raw, dict) else None
        resource = source.get("Resource", source) if isinstance(source, dict) else None
        entry = (
            source
            if isinstance(source, dict) and "Resource" in source
            else {"Resource": resource}
        )
        is_job = isinstance(resource, dict) and (
            "JobID" in resource or "Status" in resource
        )
        job = job_outcome("", entry) if is_job and job_identifier(entry) else None
        return cls(
            raw,
            job,
            lpar_uuid,
            change_location,
            resource if isinstance(resource, Mapping) else None,
        )


@dataclass(frozen=True)
class VolumeGroup:
    """Stable inventory projection for one VIOS volume group."""

    uuid: str
    name: str
    capacity_gib: float | None
    free_space_gib: float | None
    free_space_diagnostic: str | None


@dataclass(frozen=True)
class StorageMapping:
    """Stable inventory projection for one virtual SCSI mapping.

    ``id`` is the ``<server adapter>/<target device>`` identity (ADR 0168), or None
    when the HMC reports too little to identify the mapping.
    """

    id: str | None
    lpar_uuid: str | None
    backing_kind: str | None
    backing_name: str | None


def _resource(entry: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    resource = entry.get("Resource", entry)
    if not isinstance(resource, Mapping):
        raise HMCError(f"{operation} returned a resource with an invalid shape")
    return resource


def _required_text(resource: Mapping[str, Any], field: str, operation: str) -> str:
    value = resource.get(field)
    if isinstance(value, str) and value:
        return value
    raise HMCError(f"{operation} returned no usable {field}")


#: A plain non-negative decimal, optionally with a fractional part. Deliberately
#: ASCII-only and narrower than `float()`: it admits neither a sign, exponent,
#: nor the `nan`/`inf` literals, none of which name a storage quantity.
#:
#: The widths are bounded because parsing an unbounded digit string goes wrong at
#: some length either way — `int()` raises past CPython's 4300-digit conversion
#: limit, `float()` saturates to an `inf` that `json.dumps` emits as invalid JSON.
#: 10**20 MiB exceeds any real storage quantity, so anything wider is malformed
#: input and takes the same `HMCError` as any other malformed value.
_DECIMAL_TEXT = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,10})?\Z")


def _optional_number(
    resource: Mapping[str, Any], field: str, operation: str
) -> float | None:
    """Read an optional storage quantity the HMC may report with a fraction.

    The HMC reports volume-group capacity as a decimal string, and on some
    hardware that string carries a fractional part, so a whole-number-only rule
    rejects a well-formed reply. An integral value is returned as `int` so
    rendered output gains no `.0` suffix.
    """
    value = resource.get(field)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _DECIMAL_TEXT.match(value):
        whole, _, fraction = value.partition(".")
        # Route any integral value through int() rather than float(), in either
        # spelling: int is exact across the admitted width, where float() starts
        # losing digits above 2**53 and would make "…93" and "…93.0" differ.
        if not fraction.strip("0"):
            return int(whole)
        return float(value)
    raise HMCError(f"{operation} returned an invalid {field}")


def volume_group(entry: Mapping[str, Any]) -> VolumeGroup:
    operation = "list_volume_groups"
    resource = _resource(entry, operation)
    uuid = entry.get("UUID")
    if not isinstance(uuid, str) or not uuid:
        raise HMCError(f"{operation} returned no usable UUID")
    capacity_gib = _optional_number(resource, "GroupCapacity", operation)
    free_space_gib = _optional_number(resource, "FreeSpace", operation)
    diagnostic = None
    if (
        capacity_gib is not None
        and free_space_gib is not None
        and free_space_gib > capacity_gib
    ):
        free_space_gib = None
        diagnostic = "free_space_exceeds_capacity"
    return VolumeGroup(
        uuid=uuid,
        name=_required_text(resource, "GroupName", operation),
        capacity_gib=capacity_gib,
        free_space_gib=free_space_gib,
        free_space_diagnostic=diagnostic,
    )


def storage_mapping(entry: Mapping[str, Any]) -> StorageMapping:
    operation = "list_storage_mappings"
    resource = _resource(entry, operation)
    mapping_id = storage_mapping_id(resource)
    lpar_uuid = mapping_lpar_uuid(resource)
    storage = resource.get("Storage")
    backing = storage if isinstance(storage, Mapping) else {}
    for kind in ("VirtualDisk", "PhysicalVolume", "VirtualOpticalMedia"):
        candidate = backing.get(kind)
        if isinstance(candidate, Mapping):
            name = (
                candidate.get("DiskName")
                or candidate.get("VolumeName")
                or candidate.get("MediaName")
            )
            return StorageMapping(
                mapping_id, lpar_uuid, kind, name if isinstance(name, str) else None
            )
    return StorageMapping(mapping_id, lpar_uuid, None, None)


async def list_volume_groups(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> list[VolumeGroup]:
    """List volume groups on a VIOS.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        HMCError: If the HMC request fails or returns an invalid resource shape.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    return [volume_group(entry) for entry in await hmc.list_volume_groups(vios_uuid)]


async def resolve_volume_group_uuid(hmc: HMCClient, vios_uuid: str, value: str) -> str:
    """Resolve one complete VG identity from the selected VIOS's inventory."""
    uuid_selector = is_uuid(value)
    candidates = []
    matches = []
    incomplete = False
    for entry in await hmc.list_volume_groups(vios_uuid):
        resource = entry.get("Resource", entry)
        name = resource.get("GroupName") if isinstance(resource, Mapping) else None
        uuid = entry.get("UUID")
        if not isinstance(uuid, str):
            uuid = ""
        valid_uuid = is_uuid(uuid)
        candidates.append(f"{name!r} ({uuid!r})")
        if uuid_selector:
            incomplete |= not valid_uuid
            matched = valid_uuid and uuid.casefold() == value.casefold()
        else:
            incomplete |= not isinstance(name, str) or not name
            matched = name == value
        if matched:
            incomplete |= not valid_uuid
            matches.append(uuid)
    if incomplete or len(matches) != 1:
        if incomplete:
            reason = "incomplete identity"
        elif matches:
            reason = "ambiguous"
        else:
            reason = "not found"
        raise ValueError(
            f"VolumeGroup {value!r} on VIOS {vios_uuid!r}: {reason}. "
            f"Candidates: {', '.join(candidates) or 'none'}. "
            f"Inspect with storage list-vgs {vios_uuid}."
        )
    return matches[0]


async def create_volume_group(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    name: str,
    physical_volumes: list[str],
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Create a volume group from the selected physical volumes.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        HMCError: If the HMC rejects the creation request or it cannot be completed.
    """
    return StorageMutationResult.from_raw(
        await hmc.create_volume_group(
            await resolve_vios_uuid(
                hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
            ),
            name,
            physical_volumes,
        )
    )


async def create_virtual_disk(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    name: str,
    capacity_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Create a virtual disk of ``capacity_mib`` in a volume group.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Raises:
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        HMCError: If the HMC rejects the creation request or it cannot be completed.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return StorageMutationResult.from_raw(
        await hmc.create_virtual_disk(
            vios_uuid,
            vg_uuid,
            name,
            capacity_mib,
        )
    )


def _names_disk_inline(backing: dict[str, Any], vg_uuid: str, disk_name: str) -> bool:
    """V10R3 carries a mapped disk inline by DiskName with no href (#936).

    The mappings are already scoped to one VIOS, where a logical-volume name is
    unique; a VolumeGroup link, when the mapping carries one, must name *vg_uuid*.
    """
    if backing.get("DiskName") != disk_name:
        return False
    group = backing.get("VolumeGroup")
    group_link = group.get("href", "") if isinstance(group, dict) else ""
    # UUIDs compare case-insensitively: V10R3 links a VolumeGroup by a lower-case
    # UUID under an upper-case VIOS UUID.
    suffix = f"/volumegroup/{vg_uuid.lower()}"
    return not group_link or group_link.rstrip("/").lower().endswith(suffix)


async def delete_virtual_disk(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    disk_name: str,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Delete a Virtual Disk from a Volume Group.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Validates that the disk is not mapped to any LPAR before deletion.

    Args:
        hmc: HMC client instance.
        system_name_or_uuid: Optional managed-system name or UUID used to scope
            a VIOS name.
        vios_name_or_uuid: VIOS partition name or UUID.
        vg_uuid: Volume Group name or UUID identifying the group containing the disk.
        disk_name: Name of the Virtual Disk to delete.

    Returns:
        A mutation result whose raw value is the parsed HMC response entry from
        the VolumeGroup update, or ``None`` when no resource entry is returned.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the disk is mapped to an LPAR or deletion fails.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    mappings = await hmc.list_storage_mappings(vios_uuid)
    disk_link = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}/VirtualDisk/{disk_name}"

    for mapping in mappings:
        backing_storage = mapping.get("Storage", {}).get("VirtualDisk", {})
        if isinstance(backing_storage, dict):
            storage_link = backing_storage.get("href", "")
            if (
                disk_link in storage_link
                or storage_link.endswith(f"VirtualDisk/{disk_name}")
                or _names_disk_inline(backing_storage, vg_uuid, disk_name)
            ):
                lpar = mapping.get("AssociatedLogicalPartition", {})
                lpar_name = lpar.get("PartitionName", lpar.get("href", "unknown"))
                raise HMCError(
                    f"Cannot delete virtual disk {disk_name!r}: it is mapped to "
                    f"LPAR {lpar_name!r}. Use detach_storage_mapping first to "
                    "remove the mapping."
                )

    return StorageMutationResult.from_raw(
        await hmc.delete_virtual_disk(vios_uuid, vg_uuid, disk_name)
    )


async def map_storage(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    kind: StorageKind,
    storage_name: str,
    target: str | None = None,
    ownership_override: bool = False,
) -> StorageMutationResult:
    """Authorize an LPAR and map VIOS storage to it.

    Raises:
        ResourceNotFoundError: If a supplied VIOS, LPAR, or managed-system selector
            cannot be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        ValueError: If selector scope cannot be verified.
        HMCError: If the HMC rejects the mapping request or it cannot be completed.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.map_storage_to_lpar(
        vios_uuid, kind, storage_name, lpar_uuid, target
    )
    return StorageMutationResult.from_raw(
        resource,
        lpar_uuid=lpar_uuid,
        change_location=location,
    )


async def list_storage_mappings(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
    *,
    system_name_or_uuid: str | None = None,
) -> list[StorageMapping]:
    """List VirtualSCSIMappings on a VIOS, optionally scoped to an LPAR.

    Returns mappings with backing storage details and client LPAR information.
    Use lpar to scope mappings to a single partition by name or UUID.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar_uuid = None
    if lpar_name_or_uuid:
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
    return [
        storage_mapping(entry)
        for entry in await hmc.list_storage_mappings(vios_uuid, lpar_uuid)
    ]


async def detach_storage_mapping(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    mapping_id: str,
    *,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> StorageMutationResult:
    """Authorize the mapped LPAR, then detach its VirtualSCSIMapping.

    ``mapping_id`` is the exact ``<server adapter>/<target device>`` identity
    (for example ``vhost0/vtscsi0``) returned by ``list_storage_mappings``.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        PermissionError: If the mapped LPAR ownership authorization rejects the
            mutation.
        ValueError: If the mapping or its client-LPAR identity cannot be verified.
        HMCError: If the HMC request fails.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    matches = [
        item
        for item in await hmc.list_storage_mappings(vios_uuid)
        if storage_mapping_id(item) == mapping_id
    ]
    if not matches:
        raise ValueError(
            f"Storage mapping {mapping_id!r} was not found on VIOS {vios_name_or_uuid!r}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Storage mapping {mapping_id!r} is ambiguous on VIOS {vios_name_or_uuid!r}"
        )
    lpar_uuid = mapping_lpar_uuid(matches[0])
    if lpar_uuid is None:
        raise ValueError(
            f"Storage mapping {mapping_id!r} does not identify its client LPAR"
        )
    await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    await hmc.delete_storage_mapping(vios_uuid, mapping_id, lpar_uuid)
    return StorageMutationResult(None, change_location=location)
