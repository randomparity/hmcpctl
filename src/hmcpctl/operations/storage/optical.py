"""VIOS media repositories and optical devices."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from hmcpctl.client.client_storage import storage_mapping_id
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profiles.sync import read_change_location

from ...audit import records as audit
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_vios_uuid,
)
from ...ssh.transport import HMCCLIError, run_hmc_command
from ...ssh.vios_optical import (
    DEVICE_NAME,
    OpticalAction,
    optical_command,
    require_device_name,
    require_media_name,
)
from ...xmlutil import leaf_text
from .resources import (
    StorageMutationResult,
    _optional_number,
    _required_text,
    _resource,
    resolve_volume_group_uuid,
)


@dataclass(frozen=True, kw_only=True)
class StorageOpticalResult(StorageMutationResult):
    """An optical command's selected device, separate from its diagnostic payload."""

    target_device: str


@dataclass(frozen=True)
class OpticalMedia:
    """Stable inventory projection for one virtual optical medium."""

    name: str
    size_mib: float | None


def _optional_gib_as_mib(
    resource: Mapping[str, Any], field: str, operation: str
) -> float | None:
    """Read an optional GiB quantity and report it in MiB.

    Decimal arithmetic keeps a value such as 1.0801 GiB at exactly 1106.0224
    MiB, where float multiplication would add a binary rounding tail.
    """
    gib = _optional_number(resource, field, operation)
    if gib is None:
        return None
    mib = Decimal(str(gib)) * 1024
    return int(mib) if mib == mib.to_integral_value() else float(mib)


def _optical_media(entry: Mapping[str, Any]) -> OpticalMedia:
    operation = "list_optical_media"
    resource = _resource(entry, operation)
    return OpticalMedia(
        name=_required_text(resource, "MediaName", operation),
        # The live VirtualOpticalMedia carries Size, in GiB (#963).
        size_mib=_optional_gib_as_mib(resource, "Size", operation),
    )


async def create_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    size_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Create a media repository in a VIOS volume group.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the HMC request fails or returns an invalid resource shape.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return StorageMutationResult.from_raw(
        await hmc.create_media_repository(
            vios_uuid,
            vg_uuid,
            size_mib,
        )
    )


async def create_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    name: str,
    size_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Create blank optical media in a VIOS media repository.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the HMC request fails or returns an invalid resource shape.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return StorageMutationResult.from_raw(
        await hmc.create_optical_media(
            vios_uuid,
            vg_uuid,
            name,
            size_mib,
        )
    )


async def delete_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Delete the Virtual Media Repository from a Volume Group.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Refuses deletion if the repository contains any VirtualOpticalMedia
    entries (ISO images). Delete all media first.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the repository is not empty.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    media = await hmc.list_optical_media(vios_uuid, vg_uuid)
    if media:
        names = ", ".join(m.get("MediaName", "unknown") for m in media)
        raise HMCError(
            f"Cannot delete media repository: it contains {len(media)} "
            f"image(s): {names!r}. Delete all images first."
        )
    await hmc.delete_media_repository(vios_uuid, vg_uuid)
    return StorageMutationResult.from_raw(vios_uuid)


async def delete_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    media_name: str,
    *,
    system_name_or_uuid: str | None = None,
) -> StorageMutationResult:
    """Delete a VirtualOpticalMedia (ISO image) from the media repository.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Validates that no optical mapping references the media before deletion.
    Returns an error listing every blocking mapping when the image is in use.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the media is referenced by any optical mapping.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)

    # Exhaustively check optical mappings for references to this media
    optical_mappings = await hmc.list_optical_mappings(vios_uuid)
    blockers = []
    for mapping in optical_mappings:
        storage = mapping.get("Storage", {}).get("VirtualOpticalMedia", {})
        if isinstance(storage, dict):
            name = storage.get("MediaName", "")
            if name == media_name:
                lpar = mapping.get("AssociatedLogicalPartition", {})
                lpar_id = lpar.get("href", lpar.get("PartitionName", "unknown"))
                blockers.append(str(lpar_id))

    if blockers:
        raise HMCError(
            f"Cannot delete optical media {media_name!r}: it is mounted on "
            f"{len(blockers)} LPAR(s): {', '.join(blockers)!r}. "
            "Unmount the media first."
        )

    return StorageMutationResult.from_raw(
        await hmc.delete_optical_media(vios_uuid, vg_uuid, media_name)
    )


async def get_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get the VolumeGroup Atom entry containing a Virtual Media Repository.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    The repository is nested at ``Resource.MediaRepositories.VirtualMediaRepository``.
    Its ``RepositorySize`` is in GiB; optional media entries are nested under
    ``OpticalMedia.VirtualOpticalMedia`` within the repository. Returns None when
    no repository-bearing VolumeGroup entry is available.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the HMC request fails or returns an invalid resource shape.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return await hmc.get_media_repository(
        vios_uuid,
        vg_uuid,
    )


async def list_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> list[OpticalMedia]:
    """List Virtual Optical Media in the Virtual Media Repository.

    ``vg_uuid`` accepts a volume-group name or UUID on the selected VIOS.

    Returns a list of optical media entries (ISO containers) with their
    name, size in MiB (converted from the HMC's GiB Size), and media type.
    The repository must exist (VMLibrary on the specified Volume Group).

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector
            cannot be resolved.
        ValueError: If the volume-group selector is missing or ambiguous, or
            its inventory contains incomplete identities.
        HMCError: If the HMC request fails or returns an invalid resource shape.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return [
        _optical_media(entry)
        for entry in await hmc.list_optical_media(vios_uuid, vg_uuid)
    ]


async def list_optical_mappings(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
    *,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List VirtualSCSIMappings for optical media on a VIOS, optionally scoped to an LPAR.

    Returns only mappings that reference VirtualOpticalMedia backing, with media
    details and client LPAR information. Use lpar to scope mappings to a single partition
    by name or UUID.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar_uuid = None
    if lpar_name_or_uuid:
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
    return await hmc.list_optical_mappings(vios_uuid, lpar_uuid)


async def mount_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    media_name: str,
    target_device: str | None = None,
    ownership_override: bool = False,
) -> StorageMutationResult:
    """Create a VirtualSCSIMapping for optical media (mount ISO to LPAR).

    Creates a read-only optical mapping from a VirtualOpticalMedia (ISO container)
    to a client LPAR. The media_name must exist in the VIOS media repository.
    target_device optionally pins the vtscsi name. Returns the created mapping
    resource and where the HMC-created client adapter lives (#981).

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
    entry = await hmc.create_optical_mapping(
        vios_uuid, media_name, lpar_uuid, target_device
    )
    resource = entry["Resource"] if entry is not None else None
    return StorageMutationResult.from_raw(
        resource,
        lpar_uuid=lpar_uuid,
        change_location=location,
    )


async def unmount_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    media_name: str,
    ownership_override: bool = False,
) -> StorageMutationResult:
    """Remove the VirtualSCSIMapping for an optical device (unmount).

    Resolves the LPAR-scoped optical inventory entry whose ``MediaName`` equals
    ``media_name``, requires its ``<server adapter>/<target device>`` identity
    (ADR 0168), and removes it through the shared VirtualIOServer
    read-modify-write path. The backing
    VirtualOpticalMedia (ISO container) is preserved and can be remounted later.

    Removing the mapping is the whole unmount as this client implements it:
    mount_optical_media creates a VirtualSCSIMapping with the media referenced
    inside it, so detaching the mapping and unmounting the image are one
    operation here. To change the loaded medium without detaching, use
    :func:`load_optical_media` and :func:`unload_optical_media` (ADR 0317). A
    mapping left with no medium by an unload has no ``MediaName`` to match, so
    load a medium into it before unmounting it.

    Empty, missing, ambiguous, or malformed identities fail without a POST, as
    required by ADR 0079.

    The read-modify-write rewrites the whole VirtualIOServer document from a GET
    snapshot, so another writer's change in that window is lost; ADR 0079 puts
    the duty to serialize concurrent VIOS mapping changes on the caller.

    Raises:
        ResourceNotFoundError: If a supplied VIOS, LPAR, or managed-system selector
            cannot be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        ValueError: If the mapping identity or selector scope cannot be verified.
        HMCError: If the HMC rejects the deletion or it cannot be completed.
    """
    if not media_name:
        raise ValueError("Optical media name must not be empty")

    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    matches: list[dict[str, Any]] = []
    for mapping in await hmc.list_optical_mappings(vios_uuid, lpar_uuid):
        if not isinstance(mapping, dict):
            continue
        storage = mapping.get("Storage")
        optical = (
            storage.get("VirtualOpticalMedia") if isinstance(storage, dict) else None
        )
        if isinstance(optical, dict) and optical.get("MediaName") == media_name:
            matches.append(mapping)

    if not matches:
        raise HMCError(
            f"Optical mapping for media {media_name!r} was not found on "
            f"LPAR {lpar_name_or_uuid!r}"
        )
    if len(matches) > 1:
        raise HMCError(
            f"Optical mapping for media {media_name!r} on LPAR "
            f"{lpar_name_or_uuid!r} is ambiguous"
        )

    mapping_id = storage_mapping_id(matches[0])
    if mapping_id is None:
        raise HMCError("VirtualSCSIMapping has no adapter/target identity")
    location = await read_change_location(hmc, lpar_uuid)
    await hmc.delete_storage_mapping(vios_uuid, mapping_id, lpar_uuid)
    return StorageMutationResult(None, change_location=location)


#: The only answer ``viosvrcmd`` gave to a successful ``loadopt``/``unloadopt`` in the
#: #1284 capture (cli-vios-loadopt, cli-vios-unloadopt and their after-power-off pair).
_OPTICAL_SUCCESS = ":"
_OPTICAL_OUTPUT_BYTES = 65536


async def _optical_device(
    hmc: HMCClient,
    vios_uuid: str,
    lpar_uuid: str,
    lpar_label: str,
    target_device: str | None,
) -> str:
    """The virtual optical device the LPAR's mapping on this VIOS owns (ADR 0317).

    Reads every mapping of the LPAR, so a device holding no medium is found too.
    """
    names: list[str] = []
    for mapping in await hmc.list_storage_mappings(vios_uuid, lpar_uuid):
        target = mapping.get("TargetDevice") if isinstance(mapping, dict) else None
        device = (
            target.get("VirtualOpticalTargetDevice")
            if isinstance(target, dict)
            else None
        )
        name = leaf_text(device.get("TargetName")) if isinstance(device, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    listed = ", ".join(sorted(names)) or "none"
    if target_device is not None:
        if target_device not in names:
            raise HMCError(
                f"Virtual optical device {target_device!r} is not mapped to LPAR "
                f"{lpar_label!r} on this VIOS; its optical devices: {listed}"
            )
        return target_device
    if len(names) != 1:
        raise HMCError(
            f"LPAR {lpar_label!r} has {len(names)} virtual optical devices on this "
            f"VIOS ({listed}); name one with target_device"
        )
    if not DEVICE_NAME.fullmatch(names[0]):
        raise HMCError(
            f"Mapped virtual optical device {names[0]!r} is outside the device-name "
            "rule [A-Za-z0-9_]{1,15}; these tools cannot address it"
        )
    return names[0]


async def _vios_native_identity(hmc: HMCClient, vios_uuid: str) -> tuple[str, int]:
    """The managed-system name and partition id ``viosvrcmd`` addresses the VIOS by."""
    entry = await hmc.get_vios(vios_uuid)
    resource = entry.get("Resource") if isinstance(entry, dict) else None
    resource = resource if isinstance(resource, dict) else {}
    native = leaf_text(resource.get("PartitionID"))
    link = resource.get("AssociatedManagedSystem")
    href = link.get("href") if isinstance(link, Mapping) else None
    _, marker, system_uuid = (
        urlparse(href).path.rpartition("/ManagedSystem/")
        if isinstance(href, str)
        else ("", "", "")
    )
    if (
        not isinstance(native, str)
        or not re.fullmatch(r"[1-9][0-9]*", native)
        or not marker
        or not is_uuid(system_uuid)
    ):
        raise HMCError(
            f"VIOS {vios_uuid} has no usable PartitionID or AssociatedManagedSystem "
            "link; nothing was submitted"
        )
    return await resolve_system_name(hmc, system_uuid), int(native)


async def _change_optical_media(
    hmc: HMCClient,
    action: OpticalAction,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    media_name: str | None,
    system_name_or_uuid: str | None,
    target_device: str | None,
    release: bool,
    ownership_override: bool,
) -> StorageOpticalResult:
    if media_name is not None:
        require_media_name(media_name)
    if target_device is not None:
        require_device_name(target_device)
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    device = await _optical_device(
        hmc, vios_uuid, lpar_uuid, lpar_name_or_uuid, target_device
    )
    system_name, vios_id = await _vios_native_identity(hmc, vios_uuid)
    command = optical_command(
        system_name, vios_id, action, device, media_name, release=release
    )
    if release:
        audit.record_optical_release(
            action=action,
            system=system_name,
            vios=vios_uuid,
            lpar=lpar_uuid,
            target_device=device,
            media=media_name,
            host=hmc.config.host,
            agent_id=hmc.config.agent_id or "hmcpctl",
        )
    where = f"Optical {action} of {device} on VIOS {vios_uuid}"
    read_first = (
        "read the device with hmc_list_storage_mappings before retrying. "
        "Nothing was retried"
    )
    try:
        answer = await run_hmc_command(
            hmc.config, command, max_output_bytes=_OPTICAL_OUTPUT_BYTES
        )
    except HMCCLIError as error:
        if error.remote_nonzero_completed:
            raise HMCError(
                f"{where} was refused. Nothing was retried: {error}"
            ) from error
        raise HMCError(f"{where}: completion unknown; {read_first}: {error}") from error
    if answer.strip() != _OPTICAL_SUCCESS:
        raise HMCError(
            f"{where}: completion unknown, unexpected answer {answer[:512]!r}; "
            f"{read_first}"
        )
    return StorageOpticalResult(
        raw={
            "action": action,
            "vios_uuid": vios_uuid,
            "lpar_uuid": lpar_uuid,
            "target_device": device,
            "media_name": media_name,
            "release": release,
        },
        lpar_uuid=lpar_uuid,
        target_device=device,
    )


async def load_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    media_name: str,
    system_name_or_uuid: str | None = None,
    target_device: str | None = None,
    release: bool = False,
    ownership_override: bool = False,
) -> StorageOpticalResult:
    """Load a repository medium into the LPAR's existing virtual optical device.

    Runs the VIOS ``loadopt`` through ``viosvrcmd`` (ADR 0317); the mapping stays.
    The device is the one *target_device* names, or the LPAR's only optical device
    on this VIOS, and must belong to that LPAR's mapping, loaded or empty. The VIOS
    does not replace a loaded medium: unload first. *release* adds ``-release``,
    which forces the device free of a client reservation; it is audited
    (``optical-release``) before the command. The VIOS must be running with an
    active RMC connection. ADR 0079 puts serializing VIOS mapping changes on the
    caller.

    Raises:
        ValueError: If *media_name* or *target_device* is outside its name rule.
        ResourceNotFoundError: If a VIOS, LPAR, or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        HMCError: If the device cannot be selected, the VIOS identity is unusable,
            or the command was refused or its completion is unknown.
    """
    return await _change_optical_media(
        hmc,
        "load",
        vios_name_or_uuid,
        lpar_name_or_uuid,
        media_name=media_name,
        system_name_or_uuid=system_name_or_uuid,
        target_device=target_device,
        release=release,
        ownership_override=ownership_override,
    )


async def unload_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    target_device: str | None = None,
    release: bool = False,
    ownership_override: bool = False,
) -> StorageOpticalResult:
    """Unload the medium from the LPAR's virtual optical device, keeping the mapping.

    Runs the VIOS ``unloadopt`` through ``viosvrcmd`` (ADR 0317), selecting and
    checking the device as :func:`load_optical_media` does; *release* is the same
    audited ``-release``. The emptied mapping is listed only by
    ``list_storage_mappings``: :func:`list_optical_mappings` skips it and
    :func:`unmount_optical_media` cannot match it until a medium is loaded again.

    Raises:
        ValueError: If *target_device* is outside the device-name rule.
        ResourceNotFoundError: If a VIOS, LPAR, or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        HMCError: If the device cannot be selected, the VIOS identity is unusable,
            or the command was refused or its completion is unknown.
    """
    return await _change_optical_media(
        hmc,
        "unload",
        vios_name_or_uuid,
        lpar_name_or_uuid,
        media_name=None,
        system_name_or_uuid=system_name_or_uuid,
        target_device=target_device,
        release=release,
        ownership_override=ownership_override,
    )
