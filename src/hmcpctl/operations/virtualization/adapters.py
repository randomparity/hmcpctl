"""Presentation-neutral virtual-adapter operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.adapters import validate_network_adapter_creation_fields
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profile_sync import ChangeLocation, read_change_location

from ...client.client_contracts import AdapterType, validate_adapter_type
from ...resource_identity import is_uuid, resolve_lpar_uuid
from ...xmlutil import leaf_text
from .network import require_vlan_id


class NetworkCreationVerification(TypedDict):
    status: Literal["verified", "mismatched", "unavailable"]
    checked_fields: list[str]
    mismatched_fields: list[str]
    unavailable_fields: list[str]


_NETWORK_CREATION_ELEMENTS = {
    "addl_vlan_ids": "TaggedVLANIDs",
    "allowed_os_mac_addrs": "AllowedOperatingSystemMACAddresses",
    "qos_priority": "QualityOfServicePriority",
    "vsi_manager_id": "VirtualStationInterfaceManagerID",
    "vsi_type_id": "VirtualStationInterfaceTypeID",
    "vsi_type_version": "VirtualStationInterfaceTypeVersion",
}


@dataclass(frozen=True)
class AdapterResult:
    lpar_uuid: str
    resource: dict[str, Any] | None
    change_location: ChangeLocation
    creation_verification: NetworkCreationVerification | None = None


async def list_adapters(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    adapter_type: AdapterType,
    *,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List one kind of virtual adapter on an LPAR.

    Raises:
        ValueError: If ``adapter_type`` is unsupported.
    """
    validate_adapter_type(adapter_type)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    return await hmc.list_adapters(lpar_uuid, adapter_type)


async def add_network_adapter(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    port_vlan_id: int,
    *,
    system_name_or_uuid: str | None = None,
    slot_number: int | None = None,
    virtual_switch_id: int | None = None,
    tagged: bool = False,
    mac_address: str | None = None,
    ownership_override: bool = False,
    addl_vlan_ids: list[int] | None = None,
    allowed_os_mac_addrs: list[str] | Literal["all", "none"] | None = None,
    qos_priority: int | Literal["none"] | None = None,
    vsi_manager_id: int | None = None,
    vsi_type_id: int | None = None,
    vsi_type_version: int | None = None,
) -> AdapterResult:
    """Authorize one creation and independently observe selected new stored fields.

    Raises:
        ValueError: Invalid selectors, PVID or selected policy values.
        PermissionError: Partition ownership authorization rejects the mutation.
        HMCError: Baseline identity is unavailable or the native submission fails.
    """
    require_vlan_id("port_vlan_id", port_vlan_id)
    fields: dict[str, Any] = {
        "addl_vlan_ids": addl_vlan_ids,
        "allowed_os_mac_addrs": allowed_os_mac_addrs,
        "qos_priority": qos_priority,
        "vsi_manager_id": vsi_manager_id,
        "vsi_type_id": vsi_type_id,
        "vsi_type_version": vsi_type_version,
    }
    selected = {
        name: value.copy() if isinstance(value, list) else value
        for name, value in fields.items()
        if value is not None
    }
    if selected:
        validate_network_adapter_creation_fields(tagged=tagged, **selected)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    baseline = (
        _network_inventory_identities(
            await hmc._network_adapter_creation_feed(lpar_uuid)
        )
        if selected
        else set()
    )
    resource = await hmc.add_network_adapter(
        lpar_uuid,
        port_vlan_id,
        slot_number=slot_number,
        virtual_switch_id=virtual_switch_id,
        tagged=tagged,
        mac_address=mac_address,
        **selected,
    )
    verification = (
        await _verify_network_creation(hmc, lpar_uuid, baseline, resource, selected)
        if selected
        else None
    )
    return AdapterResult(lpar_uuid, resource, location, verification)


def _network_identity(entry: object) -> str | None:
    if (
        not isinstance(entry, dict)
        or entry.get("ResourceType") != "ClientNetworkAdapter"
    ):
        return None
    identity = entry.get("UUID")
    return (
        identity.casefold() if isinstance(identity, str) and is_uuid(identity) else None
    )


def _network_inventory_identities(entries: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    if not isinstance(entries, list):
        raise HMCError("Client network adapter inventory is unavailable")
    for entry in entries:
        identity = _network_identity(entry)
        if identity is None or identity in identities:
            raise HMCError(
                "Client network adapter inventory contains ambiguous identities"
            )
        identities.add(identity)
    return identities


def _network_leaf(value: object) -> str | None:
    if isinstance(value, dict) and (
        set(value) != {"@attrs", "text"} or not isinstance(value.get("@attrs"), dict)
    ):
        return None
    value = leaf_text(value)
    return value.strip() if isinstance(value, str) else None


def _network_integer(value: object, low: int, high: int) -> int | None:
    text = _network_leaf(value)
    if text is None or not re.fullmatch(r"[+-]?[0-9]+", text):
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if low <= number <= high else None


def _network_observed_value(name: str, resource: dict[str, Any]) -> object:
    raw = resource.get(_NETWORK_CREATION_ELEMENTS[name])
    if name == "qos_priority":
        enabled = _network_leaf(resource.get("QualityOfServicePriorityEnabled"))
        if enabled in ("false", "0"):
            return "none"
        return _network_integer(raw, 0, 7) if enabled in ("true", "1") else None
    if name.startswith("vsi_"):
        return _network_integer(raw, -(2**31), 2**31 - 1)
    text = _network_leaf(raw)
    if text is None:
        return None
    if name == "addl_vlan_ids":
        values = [_network_integer(value, 1, 4094) for value in text.split()]
        return values if len(values) <= 20 and None not in values else None
    policy: Any = text.lower() if text in ("ALL", "NONE") else text.split()
    try:
        validate_network_adapter_creation_fields(
            tagged=True, allowed_os_mac_addrs=policy
        )
    except ValueError:
        return None
    return [value.upper() for value in policy] if isinstance(policy, list) else policy


async def _verify_network_creation(
    hmc: HMCClient,
    lpar_uuid: str,
    baseline: set[str],
    response: dict[str, Any] | None,
    selected: dict[str, Any],
) -> NetworkCreationVerification:
    result: NetworkCreationVerification = {
        "status": "unavailable",
        "checked_fields": list(selected),
        "mismatched_fields": [],
        "unavailable_fields": list(selected),
    }
    identity = _network_identity(response)
    if identity is None or identity in baseline:
        return result
    try:
        after = await hmc._network_adapter_creation_feed(lpar_uuid)
        identities = _network_inventory_identities(after)
    except HMCError:
        return result
    if identity not in identities:
        return result
    observed = next(entry for entry in after if _network_identity(entry) == identity)
    resource = observed.get("Resource")
    if not isinstance(resource, dict):
        return result
    result["unavailable_fields"] = []
    for name, expected in selected.items():
        value = _network_observed_value(name, resource)
        if name == "allowed_os_mac_addrs" and isinstance(expected, list):
            expected = [address.upper() for address in expected]
        if value is None:
            result["unavailable_fields"].append(name)
        elif value != expected:
            result["mismatched_fields"].append(name)
    result["status"] = (
        "mismatched"
        if result["mismatched_fields"]
        else "unavailable"
        if result["unavailable_fields"]
        else "verified"
    )
    return result


async def add_vscsi_adapter(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    vios_partition_id: int,
    vios_slot: int,
    *,
    system_name_or_uuid: str | None = None,
    slot_number: int | None = None,
    ownership_override: bool = False,
) -> AdapterResult:
    """Authorize the LPAR and add a virtual SCSI adapter.

    Raises:
        ResourceNotFoundError: If a supplied LPAR or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        ValueError: If selector scope cannot be verified.
        HMCError: If the HMC rejects the adapter request or it cannot be completed.
    """
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.add_vscsi_adapter(
        lpar_uuid, vios_partition_id, vios_slot, slot_number
    )
    return AdapterResult(lpar_uuid, resource, location)


async def add_vfc_adapter(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    vios_partition_id: int,
    vios_slot: int,
    *,
    system_name_or_uuid: str | None = None,
    slot_number: int | None = None,
    ownership_override: bool = False,
) -> AdapterResult:
    """Authorize the LPAR and add a virtual Fibre Channel adapter.

    Raises:
        ResourceNotFoundError: If a supplied LPAR or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        ValueError: If selector scope cannot be verified.
        HMCError: If the HMC rejects the adapter request or it cannot be completed.
    """
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.add_vfc_adapter(
        lpar_uuid, vios_partition_id, vios_slot, slot_number
    )
    return AdapterResult(lpar_uuid, resource, location)


async def delete_adapter(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    adapter_type: AdapterType,
    adapter_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> ChangeLocation:
    """Authorize the LPAR, delete one virtual adapter, and say where that lived.

    Raises:
        ValueError: If ``adapter_type`` is unsupported.
        ResourceNotFoundError: If a supplied LPAR or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        HMCError: If the HMC rejects the deletion or it cannot be completed.
    """
    validate_adapter_type(adapter_type)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    await hmc.delete_adapter(lpar_uuid, adapter_type, adapter_uuid)
    return location
