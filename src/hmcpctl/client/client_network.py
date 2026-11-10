"""HMCClient network mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for network.
"""

from __future__ import annotations

from typing import Any

from ..documents.storage import build_virtual_network_document
from .client_contracts import NetworkClient, _reject_non_uuid_path_argument
from .client_parse import _parse_feed


class NetworkMixin:
    # Virtual Network management (children of ManagedSystem)
    async def list_virtual_switches(
        self: NetworkClient, system_uuid: str
    ) -> list[dict[str, Any]]:
        """List VirtualSwitches on a managed system (names, IDs, mode).

        Raises:
            ValueError: If system_uuid is invalid.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualSwitch"
        xml = await self._get(path, "VirtualSwitch")
        return _parse_feed(xml, path) if xml else []

    async def list_virtual_networks(
        self: NetworkClient, system_uuid: str
    ) -> list[dict[str, Any]]:
        """List Virtual Networks (VLANs) on a managed system.

        Raises:
            ValueError: If system_uuid is invalid.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualNetwork"
        xml = await self._get(path, "VirtualNetwork")
        return _parse_feed(xml, path) if xml else []

    async def list_network_bridges(
        self: NetworkClient, system_uuid: str
    ) -> list[dict[str, Any]]:
        """List NetworkBridges (Shared Ethernet Adapters) on a managed system.

        Raises:
            ValueError: If system_uuid is invalid.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/NetworkBridge"
        xml = await self._get(path, "NetworkBridge")
        return _parse_feed(xml, path) if xml else []

    async def create_virtual_network(
        self: NetworkClient,
        system_uuid: str,
        name: str,
        vlan_id: int,
        virtual_switch_id: int,
        switch_uuid: str | None = None,
        tagged: bool = False,
    ) -> dict[str, Any] | None:
        """Create a Virtual Network (VLAN) on a managed system.

        virtual_switch_id is the numeric SwitchID (from list_virtual_switches);
        switch_uuid optionally provides the AssociatedSwitch link.

        Raises:
            ValueError: If system_uuid is invalid.
        """

        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        switch_link = None
        if switch_uuid:
            switch_link = (
                f"{self._rest_base_url}/rest/api/uom/ManagedSystem/"
                f"{system_uuid}/VirtualSwitch/{switch_uuid}"
            )
        xml = build_virtual_network_document(
            name, vlan_id, virtual_switch_id, switch_link, tagged
        )
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualNetwork"
        resp = await self._put(
            path, xml, resource_type="VirtualNetwork", include_schema_version=False
        )
        entries = _parse_feed(resp, path) if resp else []
        return entries[0] if entries else None

    async def delete_virtual_network(
        self: NetworkClient, system_uuid: str, network_uuid: str
    ) -> None:
        """Delete a Virtual Network from a managed system.

        Raises:
            ValueError: If system_uuid or network_uuid is invalid.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        _reject_non_uuid_path_argument("network_uuid", network_uuid)
        await self._delete(
            f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualNetwork/{network_uuid}"
        )
