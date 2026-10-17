"""Presentation-neutral VIOS storage operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

import httpx

from hmcpctl.client.client_storage import mapping_lpar_uuid, storage_mapping_id
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profile_sync import ChangeLocation, read_change_location

from ...config import ISO_URL_ALLOWLIST_HELP
from ...documents.storage import StorageKind
from ...errors import HMCError
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_vios_uuid

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageMapResult:
    """Authorized LPAR identity, the resulting VIOS storage resource, and where it lives."""

    lpar_uuid: str
    resource: dict[str, Any] | None
    change_location: ChangeLocation


@dataclass(frozen=True)
class VolumeGroup:
    """Stable inventory projection for one VIOS volume group."""

    uuid: str
    name: str
    capacity_gib: float | None
    free_space_gib: float | None
    free_space_diagnostic: str | None


@dataclass(frozen=True)
class OpticalMedia:
    """Stable inventory projection for one virtual optical medium."""

    name: str
    size_mib: float | None


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


def _volume_group(entry: Mapping[str, Any]) -> VolumeGroup:
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


def _optical_media(entry: Mapping[str, Any]) -> OpticalMedia:
    operation = "list_optical_media"
    resource = _resource(entry, operation)
    return OpticalMedia(
        name=_required_text(resource, "MediaName", operation),
        # The live VirtualOpticalMedia carries Size, in GiB (#963).
        size_mib=_optional_gib_as_mib(resource, "Size", operation),
    )


def _storage_mapping(entry: Mapping[str, Any]) -> StorageMapping:
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


# HTTP download configuration
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 300.0
MAX_DOWNLOAD_SIZE_BYTES = 100 * 1024 * 1024 * 1024  # 100 GiB
DEFAULT_CHUNK_SIZE = 8192
# The upload's chunk is a *wire* unit, not a read hint: httpcore issues one
# `network_stream.write` per chunk an async iterator yields
# (`httpcore/_async/http11.py:157-166`), where a `bytes` body produced one write
# for the whole payload. 8 KiB would make a 20 GiB ISO 2.6 million writes.
# 64 KiB is httpx's own streaming unit (`AsyncIteratorByteStream.CHUNK_SIZE`).
UPLOAD_CHUNK_SIZE = 64 * 1024
# The 2026-09-23 upload was listed on the first poll; the bound keeps a lost
# import from holding the call open (ADR 0177).
VISIBILITY_POLLS = 12
VISIBILITY_POLL_SECONDS = 5.0
_ACCEPTED_NOTE = (
    "The HMC accepted the ISO bytes; the media may or may not reach the repository. "
    "Check list-optical-media before retrying."
)


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
    return [_volume_group(entry) for entry in await hmc.list_volume_groups(vios_uuid)]


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
) -> dict[str, Any] | None:
    """Create a volume group from the selected physical volumes.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        HMCError: If the HMC rejects the creation request or it cannot be completed.
    """
    return await hmc.create_volume_group(
        await resolve_vios_uuid(
            hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        name,
        physical_volumes,
    )


async def create_virtual_disk(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    name: str,
    capacity_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create a virtual disk of ``capacity_mib`` in a volume group.

    Raises:
        ResourceNotFoundError: If the VIOS or optional managed-system selector cannot
            be resolved.
        HMCError: If the HMC rejects the creation request or it cannot be completed.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return await hmc.create_virtual_disk(
        vios_uuid,
        vg_uuid,
        name,
        capacity_mib,
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
) -> dict[str, Any] | None:
    """Delete a Virtual Disk from a Volume Group.

    Validates that the disk is not mapped to any LPAR before deletion.

    Args:
        hmc: HMC client instance.
        system_name_or_uuid: Optional managed-system name or UUID used to scope
            a VIOS name.
        vios_name_or_uuid: VIOS partition name or UUID.
        vg_uuid: Volume Group UUID containing the disk.
        disk_name: Name of the Virtual Disk to delete.

    Returns:
        The parsed HMC response entry from the VolumeGroup update, or ``None``
        when a successful response contains no resource entry.

    Raises:
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

    return await hmc.delete_virtual_disk(vios_uuid, vg_uuid, disk_name)


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
) -> StorageMapResult:
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
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.map_storage_to_lpar(
        vios_uuid, kind, storage_name, lpar_uuid, target
    )
    return StorageMapResult(lpar_uuid, resource, location)


async def create_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    size_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create a media repository in a VIOS volume group."""
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return await hmc.create_media_repository(
        vios_uuid,
        vg_uuid,
        size_mib,
    )


async def create_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    name: str,
    size_mib: int,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Create blank optical media in a VIOS media repository."""
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return await hmc.create_optical_media(
        vios_uuid,
        vg_uuid,
        name,
        size_mib,
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
        _storage_mapping(entry)
        for entry in await hmc.list_storage_mappings(vios_uuid, lpar_uuid)
    ]


async def detach_storage_mapping(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    mapping_id: str,
    *,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> ChangeLocation:
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
        system_name_or_uuid,
        lpar_uuid,
        ownership_override=ownership_override,
    )
    location = await read_change_location(hmc, lpar_uuid)
    await hmc.delete_storage_mapping(vios_uuid, mapping_id, lpar_uuid)
    return location


async def delete_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> str:
    """Delete the Virtual Media Repository from a Volume Group.

    Refuses deletion if the repository contains any VirtualOpticalMedia
    entries (ISO images). Delete all media first.

    Raises:
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
    return vios_uuid


async def delete_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    media_name: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Delete a VirtualOpticalMedia (ISO image) from the media repository.

    Validates that no optical mapping references the media before deletion.
    Returns an error listing every blocking mapping when the image is in use.

    Raises:
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

    return await hmc.delete_optical_media(vios_uuid, vg_uuid, media_name)


async def get_media_repository(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get the Virtual Media Repository (VMLibrary) from a Volume Group.

    Returns the repository with capacity (RepositorySize, in GiB) and optionally
    embedded VirtualOpticalMedia entries if present.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return await hmc.get_media_repository(
        vios_uuid,
        vg_uuid,
    )


ACCEPTED_ISO_SCHEMES = ("http", "https")


def _require_http_url(iso_source: str) -> str:
    """Return an http(s) source; reject controls as ``HMCError``, scheme as ``ValueError``.

    This is the first half of ``upload_iso``'s input validation — the host check
    in ``_require_allowlisted_iso_url`` is the other — and it runs before the
    operation touches the filesystem, the network, or the HMC. ADR 0049
    records why: anything that is not an http(s) URL used to be read as a path on
    the MCP server's own host, so a caller holding a grant for the tool could
    have any file the server process could read uploaded into a VIOS media
    repository (#261). Refusing by scheme, first, closes that by construction —
    there is no path for traversal or a symlink to escape through.

    The message is identical whether or not a file exists at the rejected value,
    and is derived only from the caller's own input, so a refusal discloses
    nothing about the server's filesystem.
    """
    if any(char.isascii() and not char.isprintable() for char in iso_source):
        raise HMCError(
            "ISO download refused: the URL contains a non-printable ASCII character."
        )
    if urlparse(iso_source).scheme not in ACCEPTED_ISO_SCHEMES:
        accepted = " or ".join(f"{scheme}://" for scheme in ACCEPTED_ISO_SCHEMES)
        raise ValueError(
            f"iso_source must be an {accepted} URL: got {iso_source!r}. "
            "The ISO is downloaded over HTTP(S); a path on the MCP server's "
            "filesystem is not an accepted source. Publish the ISO on a web "
            "server reachable from the MCP server and pass its URL."
        )
    return iso_source


DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _require_allowlisted_iso_url(
    iso_url: str, allowlist: tuple[tuple[str, int | None], ...]
) -> str:
    """Return ``iso_url`` if its host is on the operator's allowlist, else raise.

    ``hmc_upload_iso`` fetches from the MCP server's network position, so a
    caller who chooses the URL chooses a destination inside the server's segment
    — instance-metadata endpoints, loopback services, hosts the caller cannot
    route to (#303). Nothing in the access policy bounds that: no ``TargetKind``
    names a network endpoint the server can reach (ADR 0039). Only the operator
    knows which ISO servers are legitimate, so only the operator can say.

    **An unset allowlist refuses every URL.** There is no safe default
    destination to fall back to, and a default that fetched anything would be
    the defect. The message names the setting because a fail-closed default that
    produces an opaque error is a support burden.

    The check runs before the fetch and resolves no name, so a refused host that
    exists and one that does not are refused identically — in message, because
    the text is derived only from the caller's input and the operator's
    allowlist, and in timing, because neither DNS nor a socket is touched.
    """
    if not allowlist:
        raise ValueError(
            f"iso_source is refused: got {iso_url!r}, and no ISO download "
            "allowlist is configured. hmc_upload_iso downloads from the MCP "
            "server's network position, so it refuses every URL until an "
            f"operator names the ISO servers it may reach. {ISO_URL_ALLOWLIST_HELP}"
        )
    parsed = urlparse(iso_url)
    try:
        port = parsed.port
    except ValueError:
        port = 0
    if port == 0:
        # An unusable port never reaches the allowlist comparison: falling back
        # to the scheme default here would let ``host:99999`` match an allowlist
        # entry pinned to ``host:443``.
        raise ValueError(
            f"iso_source is refused: got {iso_url!r}, which does not carry a "
            "usable TCP port. Pass an http(s) URL whose port is between 1 and "
            "65535, or omit the port to use the scheme's default."
        )
    host = parsed.hostname
    effective_port = (
        port if port is not None else DEFAULT_SCHEME_PORTS.get(parsed.scheme.lower())
    )
    for allowed_host, allowed_port in allowlist:
        if host == allowed_host and allowed_port in (None, effective_port):
            try:
                httpx.URL(iso_url)
            except httpx.InvalidURL as exc:
                raise HMCError(
                    "ISO download refused: the request URL could not be built."
                ) from exc
            return iso_url
    permitted = ", ".join(
        host_ if port_ is None else f"{host_}:{port_}" for host_, port_ in allowlist
    )
    raise ValueError(
        f"iso_source is refused: got {iso_url!r}, whose host is not on the ISO "
        f"download allowlist. Permitted: {permitted}. Publish the ISO on one of "
        "those hosts, or have an operator add this one. " + ISO_URL_ALLOWLIST_HELP
    )


async def _download_iso_from_url(url: str) -> tuple[Path, str, int]:
    """Download an ISO file from an HTTP(S) URL with bounds and validation.

    The scheme and the host are the caller's responsibility: ``upload_iso``
    admits only what ``_require_http_url`` and ``_require_allowlisted_iso_url``
    accept, and it is this function's only caller.

    **Redirects are not followed.** Following them would send the fetch to a URL
    the allowlist never saw: a permitted host that answers ``302
    http://169.254.169.254/…`` would defeat a check applied to the URL the
    caller passed (#303). A redirect response is refused rather than followed,
    so the URL fetched is always the URL checked. ``raise_for_status`` does not
    cover this — 3xx is not an error status — so the refusal is explicit.

    Args:
        url: HTTP(S) URL to download the ISO from.

    Returns:
        Tuple of (temp_file_path, sha256_hexdigest, file_size_bytes).

    Raises:
        ValueError: If the response is a redirect, or the download exceeds
            ``MAX_DOWNLOAD_SIZE_BYTES``.
        httpx.TimeoutException: If connection or read timeout is exceeded.
        httpx.HTTPStatusError: If HTTP request fails (4xx/5xx).
    """
    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
    async with (
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as client,
        client.stream("GET", url) as response,
    ):
        # Every 3xx, not just httpx's `is_redirect` (which additionally
        # requires a Location header): a 3xx without one is not a body to
        # import into a media repository either.
        if 300 <= response.status_code < 400:
            raise ValueError(
                f"iso_source is refused: {url!r} answered with HTTP "
                f"{response.status_code}, a redirect. The ISO must be served "
                "at the URL you pass, because a redirect would move the "
                "download to a host the operator's allowlist never checked. "
                "Pass the URL the ISO is actually served from."
            )
        response.raise_for_status()

        import tempfile

        fd, temp_path = tempfile.mkstemp(suffix=".iso", prefix="hmc_upload_")
        temp_file = Path(temp_path)

        sha256_hash = hashlib.sha256()
        downloaded_size = 0

        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE):
                    downloaded_size += len(chunk)
                    if downloaded_size > MAX_DOWNLOAD_SIZE_BYTES:
                        raise ValueError(
                            f"Download size {downloaded_size} bytes exceeds "
                            f"maximum allowed size of {MAX_DOWNLOAD_SIZE_BYTES} bytes"
                        )

                    f.write(chunk)
                    sha256_hash.update(chunk)

            iso_sha256 = sha256_hash.hexdigest()
            return temp_file, iso_sha256, downloaded_size

        except Exception:
            # Clean up temp file on any error
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "failed to remove incomplete ISO download %s", temp_file
                )
            raise


async def _aiter_file_chunks(
    handle: BinaryIO, chunk_size: int = UPLOAD_CHUNK_SIZE
) -> AsyncIterator[bytes]:
    """Yield *handle*'s remaining bytes in ``chunk_size`` pieces.

    The upload body httpx accepts from an ``AsyncClient`` is an async iterator —
    a file object or a sync generator raises ``RuntimeError`` at send time — so
    the file is read here rather than handed over. *handle* is left open and
    closed by the caller, which is what guarantees the descriptor is released on
    every outcome rather than at some later finalization of this generator.
    """
    while chunk := await asyncio.to_thread(handle.read, chunk_size):
        yield chunk


async def list_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> list[OpticalMedia]:
    """List Virtual Optical Media in the Virtual Media Repository.

    Returns a list of optical media entries (ISO containers) with their
    name, size in MiB (converted from the HMC's GiB Size), and media type.
    The repository must exist (VMLibrary on the specified Volume Group).
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    return [
        _optical_media(entry)
        for entry in await hmc.list_optical_media(vios_uuid, vg_uuid)
    ]


async def _refuse_existing_media(
    hmc: HMCClient, vios_uuid: str, vg_uuid: str, media_name: str
) -> None:
    """Refuse a media name the repository already lists."""
    for media in await hmc.list_optical_media(vios_uuid, vg_uuid):
        if media.get("MediaName") == media_name:
            raise FileExistsError(
                f"Media name '{media_name}' already exists in repository. "
                "Use a different name or delete the existing media first."
            )


async def _wait_for_media(
    hmc: HMCClient, vios_uuid: str, vg_uuid: str, media_name: str
) -> dict[str, Any]:
    """Return the repository entry for *media_name* once the HMC lists it."""
    try:
        for poll in range(VISIBILITY_POLLS):
            if poll:
                await asyncio.sleep(VISIBILITY_POLL_SECONDS)
            for media in await hmc.list_optical_media(vios_uuid, vg_uuid):
                if media.get("MediaName") == media_name:
                    return media
    except HMCError as exc:
        exc.add_note(_ACCEPTED_NOTE)
        raise
    raise HMCError(
        f"ISO {media_name!r} was uploaded, but volume group {vg_uuid} did not list it "
        f"after {VISIBILITY_POLLS} checks. {_ACCEPTED_NOTE}"
    )


async def _upload_iso_via_web_file(
    hmc: HMCClient,
    vios_uuid: str,
    vg_uuid: str,
    media_name: str,
    iso_path: Path,
    file_size: int,
) -> dict[str, Any]:
    """Create a web File, stream the ISO into it, wait for the media, release the File."""
    file_uuid: str | None = None
    try:
        # Again after the download: the visibility check matches by name, so a
        # same-named media added meanwhile would pass for this upload.
        await _refuse_existing_media(hmc, vios_uuid, vg_uuid, media_name)
        file_uuid = await hmc._web_file_create(vios_uuid, media_name, file_size)
        with iso_path.open("rb") as handle:
            try:
                await hmc._web_file_upload(
                    file_uuid, _aiter_file_chunks(handle), file_size
                )
            except HMCError as exc:
                if exc.status_code is not None and exc.status_code >= 500:
                    exc.add_note(_ACCEPTED_NOTE)
                raise
        return await _wait_for_media(hmc, vios_uuid, vg_uuid, media_name)
    finally:
        primary_error = sys.exception()
        if file_uuid:
            try:
                await hmc._web_file_delete(file_uuid)
            except Exception as exc:
                if primary_error is None:
                    exc.add_note(
                        f"ISO {media_name!r} is in the repository; only the release failed."
                    )
                    raise
                logger.exception("web File delete failed for ISO upload %s", file_uuid)


async def upload_iso(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    vg_uuid: str,
    media_name: str,
    iso_source: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any]:
    """Upload an ISO to a VIOS media repository via the HMC web File API (ADR 0177).

    ``iso_source`` must be an ``http`` or ``https`` URL whose host the operator
    has put on ``iso_url_allowlist`` (``HMC_ISO_URL_ALLOWLIST``), and both
    conditions are checked before any other work happens. **With no allowlist
    configured every URL is refused** — see ``_require_allowlisted_iso_url`` and
    ADR 0050. The media name is then validated against a conservative assumed
    grammar (ADR 0177), the volume group must hold a media repository, and a name
    that collides with existing media is refused, all before any transfer begins. The
    ISO is downloaded with explicit timeout and size bounds and without following
    redirects, with SHA-256 and size computed from the download. It is reported
    uploaded only once the repository lists it. The local temp file and the HMC
    web File are both released on every outcome.

    Args:
        hmc: HMC client instance.
        vios_name_or_uuid: VIOS name or UUID.
        vg_uuid: Volume Group UUID containing the media repository.
        media_name: Target name for the ISO in the repository.
        iso_source: HTTP(S) URL to download the ISO from.
        system_name_or_uuid: Optional managed-system name or UUID used to scope
            a VIOS name.

    Returns:
        Dict with:
        - 'status': 'uploaded'
        - 'media_name': Name of the media in the repository.
        - 'media_size_bytes': Size of the downloaded ISO.
        - 'source_sha256': SHA-256 of the bytes downloaded from the source URL.
          It is computed locally, not read back from the VIOS, so it shows the
          download was intact and nothing about the media the VIOS holds.
        - 'media': The HMC REST repository entry for the uploaded media. Its
          ``MountType`` is the HMC's own value and can differ from the access
          mode VIOS ``lsrep`` reports (for example ``rw`` here, ``ro`` there).

    Raises:
        HMCError: For malformed ISO URLs, HMC API errors during the web File
                  requests, or a repository that does not list the media after
                  the upload.
        ValueError: If ``iso_source`` is not an http(s) URL, if its host is not
                   on the operator's allowlist (including the unset allowlist,
                   which permits nothing), if the server answers with a
                   redirect, if the download exceeds the size bound, or if the
                   volume group holds no media repository.
        FileExistsError: If media_name already exists in the repository.
    """
    iso_url = _require_allowlisted_iso_url(
        _require_http_url(iso_source), hmc.config.iso_url_allowlist_entries
    )
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )

    # An uncaptured assumption (ADR 0177): no capture or reference states the HMC's
    # Filename grammar. The pattern is a conservative client-side allowlist, and no
    # name outside it is known to have been sent to an HMC, so the HMC may accept more.
    _MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,79}$")
    if not _MEDIA_NAME_RE.match(media_name):
        raise ValueError(
            f"media_name {media_name!r} is refused by hmcpctl before any request. "
            "It must match [A-Za-z0-9_.]{1,79} (no hyphens, spaces, or other special "
            "characters); this is a conservative rule hmcpctl assumes, not one the "
            "HMC documents, and the HMC may accept more."
        )

    vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg_uuid)
    if await hmc.get_media_repository(vios_uuid, vg_uuid) is None:
        raise ValueError(
            f"Volume group {vg_uuid} on VIOS {vios_uuid} holds no media repository. "
            "Pass the volume group that holds it, or create one with create-media-repo."
        )

    # Check for name collision before downloading anything — the check needs
    # only vios_uuid and vg_uuid, so a taken name is refused without the
    # transfer (#325).
    await _refuse_existing_media(hmc, vios_uuid, vg_uuid, media_name)

    try:
        iso_path, iso_sha256, file_size = await _download_iso_from_url(iso_url)
    except httpx.InvalidURL as exc:
        raise HMCError(
            "ISO download refused: the request URL could not be built."
        ) from exc

    try:
        uploaded_media_entry = await _upload_iso_via_web_file(
            hmc, vios_uuid, vg_uuid, media_name, iso_path, file_size
        )

        return {
            "status": "uploaded",
            "media_name": media_name,
            "media_size_bytes": file_size,
            "source_sha256": iso_sha256,
            "media": uploaded_media_entry,
        }
    finally:
        primary_error = sys.exception()

        if iso_path.exists():
            try:
                iso_path.unlink()
            except OSError as exc:
                if primary_error is None:
                    exc.add_note(
                        f"ISO upload completed, but temporary file cleanup failed: "
                        f"{iso_path}"
                    )
                    raise
                logger.exception("temporary ISO cleanup failed for %s", iso_path)


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
) -> StorageMapResult:
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
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.create_optical_mapping(
        vios_uuid, media_name, lpar_uuid, target_device
    )
    return StorageMapResult(lpar_uuid, resource, location)


async def unmount_optical_media(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    media_name: str,
    ownership_override: bool = False,
) -> ChangeLocation:
    """Remove the VirtualSCSIMapping for an optical device (unmount).

    Resolves the LPAR-scoped optical inventory entry whose ``MediaName`` equals
    ``media_name``, requires its ``<server adapter>/<target device>`` identity
    (ADR 0168), and removes it through the shared VirtualIOServer
    read-modify-write path. The backing
    VirtualOpticalMedia (ISO container) is preserved and can be remounted later.

    Removing the mapping is the whole unmount as this client implements it:
    mount_optical_media creates a VirtualSCSIMapping with the media referenced
    inside it, and no unload-without-detach path has been identified on the
    surveyed firmware, so detaching the mapping and unmounting the image are one
    operation here.  (#403 and ADR 0079 record only that a detailed
    VirtualSCSIMapping is not directly addressable; establishing the absence of
    an unload path would need its own live survey, on the ADR 0069 pattern.)

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
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
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
    return location
