"""HMCClient adapters mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for adapters.
"""

from __future__ import annotations

from typing import Any, Literal

from defusedxml import ElementTree as DET

from ..documents.adapters import (
    build_client_network_adapter_document,
    build_vfc_adapter_document,
    build_vscsi_adapter_document,
)
from ..errors import HMCError
from ..xmlutil import ATOM_NS, localname
from .client_contracts import (
    AdaptersClient,
    AdapterType,
    StorageAdapterType,
    validate_adapter_type,
    validate_storage_adapter_type,
)
from .client_parse import _parse_feed


class AdaptersMixin:
    async def list_adapters(
        self: AdaptersClient,
        lpar_uuid: str,
        adapter_type: AdapterType,
        *,
        allow_empty_network: bool = False,
    ) -> list[dict[str, Any]]:
        """List one adapter kind, requiring complete network-adapter inventory.

        ClientNetworkAdapter returns [] for a complete, empty Atom feed.
        allow_empty_network also accepts HTTP 204 when explicitly proving absence;
        it does not change other adapter kinds. The default remains strict.
        Unaccepted HTTP 204, blank responses, pagination and ambiguous entries raise
        HMCError. Other adapter kinds return [] for an empty successful response.

        Raises:
            ValueError: If adapter_type is unsupported or lpar_uuid is invalid.
            HMCError: If the request fails, the response is malformed, or network
                inventory is unavailable or incomplete.
        """
        validate_adapter_type(adapter_type)
        if adapter_type == "ClientNetworkAdapter":
            return await _complete_network_adapter_feed(
                self, lpar_uuid, allow_empty=allow_empty_network
            )
        return await self.list_child("LogicalPartition", lpar_uuid, adapter_type)

    async def delete_adapter(
        self: AdaptersClient,
        lpar_uuid: str,
        adapter_type: AdapterType,
        adapter_uuid: str,
    ) -> None:
        """Delete one adapter from a logical partition.

        Raises:
            ValueError: If adapter_type is unsupported, or lpar_uuid or
                adapter_uuid is invalid.
            HMCError: If the HMC request fails.
        """
        validate_adapter_type(adapter_type)
        await self.delete_child(
            "LogicalPartition", lpar_uuid, adapter_type, adapter_uuid
        )

    async def get_storage_adapter(
        self: AdaptersClient,
        lpar_uuid: str,
        adapter_type: StorageAdapterType,
        adapter_uuid: str,
    ) -> dict[str, Any] | None:
        """GET one client storage adapter instance; None on an empty response.

        Raises:
            ValueError: If adapter_type is not a client storage type, or a UUID is
                invalid.
            HMCError: If the HMC answers outside 200/204 (404 included).
        """
        validate_storage_adapter_type(adapter_type)
        if adapter_type == "VirtualSCSIClientAdapter":
            template = "/rest/api/uom/LogicalPartition/{}/VirtualSCSIClientAdapter/{}"
        else:
            template = (
                "/rest/api/uom/LogicalPartition/{}/VirtualFibreChannelClientAdapter/{}"
            )
        path = template.format(lpar_uuid, adapter_uuid)
        xml = await self._get(
            path,
            adapter_type,
            uuid_path_arguments={"lpar_uuid": lpar_uuid, "adapter_uuid": adapter_uuid},
        )
        entries = _parse_feed(xml, path) if xml else []
        return entries[0] if entries else None

    async def add_vscsi_adapter(
        self: AdaptersClient,
        lpar_uuid: str,
        vios_partition_id: int,
        vios_slot: int,
        slot_number: int | None = None,
    ) -> dict[str, Any] | None:
        """Add a Virtual SCSI client adapter, paired to a VIOS server adapter."""

        xml = build_vscsi_adapter_document(vios_partition_id, vios_slot, slot_number)
        return await self.create_child(
            "LogicalPartition", lpar_uuid, "VirtualSCSIClientAdapter", xml
        )

    async def add_vfc_adapter(
        self: AdaptersClient,
        lpar_uuid: str,
        vios_partition_id: int,
        vios_slot: int,
        slot_number: int | None = None,
    ) -> dict[str, Any] | None:
        """Add a Virtual Fibre Channel (NPIV) client adapter, paired to a VIOS."""

        xml = build_vfc_adapter_document(vios_partition_id, vios_slot, slot_number)
        return await self.create_child(
            "LogicalPartition", lpar_uuid, "VirtualFibreChannelClientAdapter", xml
        )

    async def add_network_adapter(
        self: AdaptersClient,
        lpar_uuid: str,
        port_vlan_id: int,
        *,
        slot_number: int | None = None,
        virtual_switch_id: int | None = None,
        tagged: bool = False,
        mac_address: str | None = None,
        addl_vlan_ids: list[int] | None = None,
        allowed_os_mac_addrs: list[str] | Literal["all", "none"] | None = None,
        qos_priority: int | Literal["none"] | None = None,
        vsi_manager_id: int | None = None,
        vsi_type_id: int | None = None,
        vsi_type_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Add a Virtual Ethernet client network adapter to an LPAR."""

        xml = build_client_network_adapter_document(
            port_vlan_id,
            slot_number,
            virtual_switch_id,
            tagged,
            mac_address,
            addl_vlan_ids=addl_vlan_ids,
            allowed_os_mac_addrs=allowed_os_mac_addrs,
            qos_priority=qos_priority,
            vsi_manager_id=vsi_manager_id,
            vsi_type_id=vsi_type_id,
            vsi_type_version=vsi_type_version,
        )
        return await self.create_child(
            "LogicalPartition", lpar_uuid, "ClientNetworkAdapter", xml
        )


async def _complete_network_adapter_feed(
    self: AdaptersClient, lpar_uuid: str, *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    """Read complete inventory for network listing and creation identity proof.

    An empty collection (HTTP 204) is refused as unavailable unless *allow_empty*:
    a caller proving an adapter absent reads it as no adapters (ADR 0325).
    """
    path = f"/rest/api/uom/LogicalPartition/{lpar_uuid}/ClientNetworkAdapter"
    response = await self._request_with_uuid_path_arguments(
        "GET",
        path,
        uuid_path_arguments={"lpar_uuid": lpar_uuid},
        headers=self._uom_headers("ClientNetworkAdapter"),
    )
    if response.status_code == 204:
        if allow_empty:
            return []
        raise HMCError("Client network adapter inventory is unavailable")
    if response.status_code != 200:
        raise HMCError(f"GET {path} failed", response.status_code, response.text)
    if "link" in response.headers or "content-range" in response.headers:
        raise HMCError("Client network adapter inventory must be a complete Atom feed")
    xml = response.text
    if not xml.strip():
        raise HMCError("Client network adapter inventory is unavailable")
    entries = _parse_feed(xml, path)
    root = DET.fromstring(xml.encode("utf-8"))
    if root.tag != f"{{{ATOM_NS}}}feed" or any(
        node.get("rel", "").casefold() == "next"
        for node in root.findall(f"{{{ATOM_NS}}}link")
    ):
        raise HMCError("Client network adapter inventory must be a complete Atom feed")
    for entry in root:
        if localname(entry.tag) != "entry":
            continue
        ids = [node for node in entry if localname(node.tag) == "id"]
        bodies = [node for node in entry if localname(node.tag) == "content"]
        if (
            entry.tag != f"{{{ATOM_NS}}}entry"
            or len(ids) != 1
            or ids[0].tag != f"{{{ATOM_NS}}}id"
            or len(ids[0]) != 0
            or len(bodies) != 1
            or bodies[0].tag != f"{{{ATOM_NS}}}content"
            or len(bodies[0]) != 1
            or localname(bodies[0][0].tag) != "ClientNetworkAdapter"
        ):
            raise HMCError(
                "Client network adapter inventory requires unambiguous Atom entries"
            )
    return entries
