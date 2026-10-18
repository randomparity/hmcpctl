"""Presentation-neutral virtual-adapter operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.operations.lpar.profile_sync import ChangeLocation, read_change_location

from ...client.client_contracts import AdapterType, validate_adapter_type
from ...resource_identity import resolve_lpar_uuid
from .network import require_vlan_id


@dataclass(frozen=True)
class AdapterResult:
    lpar_uuid: str
    resource: dict[str, Any] | None
    change_location: ChangeLocation


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
) -> AdapterResult:
    """Authorize the LPAR and add a virtual Ethernet adapter.

    Raises:
        ResourceNotFoundError: If a supplied LPAR or managed-system selector cannot
            be resolved.
        PermissionError: If the LPAR ownership authorization rejects the mutation.
        ValueError: If selector scope cannot be verified, or ``port_vlan_id`` is
            outside 1-4094.
        HMCError: If the HMC rejects the adapter request or it cannot be completed.
    """
    require_vlan_id("port_vlan_id", port_vlan_id)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    location = await read_change_location(hmc, lpar_uuid)
    resource = await hmc.add_network_adapter(
        lpar_uuid,
        port_vlan_id,
        slot_number=slot_number,
        virtual_switch_id=virtual_switch_id,
        tagged=tagged,
        mac_address=mac_address,
    )
    return AdapterResult(lpar_uuid, resource, location)


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
