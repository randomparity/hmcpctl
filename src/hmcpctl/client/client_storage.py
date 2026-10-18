"""HMCClient storage mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for storage.
"""

from __future__ import annotations

import re as _re

# ElementTree is retained for element construction, traversal, typing, and
# serialization only. Every inbound HMC response is parsed with defusedxml.
import xml.etree.ElementTree as ET  # nosec B405
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from ..documents.storage import (
    STORAGE_KINDS,
    StorageKind,
    build_virtual_disk_element,
    build_virtual_optical_mapping_document,
    build_volume_group_document,
    build_vscsi_mapping_document,
    build_web_file_document,
)
from ..errors import HMCError, HMCTransportError
from ..resource_identity import is_uuid
from ..xmlutil import WEB_NS, element_to_dict, localname
from .client_contracts import StorageClient, _reject_non_uuid_path_argument
from .client_parse import _parse_feed

# HMC UOM namespace — used in read-modify-write VolumeGroup operations.
_UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_MEDIA_UOM = "application/vnd.ibm.powervm.uom+xml"
_WEB_FILE_PATH = "/rest/api/web/File"
_WEB_FILE_TYPE = "application/vnd.ibm.powervm.web+xml; type=File"
_UUID_PATTERN = _re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")


def _find_vios_element(root: ET.Element, vios_uuid: str) -> ET.Element:
    """Return the one VIOS resource and reject ambiguous or mismatched documents."""
    tag = f"{{{_UOM_NS}}}VirtualIOServer"
    resources = ([root] if root.tag == tag else []) + root.findall(f".//{tag}")
    if len(resources) != 1:
        raise HMCError(
            f"VirtualIOServer GET response contained {len(resources)} VIOS resources; "
            "expected exactly one",
            200,
            ET.tostring(root, encoding="unicode")[:500],
        )
    vios_elem = resources[0]
    # A live VIOS document carries no <UUID> child; its identity is
    # Metadata/Atom/AtomID, with PartitionUUID alongside carrying the same value (#979).
    # Fixtures that invent a <UUID> element describe no observed HMC response.
    atom_ids = vios_elem.findall(
        f"{{{_UOM_NS}}}Metadata/{{{_UOM_NS}}}Atom/{{{_UOM_NS}}}AtomID"
    )
    partition_uuids = vios_elem.findall(f"{{{_UOM_NS}}}PartitionUUID")
    # V10R3 prints partition UUIDs upper case and answers either case in a path.
    expected = vios_uuid.casefold()
    mismatched = (
        len(atom_ids) != 1
        or (atom_ids[0].text or "").strip().casefold() != expected
        or len(partition_uuids) > 1
        or (
            len(partition_uuids) == 1
            and (partition_uuids[0].text or "").strip().casefold() != expected
        )
    )
    if mismatched:
        raise HMCError(
            f"VirtualIOServer response identity does not match {vios_uuid!r}",
            200,
            ET.tostring(vios_elem, encoding="unicode")[:500],
        )
    return vios_elem


def _whole_gib(size_mib: int) -> int:
    """Convert a MiB size to the whole GiB that RepositorySize and media Size take.

    The HMC reads both fields as GiB. A size that is not a whole number of GiB
    is refused rather than sent as a fraction whose precision the HMC has not
    been observed to accept.
    """
    if size_mib <= 0 or size_mib % 1024:
        raise ValueError(
            f"size_mib must be a positive multiple of 1024 (whole GiB); got {size_mib}"
        )
    return size_mib // 1024


def _same_gib(stored: str | None, size_gib: int) -> bool:
    """Whether an HMC GiB text such as "7" or "7.0" equals ``size_gib``."""
    try:
        return stored is not None and Decimal(stored) == size_gib
    except InvalidOperation:
        return False


def _extract_optical_media(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return media entries from the repository nesting V10R3 sends.

    ``MediaRepositories/VirtualMediaRepository/OpticalMedia/VirtualOpticalMedia``
    is the only nesting captured (#1202) or documented
    (docs/refs/hmc-rest-api-p10/virtual-storage-management/215-virtual-media-repository.md).
    """
    optical_media: list[dict[str, Any]] = []
    for entry in entries:
        resource = entry.get("Resource")
        if not isinstance(resource, dict):
            continue
        repositories = resource.get("MediaRepositories")
        if not isinstance(repositories, dict):
            continue
        repository = repositories.get("VirtualMediaRepository")
        if not isinstance(repository, dict):
            continue
        media_container = repository.get("OpticalMedia")
        if not isinstance(media_container, dict):
            continue
        media = media_container.get("VirtualOpticalMedia", [])
        if isinstance(media, list):
            optical_media.extend(item for item in media if isinstance(item, dict))
        elif isinstance(media, dict):
            optical_media.append(media)
    return optical_media


def lpar_uuid_from_href(href: object) -> str | None:
    """Return the LPAR UUID ending an HMC ``.../LogicalPartition/<uuid>`` link, or None.

    The HMC links a mapping's client LPAR absolutely and system-scoped
    (``https://<hmc>/rest/api/uom/ManagedSystem/<sys>/LogicalPartition/<uuid>``), so
    only the final path segment after the marker identifies the partition (ADR 0168).
    """
    if not isinstance(href, str):
        return None
    _, marker, tail = urlparse(href).path.rpartition("/LogicalPartition/")
    return tail if marker and tail and "/" not in tail else None


def _system_uuid_from_vios(vios_elem: ET.Element) -> str | None:
    """The managed-system UUID from a fetched VIOS's own ``AssociatedManagedSystem`` link.

    Mirrors :func:`lpar_uuid_from_href`'s parsing rule (ADR 0168) for the sibling
    segment a mapping create's ``AssociatedLogicalPartition`` href must also carry
    (ADR 0179): only the final path segment identifies the system.
    """
    link = vios_elem.find(f"{{{_UOM_NS}}}AssociatedManagedSystem")
    href = link.get("href") if link is not None else None
    if not isinstance(href, str):
        return None
    _, marker, tail = urlparse(href).path.rpartition("/ManagedSystem/")
    return tail if marker and tail and "/" not in tail else None


def _device_name(value: object) -> str | None:
    # element_to_dict yields {"@attrs": ..., "text": ...} for a leaf carrying
    # attributes it does not ignore; the name is its text either way.
    text = value.get("text") if isinstance(value, Mapping) else value
    return text if isinstance(text, str) and text and "/" not in text else None


def storage_mapping_id(mapping: Mapping[str, Any]) -> str | None:
    """Return a VirtualSCSIMapping's ``<server adapter>/<target device>`` identity.

    The HMC sends no mapping UUID; the VIOS device names ``vhost0/vtscsi0`` identify
    it (ADR 0168). Returns None unless both names exist and ``TargetDevice`` holds
    exactly one device element.
    """
    adapter = mapping.get("ServerAdapter")
    target = mapping.get("TargetDevice")
    devices = (
        [value for key, value in target.items() if not key.startswith("@")]
        if isinstance(target, Mapping)
        else []
    )
    if (
        not isinstance(adapter, Mapping)
        or len(devices) != 1
        or not isinstance(devices[0], Mapping)
    ):
        return None
    adapter_name = _device_name(adapter.get("AdapterName"))
    target_name = _device_name(devices[0].get("TargetName"))
    return f"{adapter_name}/{target_name}" if adapter_name and target_name else None


def _filter_optical_mappings(
    mappings: list[dict[str, Any]], lpar_uuid: str | None
) -> list[dict[str, Any]]:
    """Keep optical-backed mappings, optionally scoped to one client LPAR."""
    optical = [
        mapping
        for mapping in mappings
        if isinstance(mapping.get("Storage"), dict)
        and "VirtualOpticalMedia" in mapping["Storage"]
    ]
    if lpar_uuid is None:
        return optical
    return [mapping for mapping in optical if _mapping_targets_lpar(mapping, lpar_uuid)]


def mapping_lpar_uuid(mapping: Mapping[str, Any]) -> str | None:
    """Return the client-LPAR UUID a parsed VirtualSCSIMapping links to, or None."""
    partition = mapping.get("AssociatedLogicalPartition")
    return lpar_uuid_from_href(
        partition.get("href") if isinstance(partition, Mapping) else None
    )


def _mapping_targets_lpar(mapping: Mapping[str, Any], lpar_uuid: str) -> bool:
    # The HMC links LPARs by upper-case UUID; a selector may use either case.
    linked = mapping_lpar_uuid(mapping)
    return linked is not None and linked.casefold() == lpar_uuid.casefold()


def _children_named(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if localname(child.tag) == name]


def _required_etag(etag: str | None) -> str:
    """The GET's ETag; a VolumeGroup write never falls back to an unconditional POST."""
    if not etag:
        raise HMCError(
            "VolumeGroup GET returned no ETag; refusing a whole-group write "
            "that could overwrite a concurrent change. Retry, and report the HMC version "
            "if it persists."
        )
    return etag


def _virtual_disks(vg_elem: ET.Element) -> ET.Element:
    """The group's VirtualDisks collection, appended (last in the XSD) when absent."""
    found = _children_named(vg_elem, "VirtualDisks")
    if found:
        return found[0]
    disks = ET.SubElement(
        vg_elem,
        f"{{{_UOM_NS}}}VirtualDisks",
        attrib={"kb": "CUD", "kxe": "false", "schemaVersion": "V1_0"},
    )
    ET.SubElement(ET.SubElement(disks, f"{{{_UOM_NS}}}Metadata"), f"{{{_UOM_NS}}}Atom")
    return disks


def _disks_named(disks: ET.Element, disk_name: str) -> list[ET.Element]:
    return [
        disk
        for disk in _children_named(disks, "VirtualDisk")
        if any(name.text == disk_name for name in _children_named(disk, "DiskName"))
    ]


def _append_mapping(
    operation: str, document_factory: Callable[[str], str]
) -> Callable[[ET.Element, str | None], None]:
    """A mutate callback that appends one parsed ``VirtualSCSIMapping`` (create).

    ``document_factory`` builds the mapping document from the managed-system UUID
    observed on the fetched VIOS (ADR 0179); it runs only once that UUID is known,
    which is why the document is built here rather than before the RMW GET. A VIOS
    response carrying no ``AssociatedManagedSystem`` link fails the create closed,
    before any POST.
    """

    def _mutate(mappings: ET.Element, system_uuid: str | None) -> None:
        if system_uuid is None or not is_uuid(system_uuid):
            raise HMCError(
                f"{operation}: VIOS has no usable AssociatedManagedSystem link; refusing "
                "to build a mapping's client-LPAR href without a managed-system UUID"
            )
        mappings.append(
            DET.fromstring(document_factory(system_uuid)).find(
                f".//{{{_UOM_NS}}}VirtualSCSIMapping"
            )
        )

    return _mutate


# A mapping create names no adapter, so the HMC pairs or creates one itself. In the
# #1085 window a create refused with 500 REST0269 still left a VIOS server adapter
# with no target device. A partition-scoped list-mappings does not show it and an
# unscoped one shows it with no identity (#1250), and the optical listing, which keeps
# only optical-backed mappings, never shows it; this read-only listing does (#1237).
_ADAPTER_SIDE_EFFECT = (
    "A failed mapping create can leave a VIOS virtual SCSI server adapter with no "
    "target device. A partition-scoped list-mappings does not show it, an unscoped one "
    "shows it with no identity, and hmc_list_optical_mappings never shows it. List the "
    "VIOS server adapters with "
    "`lshwres -r virtualio --rsubtype scsi -m <managed-system> --level lpar "
    "--filter lpar_names=<vios-name> -F slot_num,remote_lpar_name,remote_slot_num` "
    "and compare them with the mappings."
)

# The HMC's 500 body when a mapping create names a VTD another mapping holds,
# observed once on V10R3 (#1280); no capture pins the wording yet (#1284).
_VTD_NAME_IN_USE = "name is already used in another mapping"


def _optical_target_in_use(
    target_device: str | None,
    media_name: str | None = None,
    lpar_uuid: str | None = None,
) -> str:
    device = (
        f"Target device {target_device!r}" if target_device else "The target device"
    )
    lpar = f" to LPAR {lpar_uuid}" if lpar_uuid else ""
    media = f" with media {media_name!r}" if media_name else ""
    return (
        f"{device} is already mapped on the VIOS{lpar}{media}. "
        "Name a different target_device, "
        "or, if media is mapped to it, unmount that media first (unmount-optical-media); "
        "loading media into an existing device is not supported yet."
    )


def _refuse_mapped_optical_target(mappings: ET.Element, target_device: str) -> None:
    """Refuse, before any POST, a target device an optical mapping already names (#1283)."""
    for mapping in mappings.findall(f"{{{_UOM_NS}}}VirtualSCSIMapping"):
        name = mapping.find(
            f"{{{_UOM_NS}}}TargetDevice/{{{_UOM_NS}}}VirtualOpticalTargetDevice"
            f"/{{{_UOM_NS}}}TargetName"
        )
        if name is not None and name.text == target_device:
            media = mapping.findtext(
                f"{{{_UOM_NS}}}Storage/{{{_UOM_NS}}}VirtualOpticalMedia"
                f"/{{{_UOM_NS}}}MediaName"
            )
            partition = mapping.find(f"{{{_UOM_NS}}}AssociatedLogicalPartition")
            lpar = lpar_uuid_from_href(
                partition.get("href") if partition is not None else None
            )
            raise HMCError(_optical_target_in_use(target_device, media, lpar), 409)


async def _rmw_vios_mapping(
    client: StorageClient,
    operation: str,
    path: str,
    uuid_path_arguments: Mapping[str, str],
    mutate: Callable[[ET.Element, str | None], None],
    note: str | Callable[[HMCError], str] = "",
) -> str:
    """Read-modify-write the VIOS ``ViosSCSIMapping`` group under If-Match (ADR 0169).

    Fetches the grouped document, refuses before any POST when the GET has no
    ``ETag`` or no ``VirtualSCSIMappings`` collection, hands ``mutate`` the fetched
    collection (and the VIOS's own managed-system UUID, ADR 0179) to change in place
    (append a new mapping for a create, remove one for a detach), then posts the
    whole VIOS element back under the GET's ``ETag`` so the write never replaces
    mappings ``mutate`` did not touch. A 412 means the VIOS changed since the GET;
    nothing is written, and it is reported as a concurrent change rather than
    retried.
    """
    vios_uuid = uuid_path_arguments["vios_uuid"]
    got = await client._request_with_uuid_path_arguments(
        "GET",
        path,
        uuid_path_arguments=uuid_path_arguments,
        headers={"Accept": f"{_MEDIA_UOM}; type=VirtualIOServer"},
    )
    if got.status_code != 200:
        raise HMCError(f"GET {path} failed", got.status_code, got.text)
    etag = got.headers.get("ETag")
    if not etag:
        raise HMCError(
            f"GET {path} returned no ETag; refusing {operation} without If-Match",
            200,
            got.text[:500],
        )
    ET.register_namespace("", _UOM_NS)
    ET.register_namespace("atom", _ATOM_NS)
    try:
        vios_elem = _find_vios_element(DET.fromstring(got.text), vios_uuid)
    except (DET.ParseError, DefusedXmlException) as exc:
        raise HMCError(f"GET {path} response is not valid XML", 200, got.text) from exc
    mappings = vios_elem.find(f"{{{_UOM_NS}}}VirtualSCSIMappings")
    if mappings is None:
        raise HMCError(
            f"GET {path} returned no VirtualSCSIMappings; refusing {operation} "
            "because the post could replace the VIOS mapping set. If the VIOS has "
            "no mappings yet, create the first one on the VIOS (mkvdev) or in the "
            "HMC GUI, then retry",
            200,
            got.text[:500],
        )
    mutate(mappings, _system_uuid_from_vios(vios_elem))

    async def dispatch() -> str:
        response = await client._request_with_uuid_path_arguments(
            "POST",
            path,
            uuid_path_arguments=uuid_path_arguments,
            content=ET.tostring(vios_elem, encoding="unicode"),
            headers={
                "Accept": "*/*",
                "Content-Type": f"{_MEDIA_UOM}; type=VirtualIOServer",
                "If-Match": etag,
            },
        )
        if response.status_code == 412:
            raise HMCError(
                f"{operation}: VIOS {vios_uuid} mappings changed since they were "
                "read; nothing was written, re-run to retry",
                412,
                response.text,
            )
        if response.status_code not in (200, 201, 202):
            raise HMCError(f"POST {path} failed", response.status_code, response.text)
        return response.text

    return await client._reconcile_storage_mutation(
        operation, lambda: client.list_storage_mappings(vios_uuid), dispatch, note
    )


class StorageMixin:
    async def _web_file_create(
        self: StorageClient, vios_uuid: str, filename: str, size_bytes: int
    ) -> str:
        """Create the web File an ISO upload streams into; return its FileUUID (ADR 0177)."""
        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        response = await self._request(
            "PUT",
            _WEB_FILE_PATH,
            content=build_web_file_document(
                filename=filename, size_bytes=size_bytes, vios_uuid=vios_uuid
            ),
            headers=self._web_headers(
                {"Content-Type": _WEB_FILE_TYPE, "Accept": "*/*"}
            ),
        )
        if response.status_code not in (200, 201):
            raise HMCError(
                f"Web File create failed for {filename}",
                response.status_code,
                response.text,
            )
        try:
            identities = DET.fromstring(response.text).findall(
                f".//{{{WEB_NS}}}FileUUID"
            )
        except (ET.ParseError, DefusedXmlException) as exc:
            raise HMCError(
                "Web File create response is not XML",
                response.status_code,
                response.text,
            ) from exc
        file_uuid = (identities[0].text or "").strip() if len(identities) == 1 else ""
        if not _UUID_PATTERN.match(file_uuid):
            raise HMCError(
                "Web File create response must carry exactly one UUID FileUUID",
                response.status_code,
                response.text,
            )
        return file_uuid

    async def _web_file_upload(
        self: StorageClient,
        file_uuid: str,
        content: AsyncIterator[bytes],
        content_length: int,
    ) -> None:
        """Stream bounded ISO content into a web File; never buffer it (ADR 0052).

        The HMC answers only once it has taken the whole file, later the larger
        the ISO, so the read wait is ``max(timeout, upload_timeout)`` (ADR 0177, #1055).
        """
        read = max(self.config.timeout, self.config.upload_timeout)
        path = f"{_WEB_FILE_PATH}/contents/{file_uuid}"
        try:
            response = await self._request_with_uuid_path_arguments(
                "PUT",
                path,
                uuid_path_arguments={"file_uuid": file_uuid},
                content=content,
                headers=self._web_headers(
                    {
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(content_length),
                        "Accept": "*/*",
                    }
                ),
                timeout=httpx.Timeout(self.config.timeout, read=read),
            )
        except HMCTransportError as exc:
            if not isinstance(exc.__cause__, httpx.ReadTimeout):
                raise
            raise HMCTransportError(
                f"PUT {path} got no complete response within {read:g}s after streaming "
                "the ISO. The HMC may still import it; check list-optical-media before "
                f"retrying. Increase HMC_UPLOAD_TIMEOUT above {read:g} for a slower HMC."
            ) from exc
        if response.status_code not in (200, 201, 202, 204):
            raise HMCError(
                f"Web File upload failed for {file_uuid}",
                response.status_code,
                response.text,
            )

    async def _web_file_delete(self: StorageClient, file_uuid: str) -> None:
        """Release a web File after an ISO upload; an absent File is already released."""
        response = await self._request_with_uuid_path_arguments(
            "DELETE",
            f"{_WEB_FILE_PATH}/{file_uuid}",
            uuid_path_arguments={"file_uuid": file_uuid},
            headers=self._web_headers({"Accept": "*/*"}),
        )
        if response.status_code not in (200, 202, 204, 404):
            raise HMCError(
                f"Web File delete failed for {file_uuid}",
                response.status_code,
                response.text,
            )

    # Virtual storage (children of VirtualIOServer)
    def get_lpar_link(self: StorageClient, system_uuid: str, lpar_uuid: str) -> str:
        """System-scoped href for an LPAR (used when building mappings, ADR 0179)."""
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)
        return (
            f"{self._rest_base_url}/rest/api/uom/ManagedSystem/{system_uuid}"
            f"/LogicalPartition/{lpar_uuid}"
        )

    async def _reconcile_storage_mutation(
        self: StorageClient,
        operation: str,
        snapshot: Callable[[], Awaitable[Any]],
        dispatch: Callable[[], Awaitable[Any]],
        note: str | Callable[[HMCError], str] = "",
    ) -> Any:
        """Read state around a failed storage mutation without retrying it.

        *note* adds what the caller knows the failed write may have left that the
        snapshot cannot show; a callable note receives the failed write's error.
        """
        try:
            return await dispatch()
        except HMCError as exc:
            if exc.status_code is None or not 500 <= exc.status_code <= 599:
                raise
            try:
                await snapshot()
            except HMCError as readback_error:
                observation = f"readback failed: {readback_error}"
            else:
                observation = "readback completed"
            detail = note if isinstance(note, str) else note(exc)
            raise HMCError(
                f"{operation} may have a possible side effect. Do not retry until state "
                f"is verified; {observation}" + (f". {detail}" if detail else ""),
                exc.status_code,
                exc.body,
            ) from exc

    async def list_volume_groups(
        self: StorageClient, vios_uuid: str
    ) -> list[dict[str, Any]]:
        """List Volume Groups on a VIOS (free space, PVs, virtual disks).

        The VolumeGroup endpoint returns HTTP 204 when X-HMC-Schema-Version is
        present, so we deliberately omit the schema-version header here.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup"
        xml = await self._get(
            path,
            "VolumeGroup",
            include_schema_version=False,
            uuid_path_arguments={"vios_uuid": vios_uuid},
        )
        return _parse_feed(xml, path) if xml else []

    async def get_volume_group(
        self: StorageClient, vios_uuid: str, vg_uuid: str
    ) -> dict[str, Any] | None:
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}"
        xml = await self._get(
            path,
            "VolumeGroup",
            include_schema_version=False,
            uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
        )
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        return entries[0] if entries else None

    async def create_volume_group(
        self: StorageClient,
        vios_uuid: str,
        name: str,
        physical_volumes: list[str],
    ) -> dict[str, Any] | None:
        """Create a Volume Group on a VIOS from physical volumes (e.g. ['hdisk10'])."""

        xml = build_volume_group_document(name, physical_volumes)
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup"
        resp = await self._reconcile_storage_mutation(
            "create_volume_group",
            lambda: self.list_volume_groups(vios_uuid),
            lambda: self._put(
                path,
                xml,
                resource_type="VolumeGroup",
                include_schema_version=False,
                uuid_path_arguments={"vios_uuid": vios_uuid},
            ),
        )
        entries = _parse_feed(resp, path) if resp else []
        return entries[0] if entries else None

    async def create_virtual_disk(
        self: StorageClient,
        vios_uuid: str,
        vg_uuid: str,
        disk_name: str,
        capacity_mib: int,
    ) -> dict[str, Any] | None:
        """Create a Virtual Disk (logical volume) in a Volume Group.

        Read-modify-write (#936): GET the whole VolumeGroup, insert the new
        VirtualDisk after the VirtualDisks Metadata, and POST the whole element
        back with If-Match set to the GET's ETag, so existing disks and physical
        volumes are carried through unchanged. Refuses, without writing, a GET
        with no ETag and a name the group already holds.
        """

        for argument, value in (("vios_uuid", vios_uuid), ("vg_uuid", vg_uuid)):
            if not _UUID_PATTERN.fullmatch(value):
                raise ValueError(f"{argument} must be a UUID")
        element = DET.fromstring(build_virtual_disk_element(disk_name, capacity_mib))
        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)
        etag = _required_etag(etag)
        disks = _virtual_disks(vg_elem)
        if _disks_named(disks, disk_name):
            raise HMCError(
                f"Virtual disk {disk_name!r} already exists in the volume group; "
                "create does not replace it.",
                409,
            )
        metadata = _children_named(disks, "Metadata")
        disks.insert(list(disks).index(metadata[0]) + 1 if metadata else 0, element)
        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, operation="create_virtual_disk", etag=etag
        )

    async def delete_virtual_disk(
        self: StorageClient, vios_uuid: str, vg_uuid: str, disk_name: str
    ) -> dict[str, Any] | None:
        """Delete a Virtual Disk (logical volume) from a Volume Group.

        Read-modify-write (#936): GET the whole VolumeGroup, remove the one
        VirtualDisk whose DiskName matches, and POST the whole element back with
        If-Match set to the GET's ETag. Refuses, without writing, a GET with no
        ETag and zero or several matching disks.
        """

        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)
        etag = _required_etag(etag)
        collections = _children_named(vg_elem, "VirtualDisks")
        matches = _disks_named(collections[0], disk_name) if collections else []
        if len(matches) != 1:
            raise HMCError(
                f"Refusing to delete virtual disk {disk_name!r}: the volume group holds "
                f"{len(matches)} virtual disks with that name; expected exactly one.",
                409 if matches else 404,
            )
        collections[0].remove(matches[0])
        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, operation="delete_virtual_disk", etag=etag
        )

    async def map_storage_to_lpar(
        self: StorageClient,
        vios_uuid: str,
        storage_kind: StorageKind,
        storage_name: str,
        lpar_uuid: str,
        target_device: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a VirtualSCSIMapping connecting backing storage to an LPAR.

        storage_kind is "PhysicalVolume" (whole hdisk) or "VirtualDisk" (a
        logical volume created with create_virtual_disk). storage_name is the
        device or disk name. lpar_uuid is the client partition to attach to.
        The HMC creates the client/server adapter pair for the mapping, and the
        VIOS's existing mappings are preserved (ADR 0169). The mapping document is
        built system-scoped from the managed-system UUID observed on the grouped
        VIOS GET this read-modify-write performs (ADR 0179), so lpar_uuid and
        storage_kind are validated here, before that GET.
        """
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)
        if storage_kind not in STORAGE_KINDS:
            raise ValueError(
                f"storage_kind must be PhysicalVolume or VirtualDisk, got {storage_kind!r}"
            )

        def _document(system_uuid: str) -> str:
            lpar_link = self.get_lpar_link(system_uuid, lpar_uuid)
            return build_vscsi_mapping_document(
                storage_kind, storage_name, lpar_link, target_device=target_device
            )

        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping"
        resp = await _rmw_vios_mapping(
            self,
            "map_storage_to_lpar",
            path,
            {"vios_uuid": vios_uuid},
            _append_mapping("map_storage_to_lpar", _document),
            _ADAPTER_SIDE_EFFECT,
        )
        entries = _parse_feed(resp, path) if resp else []
        return entries[0] if entries else None

    # Storage Mapping Inventory and Detach
    async def list_storage_mappings(
        self: StorageClient, vios_uuid: str, lpar_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        """List VirtualSCSIMappings on a VIOS, optionally filtered by LPAR.

        Returns mappings with backing storage details (PhysicalVolume or VirtualDisk)
        and client LPAR information. Use lpar_uuid to scope mappings to a single LPAR.

        Requests the documented ``ViosSCSIMapping`` extended group.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping"
        xml = await self._get(
            path,
            "VirtualIOServer",
            uuid_path_arguments={"vios_uuid": vios_uuid},
        )
        if not xml:
            return []

        entries = _parse_feed(xml, path)
        if not entries:
            return []

        detail = entries[0]
        mappings = detail.get("Resource", {}).get("VirtualSCSIMappings", {})
        if not isinstance(mappings, dict):
            return []
        mappings = mappings.get("VirtualSCSIMapping", [])
        if not isinstance(mappings, list):
            mappings = [mappings] if mappings else []

        if lpar_uuid:
            mappings = [
                m
                for m in mappings
                if isinstance(m, dict) and _mapping_targets_lpar(m, lpar_uuid)
            ]

        return mappings if isinstance(mappings, list) else [mappings]

    async def delete_storage_mapping(
        self: StorageClient, vios_uuid: str, mapping_id: str, lpar_uuid: str
    ) -> None:
        """Detach one mapping by read-modify-write of the VIOS ``ViosSCSIMapping`` group.

        ``mapping_id`` is the ``<server adapter>/<target device>`` identity from
        :func:`storage_mapping_id`. Exactly one mapping in the fetched grouped
        document must carry it, and its client-LPAR link must name ``lpar_uuid``,
        the partition the caller authorized; otherwise nothing is posted (ADR 0168).
        The read-modify-write itself follows the same grouped GET / If-Match POST
        sequence as a create (ADR 0169): a missing ``ETag`` refuses before any POST,
        and a 412 is reported as a concurrent change.
        """
        if not mapping_id:
            raise ValueError("Storage mapping ID must not be empty")
        ET.register_namespace("", _UOM_NS)
        ET.register_namespace("atom", _ATOM_NS)
        not_found = f"Storage mapping {mapping_id!r} not found on VIOS {vios_uuid!r}"

        def _detach_one(mappings: ET.Element, _system_uuid: str | None) -> None:
            # A detach never builds a client-LPAR href, so it has no use for the
            # managed-system UUID _rmw_vios_mapping passes every mutate callback
            # (ADR 0179); a VIOS with no AssociatedManagedSystem link still detaches.
            matches = [
                (mapping, parsed)
                for mapping in mappings.findall(f"{{{_UOM_NS}}}VirtualSCSIMapping")
                if isinstance(parsed := element_to_dict(mapping), dict)
                and storage_mapping_id(parsed) == mapping_id
            ]
            if not matches:
                raise HMCError(not_found)
            if len(matches) > 1:
                raise HMCError(
                    f"VirtualSCSIMapping {mapping_id!r} is duplicated; "
                    "refusing an ambiguous detach"
                )
            target, parsed = matches[0]
            if not _mapping_targets_lpar(parsed, lpar_uuid):
                raise HMCError(
                    f"Storage mapping {mapping_id!r} does not belong to LPAR {lpar_uuid!r}; "
                    "refusing to detach a mapping that was not authorized"
                )
            mappings.remove(target)

        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping"
        await _rmw_vios_mapping(
            self, "delete_storage_mapping", path, {"vios_uuid": vios_uuid}, _detach_one
        )

    # Virtual media repository (VolumeGroup read-modify-write operations)

    async def _get_vg_raw_xml(
        self: StorageClient, vios_uuid: str, vg_uuid: str
    ) -> tuple[str | None, ET.Element]:
        """GET the full VolumeGroup XML and return (ETag, VolumeGroup element).

        Parses the Atom feed to extract the single VolumeGroup element.
        The returned ET.Element is a copy with namespace prefixes re-registered
        so subsequent serialisation round-trips cleanly.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}"
        resp = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
            headers=self._uom_headers("VolumeGroup", include_schema_version=False),
        )
        if resp.status_code not in (200, 204):
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        raw = resp.text if resp.status_code == 200 else ""
        if not raw:
            raise HMCError(f"GET {path} returned empty body", 200, "")

        # The HMC rejects auto-generated ns0/ns1 prefixes despite equivalent URIs.
        ET.register_namespace("", _UOM_NS)
        ET.register_namespace("atom", _ATOM_NS)

        try:
            root = DET.fromstring(raw)
        except (DET.ParseError, DefusedXmlException) as exc:
            raise HMCError(f"GET {path} response is not valid XML", 200, raw) from exc
        # Firmware returns either an Atom-wrapped or bare VolumeGroup document. Only
        # the root or an Atom content child is the group: each VirtualDisk carries a
        # nested VolumeGroup link element that an unanchored search would select.
        ns = {"atom": _ATOM_NS, "uom": _UOM_NS}
        if localname(root.tag) == "VolumeGroup":
            vg_elem = root
        else:
            vg_elem = root.find(".//atom:content/uom:VolumeGroup", ns)
            if vg_elem is None:
                vg_elem = root.find(".//atom:content/VolumeGroup", ns)
        if vg_elem is None:
            raise HMCError(
                f"GET {path} response contains no VolumeGroup element",
                200,
                raw[:500],
            )
        return resp.headers.get("ETag"), vg_elem

    async def _post_vg_xml(
        self: StorageClient,
        vios_uuid: str,
        vg_uuid: str,
        vg_elem: ET.Element,
        *,
        operation: str = "update_virtual_media_repository",
        etag: str | None = None,
    ) -> dict[str, Any] | None:
        """POST the serialised VolumeGroup element and return the parsed response.

        Uses Accept: */* to avoid HTTP 406 content-negotiation failures on V10R3
        firmware, while still declaring the correct Content-Type so the HMC can
        parse the body.

        The HMC accepts the VolumeGroup element directly as the POST body — not
        wrapped in an Atom feed — with Content-Type: type=VolumeGroup.
        """
        MEDIA_UOM = "application/vnd.ibm.powervm.uom+xml"
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}"
        headers = {
            "Accept": "*/*",
            "Content-Type": f"{MEDIA_UOM}; type=VolumeGroup",
        }
        if etag:
            headers["If-Match"] = etag
        body = ET.tostring(vg_elem, encoding="unicode", xml_declaration=False)

        async def dispatch() -> Any:
            resp = await self._request_with_uuid_path_arguments(
                "POST",
                path,
                uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
                content=body,
                headers=headers,
            )
            if resp.status_code == 412:
                raise HMCError(
                    f"POST {path} refused: the volume group changed since it was read "
                    "(If-Match mismatch). Nothing was written; re-run the operation.",
                    412,
                    resp.text,
                )
            if resp.status_code not in (200, 201, 202):
                raise HMCError(f"POST {path} failed", resp.status_code, resp.text)
            return resp

        resp = await self._reconcile_storage_mutation(
            operation,
            lambda: self._get(
                path,
                "VolumeGroup",
                include_schema_version=False,
                uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
            ),
            dispatch,
        )
        entries = _parse_feed(resp.text, path) if resp.text else []
        return entries[0] if entries else None

    def _find_or_create_media_repos(self, vg_elem: ET.Element) -> ET.Element:
        """Return the MediaRepositories element from *vg_elem*, creating it if absent."""
        tag = f"{{{_UOM_NS}}}MediaRepositories"
        mr = vg_elem.find(f".//{tag}")
        if mr is None:
            mr = ET.SubElement(vg_elem, tag)
            meta = ET.SubElement(mr, f"{{{_UOM_NS}}}Metadata")
            ET.SubElement(meta, f"{{{_UOM_NS}}}Atom")
        return mr

    def _find_vmlib(self, vg_elem: ET.Element) -> ET.Element | None:
        """Return the VirtualMediaRepository (VMLibrary) element, or None."""
        return vg_elem.find(f".//{{{_UOM_NS}}}VirtualMediaRepository")

    def _build_mr_element(self, size_mib: int) -> ET.Element:
        """Build a MediaRepositories element with a VMLibrary inside.

        The HMC XSD requires schemaVersion on MediaRepositories and its children.
        """
        mr = ET.Element(
            f"{{{_UOM_NS}}}MediaRepositories", attrib={"schemaVersion": "V1_0"}
        )
        meta_mr = ET.SubElement(mr, f"{{{_UOM_NS}}}Metadata")
        ET.SubElement(meta_mr, f"{{{_UOM_NS}}}Atom")
        vmlib = ET.SubElement(
            mr,
            f"{{{_UOM_NS}}}VirtualMediaRepository",
            attrib={"schemaVersion": "V1_0"},
        )
        meta_vmlib = ET.SubElement(vmlib, f"{{{_UOM_NS}}}Metadata")
        ET.SubElement(meta_vmlib, f"{{{_UOM_NS}}}Atom")
        name_el = ET.SubElement(vmlib, f"{{{_UOM_NS}}}RepositoryName")
        name_el.text = "VMLibrary"
        size_el = ET.SubElement(vmlib, f"{{{_UOM_NS}}}RepositorySize")
        size_el.text = str(_whole_gib(size_mib))
        return mr

    def _insert_mr_at_correct_position(
        self, vg_elem: ET.Element, mr_elem: ET.Element
    ) -> None:
        """Insert *mr_elem* into *vg_elem* in the correct schema position.

        The HMC VolumeGroup XSD sequence (confirmed against the live REST GET):
          Metadata, AvailableSize, FreeSpace, GroupCapacity, GroupName,
          GroupSerialID, MaximumLogicalVolumes,
          **MediaRepositories** ← here,
          PhysicalVolumes, UniqueDeviceID, VirtualDisks.

        We insert MediaRepositories immediately before PhysicalVolumes (or,
        if absent, after MaximumLogicalVolumes). If neither anchor is present
        we fall back to appending at the end.
        """
        children = list(vg_elem)
        pvs_tag = f"{{{_UOM_NS}}}PhysicalVolumes"
        max_lv_tag = f"{{{_UOM_NS}}}MaximumLogicalVolumes"

        def _local(tag: str) -> str:
            return tag.split("}")[-1]

        insert_at = next(
            (
                i
                for i, child in enumerate(children)
                if child.tag == pvs_tag or _local(child.tag) == "PhysicalVolumes"
            ),
            None,
        )
        if insert_at is None:
            insert_at = next(
                (
                    i + 1
                    for i, child in enumerate(children)
                    if child.tag == max_lv_tag
                    or _local(child.tag) == "MaximumLogicalVolumes"
                ),
                None,
            )
        if insert_at is not None:
            vg_elem.insert(insert_at, mr_elem)
        else:
            vg_elem.append(mr_elem)

    async def create_media_repository(
        self: StorageClient, vios_uuid: str, vg_uuid: str, size_mib: int
    ) -> dict[str, Any] | None:
        """Create the Virtual Media Repository (VMLibrary) on a Volume Group.

        Uses a read-modify-write pattern: GET the full VolumeGroup XML, inject a
        VirtualMediaRepository node before VirtualDisks (per the HMC XSD sequence),
        then POST the modified XML back with If-Match set to the GET's ETag (ADR 0171).
        This is the only approach that works on HMC V10R3 firmware (minimal-payload
        POSTs return HTTP 406 or 500).

        ``size_mib`` is MiB; the HMC's RepositorySize is GiB, so it is sent as
        ``size_mib / 1024`` and must be a whole number of GiB.
        """
        size_gib = _whole_gib(size_mib)
        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)

        existing = self._find_vmlib(vg_elem)
        if existing is not None:
            name = existing.findtext(
                f"{{{_UOM_NS}}}RepositoryName"
            ) or existing.findtext("RepositoryName")
            size = existing.findtext(
                f"{{{_UOM_NS}}}RepositorySize"
            ) or existing.findtext("RepositorySize")
            if _same_gib(size, size_gib):
                return {
                    "Resource": {
                        "RepositoryName": name or "VMLibrary",
                        "RepositorySize": size,
                    }
                }
            observed = f"{size} GiB" if size else "an unknown size"
            raise HMCError(
                "Virtual media repository already exists with size "
                f"{observed}; requested {size_mib} MiB ({size_gib} GiB). "
                "Create does not replace or resize an existing repository; "
                "use an explicitly destructive repository operation.",
                409,
            )

        mr = self._build_mr_element(size_mib)
        self._insert_mr_at_correct_position(vg_elem, mr)

        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, etag=_required_etag(etag)
        )

    async def create_optical_media(
        self: StorageClient,
        vios_uuid: str,
        vg_uuid: str,
        media_name: str,
        size_mib: int,
    ) -> dict[str, Any] | None:
        """Create a blank VirtualOpticalMedia (ISO container) in the repository.

        Uses a read-modify-write pattern: GET the full VolumeGroup XML, inject a
        VirtualOpticalMedia node into the OpticalMedia container inside the
        VirtualMediaRepository, then POST the modified XML back with If-Match set to
        the GET's ETag (ADR 0171).

        The HMC XSD structure inside VirtualMediaRepository is:
          Metadata, OpticalMedia (container for VirtualOpticalMedia entries),
          RepositoryName, RepositorySize.

        ``size_mib`` is MiB; the medium's Size is GiB, so it is sent as
        ``size_mib / 1024`` and must be a whole number of GiB.
        """
        size_gib = _whole_gib(size_mib)
        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)

        vmlib = self._find_vmlib(vg_elem)
        if vmlib is None:
            raise HMCError(
                "No VirtualMediaRepository found in VolumeGroup — "
                "call create_media_repository first",
                404,
                "",
            )

        opt_media_tag = f"{{{_UOM_NS}}}OpticalMedia"
        opt_media = vmlib.find(opt_media_tag)
        if opt_media is None:
            repo_name_tag = f"{{{_UOM_NS}}}RepositoryName"
            repo_name_idx = next(
                (
                    i
                    for i, c in enumerate(list(vmlib))
                    if c.tag == repo_name_tag
                    or c.tag.split("}")[-1] == "RepositoryName"
                ),
                None,
            )
            opt_media = ET.Element(
                opt_media_tag,
                attrib={"kb": "CUD", "kxe": "false", "schemaVersion": "V1_0"},
            )
            ET.SubElement(
                ET.SubElement(opt_media, f"{{{_UOM_NS}}}Metadata"),
                f"{{{_UOM_NS}}}Atom",
            )
            if repo_name_idx is not None:
                vmlib.insert(repo_name_idx, opt_media)
            else:
                vmlib.append(opt_media)

        vom_tag = f"{{{_UOM_NS}}}VirtualOpticalMedia"
        vom = ET.SubElement(opt_media, vom_tag, attrib={"schemaVersion": "V1_0"})
        meta = ET.SubElement(vom, f"{{{_UOM_NS}}}Metadata")
        ET.SubElement(meta, f"{{{_UOM_NS}}}Atom")
        # The captured V10R3 child order and attributes (#1385), less the server-set
        # MediaUDID that the capture places between MediaName and MountType.
        # The HMC XSD names the size field Size, not MediaSize, and measures it in GiB.
        for tag, kb, text in (
            ("MediaName", "CUR", media_name),
            ("MountType", "CUD", "rw"),
            ("Size", "CUR", str(size_gib)),
        ):
            child = ET.SubElement(
                vom, f"{{{_UOM_NS}}}{tag}", attrib={"kb": kb, "kxe": "false"}
            )
            child.text = text

        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, etag=_required_etag(etag)
        )

    async def delete_media_repository(
        self: StorageClient, vios_uuid: str, vg_uuid: str
    ) -> dict[str, Any] | None:
        """Delete the Virtual Media Repository (VMLibrary) from a Volume Group.

        Uses a read-modify-write pattern: GET the full VolumeGroup XML, remove the
        MediaRepositories block, then POST the modified XML back with If-Match set to
        the GET's ETag (ADR 0171). Refuses, without writing, when the MediaRepositories
        block this GET observed still holds any VirtualOpticalMedia — the caller's own
        emptiness check is a separate, earlier GET, so a medium created between the two
        would otherwise be deleted with it (#1012).
        """
        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)

        mr_tag = f"{{{_UOM_NS}}}MediaRepositories"
        mr = vg_elem.find(f".//{mr_tag}")
        if mr is None:
            return None

        vom_tag = f"{{{_UOM_NS}}}VirtualOpticalMedia"
        name_tag = f"{{{_UOM_NS}}}MediaName"
        media = mr.findall(f".//{vom_tag}")
        if media:
            names = ", ".join(m.findtext(name_tag) or "unknown" for m in media)
            raise HMCError(
                f"Cannot delete media repository: it contains {len(media)} "
                f"image(s): {names!r}. Delete all images first.",
                409,
            )

        vg_elem.remove(mr)

        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, etag=_required_etag(etag)
        )

    async def delete_optical_media(
        self: StorageClient, vios_uuid: str, vg_uuid: str, media_name: str
    ) -> dict[str, Any] | None:
        """Delete a VirtualOpticalMedia (ISO image) from the media repository.

        Uses a read-modify-write pattern: GET the full VolumeGroup XML, remove the
        named VirtualOpticalMedia node from the OpticalMedia container, then POST back
        with If-Match set to the GET's ETag (ADR 0171).
        """
        etag, vg_elem = await self._get_vg_raw_xml(vios_uuid, vg_uuid)

        vmlib = self._find_vmlib(vg_elem)
        if vmlib is None:
            return None  # Nothing to remove.

        opt_media_tag = f"{{{_UOM_NS}}}OpticalMedia"
        vom_tag = f"{{{_UOM_NS}}}VirtualOpticalMedia"
        name_tag = f"{{{_UOM_NS}}}MediaName"

        opt_media = vmlib.find(opt_media_tag)
        if opt_media is None:
            return None  # Nothing to remove.

        to_remove: ET.Element | None = None
        for vom in list(opt_media.findall(vom_tag)):
            n = vom.find(name_tag)
            if n is not None and n.text == media_name:
                to_remove = vom
                break
        if to_remove is None:
            return None
        opt_media.remove(to_remove)

        return await self._post_vg_xml(
            vios_uuid, vg_uuid, vg_elem, etag=_required_etag(etag)
        )

    async def get_media_repository(
        self: StorageClient, vios_uuid: str, vg_uuid: str
    ) -> dict[str, Any] | None:
        """Get the VolumeGroup Atom entry containing a Virtual Media Repository.

        The repository is nested at ``Resource.MediaRepositories.VirtualMediaRepository``.
        Its ``RepositorySize`` is in GiB; optional media entries are nested under
        ``OpticalMedia.VirtualOpticalMedia`` within the repository. Returns None when
        no repository-bearing VolumeGroup entry is available.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}"
        try:
            xml = await self._get(
                path,
                "VolumeGroup",
                include_schema_version=False,
                uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
            )
        except HMCError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        if not entries:
            return None
        entry = entries[0]
        resource = entry.get("Resource")
        if not isinstance(resource, dict):
            return None
        repositories = resource.get("MediaRepositories")
        if not isinstance(repositories, dict):
            return None
        if "VirtualMediaRepository" not in repositories:
            return None
        return entry

    async def list_optical_media(
        self: StorageClient, vios_uuid: str, vg_uuid: str
    ) -> list[dict[str, Any]]:
        """List Virtual Optical Media in the Virtual Media Repository.

        Returns a list of optical media entries (ISO containers) as the HMC sends
        them: V10R3 gives MediaName, MediaUDID, MountType and Size (GiB), with no
        MediaType. Returns empty list if the
        Volume Group does not exist or has no media repository.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup/{vg_uuid}"
        try:
            xml = await self._get(
                path,
                "VolumeGroup",
                include_schema_version=False,
                uuid_path_arguments={"vios_uuid": vios_uuid, "vg_uuid": vg_uuid},
            )
        except HMCError as exc:
            if exc.status_code == 404:
                return []
            raise
        if not xml:
            return []

        entries = _parse_feed(xml, path)
        if not entries:
            return []

        return _extract_optical_media(entries)

    # Virtual Optical Mapping (VirtualSCSIMapping for VirtualOpticalMedia)
    async def list_optical_mappings(
        self: StorageClient, vios_uuid: str, lpar_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        """List VirtualSCSIMappings for optical media on a VIOS, optionally filtered by LPAR.

        Returns only mappings that reference VirtualOpticalMedia backing, with media
        details and client LPAR information. Use lpar_uuid to scope mappings to a single LPAR.
        """
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping"
        xml = await self._get(
            path,
            "VirtualIOServer",
            uuid_path_arguments={"vios_uuid": vios_uuid},
        )
        if not xml:
            return []

        entries = _parse_feed(xml, path)
        if not entries:
            return []

        detail = entries[0]
        mappings = detail.get("Resource", {}).get("VirtualSCSIMappings", {})
        if not isinstance(mappings, dict):
            return []
        mappings = mappings.get("VirtualSCSIMapping", [])
        if not isinstance(mappings, list):
            mappings = [mappings] if mappings else []

        return _filter_optical_mappings(
            [mapping for mapping in mappings if isinstance(mapping, dict)], lpar_uuid
        )

    async def create_optical_mapping(
        self: StorageClient,
        vios_uuid: str,
        media_name: str,
        lpar_uuid: str,
        target_device: str | None = None,
    ) -> dict[str, Any] | None:
        """Create an optical-media mapping and return its response entry, if any.

        The mapping document is built system-scoped from the managed-system UUID
        observed on the grouped VIOS GET this read-modify-write performs (ADR 0179),
        so lpar_uuid is validated here, before that GET.
        """
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)

        def _document(system_uuid: str) -> str:
            lpar_link = self.get_lpar_link(system_uuid, lpar_uuid)
            return build_virtual_optical_mapping_document(
                media_name,
                lpar_link,
                target_device=target_device,
            )

        append = _append_mapping("create_optical_mapping", _document)

        def _mutate(mappings: ET.Element, system_uuid: str | None) -> None:
            if target_device:
                _refuse_mapped_optical_target(mappings, target_device)
            append(mappings, system_uuid)

        def _note(failure: HMCError) -> str:
            if _VTD_NAME_IN_USE not in (failure.body or ""):
                return _ADAPTER_SIDE_EFFECT
            return f"{_optical_target_in_use(target_device)} {_ADAPTER_SIDE_EFFECT}"

        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        path = f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping"
        response = await _rmw_vios_mapping(
            self,
            "create_optical_mapping",
            path,
            {"vios_uuid": vios_uuid},
            _mutate,
            _note,
        )
        entries = _parse_feed(response, path) if response else []
        return entries[0].get("Resource", entries[0]) if entries else None
