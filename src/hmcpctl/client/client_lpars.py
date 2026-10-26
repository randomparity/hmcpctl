"""HMCClient lpars mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for lpars.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # nosec B405 - serializes a defusedxml-parsed tree
from collections.abc import Callable, Mapping
from typing import Any

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from ..documents.lpar import sharing_observation
from ..errors import HMCError, LparUpdateError
from ..xmlutil import localname
from .client_contracts import LparsClient, _reject_non_uuid_path_argument
from .client_parse import _parse_feed
from .client_resolution import (
    ambiguity_candidate_ids,
    ambiguous_parent_details,
    require_absence_provable,
    require_operating_system,
    summary_for,
)

_UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_MEDIA_UOM = "application/vnd.ibm.powervm.uom+xml"


def _logical_partition_element(root: ET.Element, path: str, raw: str) -> ET.Element:
    """The partition element: the document root or an Atom entry's content child."""
    if localname(root.tag) == "LogicalPartition":
        return root
    found = root.find(f"{{{_ATOM_NS}}}content/{{{_UOM_NS}}}LogicalPartition")
    if found is None:
        raise HMCError(
            f"GET {path} contains no LogicalPartition element", 200, raw[:500]
        )
    return found


_WEIGHT_PATH = (
    "PartitionProcessorConfiguration/SharedProcessorConfiguration/UncappedWeight"
)
_SHARED_PATH = "PartitionProcessorConfiguration/SharedProcessorConfiguration"


def _exact_partition_path(lpar: ET.Element, path: str) -> ET.Element | None:
    node = lpar
    for part in path.split("/"):
        matches = [child for child in node if localname(child.tag) == part]
        if not matches:
            return None
        if len(matches) != 1 or matches[0].tag != f"{{{_UOM_NS}}}{part}":
            raise HMCError(f"Ambiguous or wrong-namespace partition path {path}")
        node = matches[0]
    return node


def _edit_logical_partition(
    lpar: ET.Element, changes: Mapping[str, str | None], subject: str
) -> None:
    """Replace exact scalar paths, with only the configured-weight insertion exception."""
    if lpar.tag != f"{{{_UOM_NS}}}LogicalPartition":
        raise HMCError("Expected a UOM LogicalPartition document")
    if not changes:
        raise ValueError(f"nothing to write for {subject}; nothing was sent")
    missing_weight = False
    for field, text in changes.items():
        if any(part.startswith(("Current", "Runtime")) for part in field.split("/")):
            raise HMCError(f"Refusing to write observed partition path {field}")
        element = _exact_partition_path(lpar, field)
        if element is None:
            if field == _WEIGHT_PATH:
                missing_weight = True
                continue
            raise HMCError(f"GET partition has no {field}; refusing to write {subject}")
        if len(element):
            raise HMCError(f"Expected a scalar partition path {field}")
        element.text = text
    if _WEIGHT_PATH not in changes:
        return
    shared = _exact_partition_path(lpar, _SHARED_PATH)
    if shared is None:
        raise HMCError(
            f"GET partition has no {_SHARED_PATH}; refusing to write {subject}"
        )
    anchor = _exact_partition_path(lpar, f"{_SHARED_PATH}/SharedProcessorPoolID")
    if anchor is not None and len(anchor):
        raise HMCError("Expected a scalar SharedProcessorPoolID anchor")
    if not missing_weight:
        return
    configured = sharing_observation(lpar)["configured"]
    if (
        anchor is None
        or configured is None
        or configured["dedicated"]
        or configured["mode"] != "uncapped"
    ):
        raise HMCError(
            "Missing uncapped weight requires shared uncapped context and a "
            "SharedProcessorPoolID anchor"
        )
    weight = ET.Element(f"{{{_UOM_NS}}}UncappedWeight", {"kxe": "false", "kb": "CUD"})
    weight.text = changes[_WEIGHT_PATH]
    shared.insert(list(shared).index(anchor) + 1, weight)


class LparsMixin:
    async def list_logical_partitions(
        self: LparsClient, system_uuid: str
    ) -> list[dict[str, Any]]:
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/LogicalPartition"
        xml = await self._get(path, "LogicalPartition")
        entries = _parse_feed(xml, path) if xml else []
        if not entries:
            await require_operating_system(
                self.list_system_summaries, system_uuid, "LPARs"
            )
        return entries

    async def get_logical_partition(
        self: LparsClient, uuid: str
    ) -> dict[str, Any] | None:
        """Get one LogicalPartition using the ``get_uom`` empty-success/error contract.

        None means an empty successful response or no parsed entries; HTTP 404
        and other statuses outside 200/204 raise HMCError.
        """
        return await self.get_uom("LogicalPartition", uuid)

    async def find_partition_by_name(
        self: LparsClient, name: str, system_uuid: str | None = None
    ) -> dict[str, Any] | None:
        """Find an LPAR by exact PartitionName, optionally within one system.

        Returns:
            The matching entry, or None for confirmed absence in the searched scope.
            An unscoped miss is confirmed only when every system is readable.

        Raises:
            ValueError: The name is ambiguous or its candidates cannot be identified.
            HMCError: A transport/API request fails, or unreadable system inventory
                prevents confirming an unscoped miss.
        """
        if system_uuid:
            entries = await self.list_logical_partitions(system_uuid)
            results = [
                entry
                for entry in entries
                if (entry.get("Resource") or {}).get("PartitionName") == name
            ]
            if len(results) > 1:
                ambiguity_candidate_ids(results, "LPAR", name)
                system_name = summary_for(
                    await self.list_system_summaries(), system_uuid
                ).get("SystemName")
                if not isinstance(system_name, str) or not system_name:
                    raise ValueError(
                        f"Cannot resolve ambiguous LPAR name {name!r}: cannot "
                        f"identify managed system {system_uuid}"
                    )
                details = ", ".join(
                    f"{entry.get('UUID')} on {system_name!r} ({system_uuid})"
                    for entry in sorted(results, key=lambda item: str(item.get("UUID")))
                )
                raise ValueError(f"Ambiguous LPAR name {name!r}: {details}")
            return results[0] if results else None

        results = await self.search_uom("LogicalPartition", "PartitionName", name)
        if not results:
            await require_absence_provable(self.list_system_summaries, "LPAR", name)
            return None
        if len(results) == 1:
            return results[0]

        details = await ambiguous_parent_details(
            results,
            await self.list_system_summaries(),
            "LPAR",
            name,
            self.list_logical_partitions,
        )
        raise ValueError(f"Ambiguous LPAR name {name!r}: {details}")

    async def create_logical_partition(
        self: LparsClient, system_uuid: str, lpar_xml: str
    ) -> dict[str, Any] | None:
        """Create an LPAR on a managed system.

        PUTs a LogicalPartition document (see documents.lpar.build_lpar_document)
        to /rest/api/uom/ManagedSystem/{system_uuid}/LogicalPartition and
        returns the created partition entry.

        Omits the X-HMC-Schema-Version header; the write headers follow ADR 0178.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/LogicalPartition"
        xml = await self._put(
            path,
            lpar_xml,
            resource_type="LogicalPartition",
            include_schema_version=False,
        )
        entries = _parse_feed(xml, path) if xml else []
        return entries[0] if entries else None

    async def set_pending_boot_string(
        self: LparsClient, lpar_uuid: str, boot_string: str
    ) -> dict[str, Any] | None:
        """Set ``BootListInformation/PendingBootString`` by read-modify-write.

        Replaces only that element's text through :meth:`update_logical_partition`,
        which refuses before any POST when the GET carries no ETag or the partition
        has no ``BootListInformation/PendingBootString``.
        """
        return await self.update_logical_partition(
            lpar_uuid,
            lambda _lpar: {
                "BootListInformation/PendingBootString": boot_string or None
            },
            "the boot order",
        )

    async def read_logical_partition_document(
        self: LparsClient, lpar_uuid: str, *, require_etag: bool = False
    ) -> tuple[ET.Element, str | None]:
        """Read a detached Advanced document, optionally requiring write protection."""
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)
        path = f"/rest/api/uom/LogicalPartition/{lpar_uuid}?group=Advanced"
        got = await self._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments={"lpar_uuid": lpar_uuid},
            headers=self._uom_headers("LogicalPartition", include_schema_version=False),
        )
        if got.status_code != 200:
            raise HMCError(f"GET {path} failed", got.status_code, got.text)
        etag = got.headers.get("ETag")
        if require_etag and not etag:
            raise HMCError(
                f"GET {path} returned no ETag; refusing a whole-partition write "
                "that could overwrite a concurrent change",
                200,
                got.text[:500],
            )
        # The HMC rejects auto-generated ns0/ns1 prefixes despite equivalent URIs.
        ET.register_namespace("", _UOM_NS)
        ET.register_namespace("atom", _ATOM_NS)
        try:
            root = DET.fromstring(got.text)
        except (DET.ParseError, DefusedXmlException) as exc:
            raise HMCError(
                f"GET {path} response is not valid XML", 200, got.text[:500]
            ) from exc
        lpar = _logical_partition_element(root, path, got.text)
        return lpar, etag

    async def validate_logical_partition_update(
        self: LparsClient,
        lpar_uuid: str,
        updates: Callable[[ET.Element], Mapping[str, str | None]],
        subject: str,
    ) -> None:
        """Validate the real edit against a detached read without submitting it."""
        lpar, _ = await self.read_logical_partition_document(lpar_uuid)
        _edit_logical_partition(lpar, updates(lpar), subject)

    async def update_logical_partition(
        self: LparsClient,
        lpar_uuid: str,
        updates: Callable[[ET.Element], Mapping[str, str | None]],
        subject: str,
    ) -> dict[str, Any] | None:
        """Edit a fresh whole partition and submit once under its original ETag.

        Only configured missing uncapped weight may be inserted. Failures before
        edit validation refuse normally; later failures retain submission certainty.
        The returned POST entry is not independent proof of configuration completion.
        """
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)
        path = f"/rest/api/uom/LogicalPartition/{lpar_uuid}?group=Advanced"
        lpar, etag = await self.read_logical_partition_document(
            lpar_uuid, require_etag=True
        )
        _edit_logical_partition(lpar, updates(lpar), subject)
        body = ET.tostring(lpar, encoding="unicode")
        try:
            response = await self._request_with_uuid_path_arguments(
                "POST",
                path,
                uuid_path_arguments={"lpar_uuid": lpar_uuid},
                content=body,
                headers={
                    "Accept": "*/*",
                    "Content-Type": f"{_MEDIA_UOM}; type=LogicalPartition",
                    "If-Match": etag,
                },
            )
        except HMCError as exc:
            submission = "unknown"
            message = str(exc)
            if exc.status_code == 412:
                submission = "rejected"
                message = (
                    "The partition changed since it was read (If-Match mismatch). "
                    f"Nothing was written; re-run the operation. {exc}"
                )
            elif exc.status_code is not None and 200 <= exc.status_code < 300:
                submission = "acknowledged"
            # The cause already rendered/bounded its status and body. Preserve that
            # diagnostic once, along with the original structured error metadata.
            error = LparUpdateError(message, submission)
            error.status_code, error.body = exc.status_code, exc.body
            raise error from exc
        if response.status_code == 412:
            raise LparUpdateError(
                f"POST {path} refused: the partition changed since it was read "
                "(If-Match mismatch). Nothing was written; re-run the operation.",
                "rejected",
                412,
                response.text,
            )
        if not 200 <= response.status_code < 300:
            raise LparUpdateError(
                f"POST {path} failed", "unknown", response.status_code, response.text
            )
        try:
            entries = _parse_feed(response.text, path) if response.text else []
        except HMCError as exc:
            raise LparUpdateError(
                str(exc), "acknowledged", response.status_code, response.text
            ) from exc
        return entries[0] if entries else None

    async def delete_logical_partition(self: LparsClient, lpar_uuid: str) -> None:
        """Delete an LPAR. It must be powered off first."""
        _reject_non_uuid_path_argument("lpar_uuid", lpar_uuid)
        await self._delete(f"/rest/api/uom/LogicalPartition/{lpar_uuid}")
