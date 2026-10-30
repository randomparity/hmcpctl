"""MCP tools for virtual network and I/O adapters."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from pydantic import StrictBool, StrictInt, StrictStr

from ..._app import run_limited_collection, with_client
from ...client.client_contracts import AdapterType
from ...client.core import HMCClient
from ...documents.adapters import (
    validate_network_adapter_creation_fields,
    validate_network_adapter_update_fields,
)
from ...operations.lpar.profile_sync import resource_with_change_location
from ...operations.virtualization.adapters import (
    _validate_network_update_target,
    add_network_adapter,
    add_vfc_adapter,
    add_vscsi_adapter,
    delete_adapter,
    list_adapters,
    update_network_adapter,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="adapter.list", target_kind="lpar")
def hmc_list_adapters(
    lpar_name_or_uuid: str,
    adapter_type: AdapterType = "ClientNetworkAdapter",
    profile: str | None = None,
    limit: int | None = None,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """List one LPAR's virtual adapters of the selected adapter type.

    Args:
        lpar_name_or_uuid: Partition name or UUID; discover partitions with
            ``hmc_list_lpars``.
        adapter_type: Adapter resource type to list.
        profile: TOML profile name, or the environment-default HMC when omitted.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """
    return run_limited_collection(
        lambda hmc: list_adapters(
            hmc,
            lpar_name_or_uuid,
            adapter_type,
            system_name_or_uuid=system_name_or_uuid,
        ),
        limit,
        profile=profile,
    )


@tool(effect="mutate", operation="adapter.add_network", target_kind="lpar")
def hmc_add_network_adapter(
    lpar_name_or_uuid: str,
    port_vlan_id: int,
    slot_number: int | None = None,
    virtual_switch_id: int | None = None,
    tagged: bool = False,
    mac_address: str | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
    *,
    addl_vlan_ids: list[StrictInt] | None = None,
    allowed_os_mac_addrs: list[StrictStr] | Literal["all", "none"] | None = None,
    qos_priority: StrictInt | Literal["none"] | None = None,
    vsi_manager_id: StrictInt | None = None,
    vsi_type_id: StrictInt | None = None,
    vsi_type_version: StrictInt | None = None,
) -> dict[str, Any]:
    """Add a virtual Ethernet adapter to an LPAR; active LPARs require RMC.

    Returns the adapter plus ``change_location``: the partition's
    CurrentProfileSync and whether the adapter lives only in the current
    configuration (a profile activation then drops it) or in its profile too.

    Selected new fields add ``creation_verification`` beside the original identity:
    verified, mismatched or unavailable stored-field evidence. This does not prove
    runtime traffic, QoS, VSI registration or saved-profile equality. Do not retry
    blindly after uncertainty; no automatic compensation is performed.

    Args:
        lpar_name_or_uuid: Partition name or UUID; discover partitions with
            ``hmc_list_lpars``.
        port_vlan_id: Port VLAN ID assigned to untagged traffic.
        slot_number: Client virtual slot, or ``None`` for HMC auto-assignment.
        virtual_switch_id: Numeric switch ID from ``hmc_list_virtual_switches``,
            or ``None`` to use the HMC default.
        tagged: Whether the adapter accepts IEEE 802.1Q tagged VLAN traffic.
        mac_address: Optional 12-hex-digit MAC address, with no separators.
        addl_vlan_ids: Additional VLAN IDs 1–4094, at most twenty; nonempty requires
            tagged=True. Omitted leaves HMC defaults; an empty list is explicit.
        allowed_os_mac_addrs: all, none, or one to four 12-hex MAC addresses.
        qos_priority: Priority 0–7, or none to disable. Omitted preserves defaults.
        vsi_manager_id: VSI manager signed 32-bit value; HMC validates semantic limits.
        vsi_type_id: VSI type signed 32-bit value; HMC validates semantic limits.
        vsi_type_version: VSI version signed 32-bit value; HMC validates semantic limits.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    fields: dict[str, Any] = {
        "addl_vlan_ids": addl_vlan_ids,
        "allowed_os_mac_addrs": allowed_os_mac_addrs,
        "qos_priority": qos_priority,
        "vsi_manager_id": vsi_manager_id,
        "vsi_type_id": vsi_type_id,
        "vsi_type_version": vsi_type_version,
    }
    selected = {name: value for name, value in fields.items() if value is not None}
    if selected:
        validate_network_adapter_creation_fields(tagged=tagged, **selected)
        selected = {
            name: value.copy() if isinstance(value, list) else value
            for name, value in selected.items()
        }

    async def operation(hmc: HMCClient):
        result = await add_network_adapter(
            hmc,
            lpar_name_or_uuid,
            port_vlan_id,
            slot_number=slot_number,
            virtual_switch_id=virtual_switch_id,
            tagged=tagged,
            mac_address=mac_address,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
            **selected,
        )
        rendered = resource_with_change_location(
            result.resource, result.change_location
        )
        if result.creation_verification is not None:
            rendered["creation_verification"] = result.creation_verification
        return rendered

    return with_client(operation, profile=profile)


@tool(effect="mutate", operation="adapter.update_network", target_kind="lpar")
def hmc_update_network_adapter(
    lpar_name_or_uuid: str,
    adapter_uuid: str,
    *,
    system_name_or_uuid: str,
    exclusive_writer_window: StrictBool,
    tagged: StrictBool | None = None,
    addl_vlan_ids: list[StrictInt] | None = None,
    vlan_action: Literal["replace", "add", "remove"] = "replace",
    qos_priority: StrictInt | Literal["none"] | None = None,
    vsi_manager_id: StrictInt | None = None,
    vsi_type_id: StrictInt | None = None,
    vsi_type_version: StrictInt | None = None,
    remove_vsi_profile: StrictBool = False,
    ownership_override: StrictBool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Update one contained running client Ethernet adapter with one native attempt.

    Requires SSH and coordinated writers; the acknowledgement provides no HMC lock.
    Omitted fields remain unchanged. False, zero and empty VLAN replacement are explicit.
    Reports native submission, independent selected-field verification and parsed adapter
    preservation separately. Missing evidence is unavailable; VSI removal has no qualified
    positive readback oracle. No runtime traffic or saved-profile equality is established.
    Refusal or uncertainty may have effects: inspect observations, never retry blindly.

    Args:
        lpar_name_or_uuid: Running LPAR name or UUID.
        adapter_uuid: Existing client network adapter UUID contained by this LPAR.
        system_name_or_uuid: Required containing managed system name or UUID.
        exclusive_writer_window: Must be true; caller coordinates all writers.
        tagged: Set IEEE tagging; omitted preserves it.
        addl_vlan_ids: Unique VLAN IDs 1–4094; resulting set has at most twenty.
        vlan_action: Replace, add or remove; relative actions require complete baseline.
        qos_priority: Priority 0–7, or none to disable QoS.
        vsi_manager_id: Signed 32-bit VSI manager; HMC validates remaining limits.
        vsi_type_id: Signed 32-bit VSI type; HMC validates remaining limits.
        vsi_type_version: Signed 32-bit VSI version; HMC validates remaining limits.
        remove_vsi_profile: Remove whole VSI profile; cannot combine with numeric VSI.
        ownership_override: Standard audited ownership exception; containment still required.
        profile: TOML connection profile, or environment-default HMC when omitted.
    """
    if exclusive_writer_window is not True:
        raise ValueError("exclusive_writer_window=True is required")
    _validate_network_update_target(
        lpar_name_or_uuid, adapter_uuid, system_name_or_uuid
    )
    fields: dict[str, Any] = {
        "tagged": tagged,
        "addl_vlan_ids": addl_vlan_ids,
        "vlan_action": vlan_action,
        "qos_priority": qos_priority,
        "vsi_manager_id": vsi_manager_id,
        "vsi_type_id": vsi_type_id,
        "vsi_type_version": vsi_type_version,
        "remove_vsi_profile": remove_vsi_profile,
    }
    validate_network_adapter_update_fields(**fields)
    if addl_vlan_ids is not None:
        fields["addl_vlan_ids"] = addl_vlan_ids.copy()

    async def operation(hmc: HMCClient):
        return asdict(
            await update_network_adapter(
                hmc,
                lpar_name_or_uuid,
                adapter_uuid,
                system_name_or_uuid=system_name_or_uuid,
                exclusive_writer_window=True,
                ownership_override=ownership_override,
                **fields,
            )
        )

    return with_client(operation, profile=profile)


# Not exhaustive: `vios_partition_id` is a slot number within one managed
# system, reused on every system in a fleet, so a `vios` allowlist entry can
# never name the VIOS this call actually pairs to. It has no UUID form to fall
# back on, so ADR 0039 grants this tool only under `targets = "all-targets"`.
@tool(
    effect="mutate",
    operation="adapter.add_vscsi",
    target_kind="lpar",
    exhaustive_targets=False,
)
def hmc_add_vscsi_adapter(
    lpar_name_or_uuid: str,
    vios_partition_id: int,
    vios_slot: int,
    slot_number: int | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Add a virtual SCSI client adapter paired to a VIOS server slot.

    Returns the adapter plus ``change_location``, as ``hmc_add_network_adapter``.

    Args:
        lpar_name_or_uuid: Client partition name or UUID.
        vios_partition_id: Numeric VIOS partition ID from ``hmc_list_vios``.
        vios_slot: Server-side virtual slot configured on that VIOS.
        slot_number: Client virtual slot, or ``None`` for HMC auto-assignment.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def operation(hmc: HMCClient):
        result = await add_vscsi_adapter(
            hmc,
            lpar_name_or_uuid,
            vios_partition_id,
            vios_slot,
            slot_number=slot_number,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
        return resource_with_change_location(result.resource, result.change_location)

    return with_client(operation, profile=profile)


# Not exhaustive: `vios_partition_id` is a slot number within one managed
# system, reused on every system in a fleet, so a `vios` allowlist entry can
# never name the VIOS this call actually pairs to. It has no UUID form to fall
# back on, so ADR 0039 grants this tool only under `targets = "all-targets"`.
@tool(
    effect="mutate",
    operation="adapter.add_vfc",
    target_kind="lpar",
    exhaustive_targets=False,
)
def hmc_add_vfc_adapter(
    lpar_name_or_uuid: str,
    vios_partition_id: int,
    vios_slot: int,
    slot_number: int | None = None,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Add an NPIV virtual Fibre Channel client adapter to an LPAR.

    Returns the adapter plus ``change_location``, as ``hmc_add_network_adapter``.

    Args:
        lpar_name_or_uuid: Client partition name or UUID.
        vios_partition_id: Numeric VIOS partition ID from ``hmc_list_vios``.
        vios_slot: Server-side NPIV virtual slot configured on that VIOS.
        slot_number: Client virtual slot, or ``None`` for HMC auto-assignment.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def operation(hmc: HMCClient):
        result = await add_vfc_adapter(
            hmc,
            lpar_name_or_uuid,
            vios_partition_id,
            vios_slot,
            slot_number=slot_number,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
        return resource_with_change_location(result.resource, result.change_location)

    return with_client(operation, profile=profile)


@tool(effect="destructive", operation="adapter.delete", target_kind="lpar")
def hmc_delete_adapter(
    lpar_name_or_uuid: str,
    adapter_type: AdapterType,
    adapter_uuid: str,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> str:
    """Remove an adapter by UUID, detaching its network or storage path.

    Confirm the adapter is no longer required before deletion. The message
    ends with where the deletion lives (the partition's CurrentProfileSync).

    Args:
        lpar_name_or_uuid: Partition name or UUID containing the adapter.
        adapter_type: Adapter resource type that owns ``adapter_uuid``.
        adapter_uuid: Adapter UUID returned by ``hmc_list_adapters``.
        ownership_override: Bypass LPAR ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide.
    """

    async def operation(hmc: HMCClient):
        location = await delete_adapter(
            hmc,
            lpar_name_or_uuid,
            adapter_type,
            adapter_uuid,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )
        return (
            f"Deleted {adapter_type} {adapter_uuid} from {lpar_name_or_uuid}. "
            f"{location.summary()}"
        )

    return with_client(operation, profile=profile)
