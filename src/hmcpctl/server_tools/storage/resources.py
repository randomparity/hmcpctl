"""MCP tools for virtual storage, media repositories, and clusters/SSP."""

from __future__ import annotations

from typing import Any

from ..._app import (
    run_limited_collection,
    serialize_tool_result,
    with_client,
)
from ...client.core import HMCClient
from ...documents.storage import StorageKind
from ...jobs import (
    DeviceType,
    LuType,
)
from ...operations.lpar.profile_sync import resource_with_change_location
from ...operations.lpar.provision import (
    AttachDiskResult,
    ProvisionStorage,
    attach_disk_to_lpar,
)
from ...operations.storage.cluster import (
    create_logical_unit,
    delete_logical_unit,
)
from ...operations.storage.resources import (
    create_media_repository,
    create_optical_media,
    create_virtual_disk,
    create_volume_group,
    delete_media_repository,
    delete_optical_media,
    delete_virtual_disk,
    detach_storage_mapping,
    get_media_repository,
    list_optical_mappings,
    list_optical_media,
    list_storage_mappings,
    list_volume_groups,
    map_storage,
    mount_optical_media,
    unmount_optical_media,
    upload_iso,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="storage.list_volume_groups", target_kind="vios")
def hmc_list_volume_groups(
    vios_name_or_uuid: str,
    profile: str | None = None,
    limit: int | None = None,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List Volume Groups on a VIOS.

    Each Volume Group shows capacity and free space (GiB), the physical volumes backing it
    and the virtual disks already carved out.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        profile: TOML profile name, or the environment-default HMC when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
    """

    return [
        serialize_tool_result(entry)
        for entry in run_limited_collection(
            lambda hmc: list_volume_groups(
                hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
            ),
            limit,
            profile=profile,
        )
    ]


@tool(effect="mutate", operation="storage.create_volume_group", target_kind="vios")
def hmc_create_volume_group(
    vios_name_or_uuid: str,
    name: str,
    physical_volumes: list[str],
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create a Volume Group on a VIOS from one or more physical volumes.

    ``physical_volumes`` is a list of free PV device names (e.g. ['hdisk10']). Use
    the GetFreePhysicalVolumes job / VIOS 'lspv' to find unused disks. This
    pools the disks so virtual disks can be carved out for LPARs.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        name: New volume-group name.
        physical_volumes: One or more unused VIOS physical-volume device names.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def operation(hmc):
        return (
            await create_volume_group(
                hmc,
                vios_name_or_uuid,
                name,
                physical_volumes,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(operation, profile=profile)


# Exhaustive (#1086): the LPAR, its optional system, and `vios_uuid` are declared
# selectors, and `vg_uuid`/`disk_name` are addressed inside that VIOS, which ADR
# 0039 treats as contained — the same surface as `hmc_create_virtual_disk` plus
# `hmc_map_storage_to_lpar`, both table-grantable.
@tool(effect="mutate", operation="storage.attach_disk", target_kind="lpar")
def hmc_attach_disk_to_lpar(
    lpar_name_or_uuid: str,
    vios_uuid: str,
    vg_uuid: str,
    disk_name: str,
    capacity_mib: int,
    dry_run: bool = False,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> AttachDiskResult:
    """Create and attach a virtual disk to an existing LPAR.

    Validates the LPAR and volume group before mutation. The execution order is
    create disk, then map it; the mapping makes the HMC create the vSCSI
    client/server adapter pair, so no adapter is added beforehand. Set
    dry_run=True to validate only. Expected HMC failures are returned per step;
    completed steps are ``ok`` and unattempted steps are ``skipped``. Returns
    ``change_location`` on a real run: the partition's ``CurrentProfileSync``
    and whether the mapping also reaches its current profile; null, with a
    warning, when the read fails.

    Args:
        lpar_name_or_uuid: Target partition name or UUID.
        vios_uuid: UUID of the VIOS that owns the volume group.
        vg_uuid: Volume-group UUID from ``hmc_list_volume_groups``.
        disk_name: Name for the new virtual disk.
        capacity_mib: New disk capacity in mebibytes.
        dry_run: Validate all selectors and prerequisites without mutating the HMC.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    return with_client(
        lambda hmc: attach_disk_to_lpar(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            ProvisionStorage(vios_uuid, disk_name, vg_uuid=vg_uuid),
            capacity_mib=capacity_mib,
            dry_run=dry_run,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="storage.create_disk", target_kind="vios")
def hmc_create_virtual_disk(
    vios_name_or_uuid: str,
    vg_uuid: str,
    disk_name: str,
    capacity_mib: int,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create a Virtual Disk (logical volume) inside a Volume Group.

    The disk becomes backing storage that you
    then attach to an LPAR with hmc_map_storage_to_lpar (storage_kind
    'VirtualDisk'). Find vg_uuid with hmc_list_volume_groups.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID from ``hmc_list_volume_groups``.
        disk_name: Name for the new virtual disk.
        capacity_mib: New disk capacity in mebibytes.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def operation(hmc):
        return (
            await create_virtual_disk(
                hmc,
                vios_name_or_uuid,
                vg_uuid,
                disk_name,
                capacity_mib=capacity_mib,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(
        operation,
        profile=profile,
    )


@tool(effect="destructive", operation="storage.delete_disk", target_kind="vios")
def hmc_delete_virtual_disk(
    vios_name_or_uuid: str,
    vg_uuid: str,
    disk_name: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Delete a Virtual Disk from a Volume Group.

    Validates that the disk is not mapped to any LPAR before deletion.
    Raises an error if the disk is in use; otherwise deletes the disk.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID from ``hmc_list_volume_groups``.
        disk_name: Name of the Virtual Disk to delete.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def operation(hmc):
        return (
            await delete_virtual_disk(
                hmc,
                vios_name_or_uuid,
                vg_uuid,
                disk_name,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(
        operation,
        profile=profile,
    )


@tool(effect="mutate", operation="storage.map", target_kind="vios")
def hmc_map_storage_to_lpar(
    vios_name_or_uuid: str,
    storage_name: str,
    lpar_name_or_uuid: str,
    storage_kind: StorageKind = "VirtualDisk",
    target_device: str | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Map backing storage to an LPAR via a Virtual SCSI mapping on a VIOS.

    The HMC creates the vSCSI client/server adapter pair for the mapping, so
    do not add one first with hmc_add_vscsi_adapter; that adapter stays
    unpaired. The VIOS's existing mappings are preserved. The result's
    ``change_location`` says whether that adapter pair lives only in the
    partition's current configuration (a profile activation then drops it).
    ``storage_kind`` is 'VirtualDisk' (a logical volume created with
    hmc_create_virtual_disk) or 'PhysicalVolume' (a whole hdisk). storage_name
    is the DiskName / device name. target_device optionally pins the vtscsi
    device name on the VIOS.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        storage_name: Virtual-disk name or physical-volume device name.
        lpar_name_or_uuid: Target partition name or UUID from ``hmc_list_lpars``.
        storage_kind: ``VirtualDisk`` for a logical volume or ``PhysicalVolume``
            for a whole disk.
        target_device: Optional VIOS virtual-target-device name.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def mapping(hmc: HMCClient):
        result = await map_storage(
            hmc,
            vios_name_or_uuid,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            kind=storage_kind,
            storage_name=storage_name,
            target=target_device,
            ownership_override=ownership_override,
        )
        return result.raw

    return with_client(mapping, profile=profile)


@tool(effect="mutate", operation="media.create_repository", target_kind="vios")
def hmc_create_media_repository(
    vios_name_or_uuid: str,
    vg_uuid: str,
    size_mib: int,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create the Virtual Media Repository (named VMLibrary) on a Volume Group.

    The repository holds file-backed ISO images for client partitions; only one
    can exist per VIOS. size_mib is MiB; the HMC's RepositorySize is GiB, so
    size_mib must be a multiple of 1024 and is sent as size_mib / 1024.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID from ``hmc_list_volume_groups``.
        size_mib: Repository capacity in mebibytes, a multiple of 1024.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def operation(hmc):
        return (
            await create_media_repository(
                hmc,
                vios_name_or_uuid,
                vg_uuid,
                size_mib,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(
        operation,
        profile=profile,
    )


@tool(effect="mutate", operation="media.create", target_kind="vios")
def hmc_create_optical_media(
    vios_name_or_uuid: str,
    vg_uuid: str,
    media_name: str,
    size_mib: int,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create a blank VirtualOpticalMedia (ISO container) in the media repository.

    Only blank media can be created via the API; media_name is the file name
    (e.g. 'aix.iso'). size_mib is MiB; the HMC's media Size is GiB, so
    size_mib must be a multiple of 1024 and is sent as size_mib / 1024.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID containing the media repository.
        media_name: ISO container file name, such as ``aix.iso``.
        size_mib: Blank media capacity in mebibytes, a multiple of 1024.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def operation(hmc):
        return (
            await create_optical_media(
                hmc,
                vios_name_or_uuid,
                vg_uuid,
                media_name,
                size_mib,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(
        operation,
        profile=profile,
    )


@tool(effect="destructive", operation="media.delete_repository", target_kind="vios")
def hmc_delete_media_repository(
    vios_name_or_uuid: str,
    vg_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> str:
    """Delete the Virtual Media Repository from a Volume Group.

    This is an immediate (synchronous) delete — it returns a confirmation
    string once the HMC has applied the change; there is no job to poll.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID containing the repository.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def delete_repository_and_confirm(hmc):
        await delete_media_repository(
            hmc, vios_name_or_uuid, vg_uuid, system_name_or_uuid=system_name_or_uuid
        )
        return f"Deleted media repository from VolumeGroup {vg_uuid}"

    return with_client(delete_repository_and_confirm, profile=profile)


@tool(effect="destructive", operation="media.delete", target_kind="vios")
def hmc_delete_optical_media(
    vios_name_or_uuid: str,
    vg_uuid: str,
    media_name: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> str:
    """Delete a VirtualOpticalMedia (ISO image) from the media repository.

    Refuses deletion if the media is currently mounted to any LPAR. Unmount
    all references first.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID containing the repository.
        media_name: Name of the ISO image to delete.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def delete_media_and_confirm(hmc):
        await delete_optical_media(
            hmc,
            vios_name_or_uuid,
            vg_uuid,
            media_name,
            system_name_or_uuid=system_name_or_uuid,
        )
        return f"Deleted optical media '{media_name}' from VolumeGroup {vg_uuid}"

    return with_client(delete_media_and_confirm, profile=profile)


@tool(effect="read", operation="media.get_repository", target_kind="vios")
def hmc_get_media_repository(
    vios_name_or_uuid: str,
    vg_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get the VolumeGroup Atom entry containing a Virtual Media Repository.

    The repository is nested at ``Resource.MediaRepositories.VirtualMediaRepository``.
    Its ``RepositorySize`` is in GiB; optional media entries are nested under
    ``OpticalMedia.VirtualOpticalMedia`` within the repository. Returns None when
    no repository-bearing VolumeGroup entry is available.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID containing the repository.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: get_media_repository(
            hmc, vios_name_or_uuid, vg_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="read", operation="media.list", target_kind="vios")
def hmc_list_optical_media(
    vios_name_or_uuid: str,
    vg_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List Virtual Optical Media in the Virtual Media Repository.

    Returns a list of optical media entries (ISO containers) with their
    name and size_mib (converted from the HMC's GiB Size).
    The repository must exist (VMLibrary on the specified Volume Group).

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume-group UUID containing the repository.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return [
        serialize_tool_result(entry)
        for entry in with_client(
            lambda hmc: list_optical_media(
                hmc, vios_name_or_uuid, vg_uuid, system_name_or_uuid=system_name_or_uuid
            ),
            profile=profile,
        )
    ]


@tool(effect="read", operation="storage.list_mappings", target_kind="vios")
def hmc_list_storage_mappings(
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List VirtualSCSIMappings on a VIOS, optionally filtered by LPAR.

    Returns storage mappings with backing storage details (PhysicalVolume or
    VirtualDisk) and client LPAR information. Each mapping's ``id`` is its
    ``<server adapter>/<target device>`` identity (for example ``vhost0/vtscsi0``),
    or null when the HMC reports too little to identify it.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        lpar_name_or_uuid: Optional LPAR name or UUID to scope mappings to a
            single client LPAR.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    return [
        serialize_tool_result(entry)
        for entry in with_client(
            lambda hmc: list_storage_mappings(
                hmc,
                vios_name_or_uuid,
                lpar_name_or_uuid,
                system_name_or_uuid=system_name_or_uuid,
            ),
            profile=profile,
        )
    ]


@tool(effect="destructive", operation="storage.detach_mapping", target_kind="vios")
def hmc_detach_storage_mapping(
    vios_name_or_uuid: str,
    mapping_id: str,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Detach a VirtualSCSIMapping by its inventory ID.

    Removes the mapping only; the backing storage (PhysicalVolume or
    VirtualDisk) is preserved. Returns ``mapping_id`` and ``change_location``:
    the client partition's CurrentProfileSync and where the removal lives.

    Args:
        system_name_or_uuid: Optional managed-system selector used to authorize the
            client LPAR; omit it to discover the owning system.
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        mapping_id: Exact ``id`` returned by ``hmc_list_storage_mappings``, for
            example ``vhost0/vtscsi0``.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    async def detach_mapping(hmc) -> dict[str, Any]:
        location = await detach_storage_mapping(
            hmc,
            vios_name_or_uuid,
            mapping_id,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        )
        return {"mapping_id": mapping_id, "change_location": location.raw}

    return with_client(detach_mapping, profile=profile)


@tool(effect="read", operation="cluster.list", target_kind="console")
def hmc_list_clusters(
    profile: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """List Clusters (sets of VIOS nodes sharing a storage pool).

    Args:
        profile: TOML profile name, or the environment-default HMC when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
    """

    return run_limited_collection(
        lambda hmc: hmc.list_clusters(), limit, profile=profile
    )


@tool(effect="read", operation="cluster.list_pools", target_kind="console")
def hmc_list_shared_storage_pools(
    profile: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List all Shared Storage Pools with capacity and logical-unit details.

    Args:
        profile: TOML profile name, or the environment-default HMC when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
    """

    return run_limited_collection(
        lambda hmc: hmc.list_shared_storage_pools(), limit, profile=profile
    )


@tool(effect="read", operation="cluster.get_pool", target_kind="shared_storage_pool")
def hmc_get_shared_storage_pool(
    ssp_uuid: str, profile: str | None = None
) -> dict[str, Any] | None:
    """Get one Shared Storage Pool by UUID, or None for an empty response.

    Args:
        ssp_uuid: Shared-storage-pool UUID from ``hmc_list_shared_storage_pools``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: hmc.get_shared_storage_pool(ssp_uuid), profile=profile
    )


@tool(effect="mutate", operation="cluster.create_logical_unit", target_kind="cluster")
def hmc_create_logical_unit(
    cluster_uuid: str,
    lu_name: str,
    lu_size_gib: int,
    lu_type: LuType = "THIN",
    device_type: DeviceType = "VirtualIO_Disk",
    cloned_from: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Create a Logical Unit (file-backed disk) in a Cluster/SSP.

    Submits a CreateLogicalUnit job and returns it — poll hmc_get_job for
    status; the result holds the new LU's UDID in LUCreated. lu_size_gib is the
    requested logical-unit size in GiB. lu_type is THIN
    or THICK; device_type is VirtualIO_Disk or VirtualIO_Image. cloned_from is
    an optional source LU UDID to clone. Find cluster_uuid with
    hmc_list_clusters.

    Set wait=True to block until the job reaches a terminal state.

    Args:
        cluster_uuid: Cluster UUID from ``hmc_list_clusters``.
        lu_name: Name for the new logical unit.
        lu_size_gib: Logical-unit capacity in gibibytes.
        lu_type: ``THIN`` for sparse allocation or ``THICK`` for full allocation.
        device_type: ``VirtualIO_Disk`` or ``VirtualIO_Image``.
        cloned_from: Optional source logical-unit UDID to clone.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: create_logical_unit(
            hmc,
            cluster_uuid,
            lu_name,
            lu_size_gib,
            lu_type,
            device_type,
            cloned_from=cloned_from,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(
    effect="destructive", operation="cluster.delete_logical_unit", target_kind="cluster"
)
def hmc_delete_logical_unit(
    cluster_uuid: str,
    lu_udid: str,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Delete a Logical Unit from a Cluster/SSP by its UDID.

    Submits a DeleteLogicalUnit job and returns it — poll hmc_get_job for
    status (an asynchronous delete, unlike the immediate delete tools).

    Set wait=True to block until the job reaches a terminal state.

    Args:
        cluster_uuid: Cluster UUID from ``hmc_list_clusters``.
        lu_udid: Logical-unit UDID to permanently delete.
        wait: Wait for the submitted job to reach a terminal state.
        timeout_seconds: Maximum wait duration in seconds.
        poll_interval: Seconds between job-status requests while waiting.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: delete_logical_unit(
            hmc,
            cluster_uuid,
            lu_udid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="media.upload_iso", target_kind="vios")
def hmc_upload_iso(
    vios_name_or_uuid: str,  # VIOS name or UUID to target
    vg_uuid: str,  # Volume Group UUID containing the media repository
    media_name: str,  # Target name for the ISO in the repository
    iso_source: str,  # http(s) URL to download the ISO from
    profile: str | None = None,  # HMC profile name (uses default if omitted)
    system_name_or_uuid: str | None = None,
) -> dict[str, Any]:
    """Upload an ISO to a VIOS media repository via the HMC web File API.

    The ISO is downloaded from an http(s) URL, which is the only accepted source:
    a path on the MCP server's filesystem is refused. The download runs from the
    MCP server's network position, so the URL's host must be on the operator's
    allowlist (`HMC_ISO_URL_ALLOWLIST`); with no allowlist configured every URL
    is refused, and redirects are never followed. Computes the SHA-256 and size
    of the downloaded bytes before upload, refuses a volume group without a media
    repository and name collisions, reports success only once the repository lists
    the media, and releases the HMC upload handle on every outcome. Returns the result data,
    including the HMC repository's media entry.

    Args:
        vios_name_or_uuid: VIOS name or UUID to target.
        system_name_or_uuid: Optional managed system that scopes a VIOS name.
        vg_uuid: Volume Group UUID containing the media repository.
        media_name: Target name for the ISO in the repository.
        iso_source: http(s) URL to download the ISO from, served by a host on
            the operator's `HMC_ISO_URL_ALLOWLIST`. Local filesystem paths are
            not accepted, and the ISO must be served at this URL rather than
            redirected from it.
        profile: HMC profile name (uses default if omitted).

    Returns:
        Dict with upload status, media name, size, `source_sha256`, and `media`.
        `source_sha256` is the SHA-256 of the bytes downloaded from the source
        URL, computed locally and not read back from the VIOS. `media` is the
        HMC REST repository entry; its `MountType` is the HMC's own value and can
        differ from the access mode VIOS `lsrep` reports.

    Raises:
        HMCError: For HMC API errors during the upload, or when the repository
            does not list the media after it.
        ValueError: If iso_source is not an http(s) URL, if its host is not on
            the operator's allowlist, if the server answers with a redirect, if
            the download exceeds the size bound, or if the volume group holds no
            media repository.
        FileExistsError: If media_name already exists in the repository.
    """

    async def operation(hmc):
        return (
            await upload_iso(
                hmc,
                vios_name_or_uuid,
                vg_uuid,
                media_name,
                iso_source,
                system_name_or_uuid=system_name_or_uuid,
            )
        ).raw

    return with_client(
        operation,
        profile=profile,
    )


@tool(effect="read", operation="media.list_mappings", target_kind="vios")
def hmc_list_optical_mappings(
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
    profile: str | None = None,
    limit: int | None = None,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List VirtualSCSIMappings for optical media on a VIOS, optionally filtered by LPAR.

    Returns only mappings that reference VirtualOpticalMedia backing, with media
    details and client LPAR information.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        lpar_name_or_uuid: Optional LPAR name or UUID to scope mappings to a
            single client LPAR.
        profile: TOML profile name, or the environment-default HMC when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def limited_optical_mappings(hmc):
        mappings = await list_optical_mappings(
            hmc,
            vios_name_or_uuid,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
        )
        return mappings if limit is None else mappings[:limit]

    return with_client(limited_optical_mappings, profile=profile)


@tool(effect="mutate", operation="media.mount", target_kind="vios")
def hmc_mount_optical_media(
    vios_name_or_uuid: str,
    media_name: str,
    lpar_name_or_uuid: str,
    target_device: str | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Create a VirtualSCSIMapping for optical media (mount ISO to LPAR).

    Creates a read-only optical mapping from a VirtualOpticalMedia (ISO container)
    to a client LPAR. The media_name must exist in the VIOS media repository.
    Returns the mapping plus ``change_location``: the partition's
    CurrentProfileSync and whether the HMC-created client adapter lives only in
    the current configuration (a profile activation then drops it).

    A partition not activated since its profile changed refuses the mount with
    500 REST0269; activate or apply a profile once first. That refusal can still
    leave a VIOS server adapter with no mapping, so the error names the
    ``lshwres -r virtualio --rsubtype scsi`` listing that shows it.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        media_name: Name of the VirtualOpticalMedia (ISO) in the repository.
        lpar_name_or_uuid: LPAR name or UUID to mount the media to.
        target_device: Optional vtscsi target device name to pin the mapping.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def mount(hmc: HMCClient) -> dict[str, Any]:
        result = await mount_optical_media(
            hmc,
            vios_name_or_uuid,
            lpar_name_or_uuid,
            media_name=media_name,
            target_device=target_device,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
        assert result.change_location is not None
        return resource_with_change_location(
            result.raw["resource"], result.change_location
        )

    return with_client(mount, profile=profile)


@tool(effect="destructive", operation="media.unmount", target_kind="vios")
def hmc_unmount_optical_media(
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    media_name: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> str:
    """Remove a VirtualSCSIMapping for optical media (unmount).

    Resolves exactly one LPAR-scoped optical inventory entry whose MediaName
    equals the supplied name, then removes that mapping by its UUID. Empty,
    missing, ambiguous, or malformed identities fail without rewriting the
    VIOS. The backing VirtualOpticalMedia (ISO container) is preserved and can
    be remounted later. No unload-without-detach path has been identified on
    the surveyed firmware, so this is also how you detach an optical mapping.
    The message ends with where the removal lives (CurrentProfileSync).

    The read-modify-write also rewrites the whole VirtualIOServer document from
    a GET snapshot, so another writer's change in that window is lost; ADR 0079
    requires callers to serialize concurrent VIOS mapping changes.

    Args:
        vios_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        lpar_name_or_uuid: LPAR name or UUID the media is mounted to.
        media_name: Exact ``MediaName`` of the VirtualOpticalMedia (ISO) to
            unmount, as reported by ``hmc_list_optical_mappings``. The mapping
            must exist exactly once for the selected LPAR.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def unmount_media_and_confirm(hmc: HMCClient):
        location = await unmount_optical_media(
            hmc,
            vios_name_or_uuid,
            lpar_name_or_uuid,
            media_name=media_name,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
        assert location.change_location is not None
        return (
            f"Unmounted {media_name!r} from LPAR {lpar_name_or_uuid} on VIOS "
            f"{vios_name_or_uuid}. {location.change_location.summary()}"
        )

    return with_client(unmount_media_and_confirm, profile=profile)
