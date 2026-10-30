"""Presentation-neutral virtual-adapter operations."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.adapters import (
    validate_network_adapter_creation_fields,
    validate_network_adapter_update_fields,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.ownership import (
    authorize_lpar_mutation,
    resolve_and_authorize_lpar_mutation,
)
from hmcpctl.operations.lpar.profile_sync import (
    ChangeLocation,
    change_location_of,
    read_change_location,
)

from ...client.client_contracts import AdapterType, validate_adapter_type
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_uuid
from ...ssh.ethernet import _set_ethernet_adapter
from ...ssh.transport import HMCCLIError
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


@dataclass(frozen=True)
class NetworkUpdateVerification:
    status: Literal["verified", "mismatched", "unavailable"]
    expected: dict[str, object]
    observed: dict[str, object]
    mismatched_fields: tuple[str, ...]
    unavailable_fields: tuple[str, ...]


@dataclass(frozen=True)
class NetworkUpdatePreservation:
    status: Literal["unchanged", "drifted", "unavailable"]
    scope: str
    changed_adapter_uuids: tuple[str, ...]


@dataclass(frozen=True)
class NetworkUpdateResult:
    system_uuid: str
    lpar_uuid: str
    adapter_uuid: str
    slot_number: int
    mutation_dispatched: bool
    submission: Literal["not_dispatched", "completed", "rejected", "unknown"]
    reason: Literal["already_satisfied"] | None
    refusal_code: str | None
    verification: NetworkUpdateVerification
    preservation: NetworkUpdatePreservation
    change_location: ChangeLocation
    errors: tuple[str, ...]


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


def _validate_network_update_target(
    lpar_name_or_uuid: str, adapter_uuid: str, system_name_or_uuid: str
) -> None:
    if (
        not isinstance(adapter_uuid, str)
        or not is_uuid(adapter_uuid)
        or any(
            not isinstance(value, str) or not value.strip()
            for value in (system_name_or_uuid, lpar_name_or_uuid)
        )
    ):
        raise ValueError(
            "network update requires system, partition and valid adapter UUID selectors"
        )


async def update_network_adapter(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    adapter_uuid: str,
    *,
    system_name_or_uuid: str,
    exclusive_writer_window: bool,
    tagged: bool | None = None,
    addl_vlan_ids: list[int] | None = None,
    vlan_action: Literal["replace", "add", "remove"] = "replace",
    qos_priority: int | Literal["none"] | None = None,
    vsi_manager_id: int | None = None,
    vsi_type_id: int | None = None,
    vsi_type_version: int | None = None,
    remove_vsi_profile: bool = False,
    ownership_override: bool = False,
) -> NetworkUpdateResult:
    """Attempt one coordinated native update, independently observing its stored result."""
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
    if exclusive_writer_window is not True or type(ownership_override) is not bool:
        raise ValueError(
            "network update requires exclusive_writer_window=true and boolean override"
        )
    _validate_network_update_target(
        lpar_name_or_uuid, adapter_uuid, system_name_or_uuid
    )
    if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
        raise ValueError("network update requires a finite positive ssh_timeout")
    selected = {
        name: value.copy() if isinstance(value, list) else value
        for name, value in fields.items()
        if value is not None and name not in ("vlan_action", "remove_vsi_profile")
    }
    if addl_vlan_ids is not None:
        selected["vlan_action"] = vlan_action
    if remove_vsi_profile:
        selected["remove_vsi_profile"] = True
    adapter_uuid = adapter_uuid.casefold()
    system_uuid = (await resolve_system_uuid(hmc, system_name_or_uuid)).casefold()
    system_name = await _network_update_system(hmc, system_uuid)
    lpar_uuid = (
        await resolve_lpar_uuid(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_uuid,
        )
    ).casefold()
    parent = await _network_update_parent(hmc, system_uuid, lpar_uuid)
    await authorize_lpar_mutation(
        hmc, system_name, parent[0], ownership_override=ownership_override
    )
    before = deepcopy(await hmc._network_adapter_creation_feed(lpar_uuid))
    inventory = _network_update_inventory(before)
    if adapter_uuid not in inventory:
        raise ValueError(
            "selected adapter UUID is absent from the contained client inventory"
        )
    expected, attributes = _network_update_expectation(
        inventory[adapter_uuid], selected
    )
    fresh_parent = await _network_update_parent(hmc, system_uuid, lpar_uuid)
    fresh = deepcopy(await hmc._network_adapter_creation_feed(lpar_uuid))
    if (
        await _network_update_system(hmc, system_uuid) != system_name
        or fresh_parent[:2] != parent[:2]
        or _network_update_inventory_projection(before)
        != _network_update_inventory_projection(fresh)
    ):
        raise ValueError(
            "network update baseline changed; inspect and establish a fresh writer window"
        )
    slot = _network_integer(inventory[adapter_uuid].get("VirtualSlotNumber"), 0, 65535)
    assert slot is not None  # The complete baseline inventory proved every slot.
    if not attributes:
        verification, preservation = _network_update_observation(
            before, fresh, adapter_uuid, expected, selected
        )
        return NetworkUpdateResult(
            system_uuid,
            lpar_uuid,
            adapter_uuid,
            slot,
            False,
            "not_dispatched",
            "already_satisfied",
            None,
            verification,
            preservation,
            parent[2],
            (),
        )
    submission, code, errors = await _network_update_submission(
        hmc, system_name, parent[1], slot, attributes
    )
    verification, preservation, after_errors = await _network_update_after(
        hmc,
        system_uuid,
        lpar_uuid,
        parent[:2],
        before,
        adapter_uuid,
        expected,
        selected,
    )
    return NetworkUpdateResult(
        system_uuid,
        lpar_uuid,
        adapter_uuid,
        slot,
        True,
        submission,
        None,
        code,
        verification,
        preservation,
        parent[2],
        errors + after_errors,
    )


async def _network_update_system(hmc: HMCClient, system_uuid: str) -> str:
    summaries = await hmc.list_system_summaries()
    if (
        not isinstance(system_uuid, str)
        or not is_uuid(system_uuid)
        or not isinstance(summaries, list)
    ):
        raise ValueError("network update requires a complete native system-name index")
    identities: set[str] = set()
    names: list[str] = []
    selected = None
    for entry in summaries:
        identity = entry.get("UUID") if isinstance(entry, dict) else None
        name = entry.get("SystemName") if isinstance(entry, dict) else None
        if (
            not isinstance(identity, str)
            or not is_uuid(identity)
            or not isinstance(name, str)
            or not name.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ValueError(
                "native system-name index contains unavailable identities or names"
            )
        if identity.casefold() in identities:
            raise ValueError("native system-name index repeats a system UUID")
        identities.add(identity.casefold())
        names.append(name)
        if identity.casefold() == system_uuid:
            selected = name
    if selected is None or names.count(selected) != 1:
        raise ValueError(
            "ambiguous native system name; use a uniquely named system before updating"
        )
    return selected


async def _network_update_parent(
    hmc: HMCClient,
    system_uuid: str,
    lpar_uuid: str,
) -> tuple[str, int, ChangeLocation]:
    await resolve_lpar_uuid(hmc, lpar_uuid, system_name_or_uuid=system_uuid)
    entry = await hmc.get_logical_partition(lpar_uuid)
    if not isinstance(entry, dict) or (
        str(entry.get("UUID", "")).casefold() != lpar_uuid
        or entry.get("ResourceType") != "LogicalPartition"
        or not isinstance(entry.get("Resource"), dict)
    ):
        raise ValueError("network update partition identity is unavailable")
    resource = entry["Resource"]
    name = _network_leaf(resource.get("PartitionName"))
    identifier = _network_leaf(resource.get("PartitionID"))
    if not name or identifier is None or not re.fullmatch(r"[0-9]+", identifier):
        raise ValueError(
            "network update requires partition name and positive native ID"
        )
    try:
        native_id = int(identifier)
    except ValueError:
        raise ValueError("network update native partition ID is unavailable") from None
    if native_id <= 0 or _network_leaf(resource.get("PartitionState")) != "running":
        raise ValueError(
            "network update requires a running partition and positive native ID"
        )
    location = change_location_of(entry)
    return (
        name,
        native_id,
        ChangeLocation(location.current_profile_sync, "unknown", location.profile_name),
    )


def _network_update_inventory(
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    _network_inventory_identities(entries)
    inventory: dict[str, dict[str, Any]] = {}
    slots: set[int] = set()
    for entry in entries:
        resource = entry.get("Resource")
        if not isinstance(resource, dict):
            raise HMCError("network adapter resource is unavailable")
        slot = _network_integer(resource.get("VirtualSlotNumber"), 0, 65535)
        if slot is None or slot in slots:
            raise ValueError(
                "network adapter inventory requires unique observed virtual slots"
            )
        slots.add(slot)
        inventory[entry["UUID"].casefold()] = resource
    return inventory


def _network_update_inventory_projection(
    entries: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        identity: {key: value for key, value in resource.items() if key != "Metadata"}
        for identity, resource in _network_update_inventory(entries).items()
    }


def _network_update_vlans(resource: dict[str, Any]) -> list[int] | None:
    text = _network_leaf(resource.get("TaggedVLANIDs"))
    if text is None:
        return None
    values = [_network_integer(value, 1, 4094) for value in text.split()]
    if len(values) > 4094 or None in values or len(set(values)) != len(values):
        return None
    return sorted(value for value in values if value is not None)


def _network_update_value(name: str, resource: dict[str, Any]) -> object:
    if name == "tagged":
        text = _network_leaf(resource.get("TaggedVLANSupported"))
        return (
            True if text in ("true", "1") else False if text in ("false", "0") else None
        )
    if name == "addl_vlan_ids":
        return _network_update_vlans(resource)
    if name == "remove_vsi_profile":
        return {
            field: _network_observed_value(field, resource)
            for field in ("vsi_manager_id", "vsi_type_id", "vsi_type_version")
        }
    return _network_observed_value(name, resource)


def _network_update_expectation(
    resource: dict[str, Any],
    selected: dict[str, Any],
) -> tuple[dict[str, object], list[tuple[str, object]]]:
    expected: dict[str, object] = {}
    attributes: list[tuple[str, object]] = []
    for name in (
        "tagged",
        "qos_priority",
        "vsi_manager_id",
        "vsi_type_id",
        "vsi_type_version",
    ):
        if name in selected:
            expected[name] = selected[name]
            attributes.append(
                ("ieee_virtual_eth", int(selected[name]))
                if name == "tagged"
                else (name, selected[name])
            )
    if selected.get("remove_vsi_profile") is True:
        expected["remove_vsi_profile"] = True
        attributes.append(("vsi_manager_id", "none"))
    baseline = _network_update_vlans(resource)
    if "addl_vlan_ids" in selected:
        values, action = selected["addl_vlan_ids"], selected["vlan_action"]
        if (action != "replace" or not values) and baseline is None:
            raise ValueError("VLAN action requires a complete observed baseline list")
        result = (
            set(values)
            if action == "replace"
            else (
                set(baseline or []) | set(values)
                if action == "add"
                else set(baseline or []) - set(values)
            )
        )
        if len(result) > 20:
            raise ValueError("the resulting VLAN set must contain at most twenty IDs")
        tagging = selected.get("tagged", _network_update_value("tagged", resource))
        if result and tagging is not True:
            raise ValueError(
                "nonempty resulting VLANs require positively enabled tagging"
            )
        expected["addl_vlan_ids"] = sorted(result)
        if values:
            operator = {"replace": "", "add": "+", "remove": "-"}[action]
            attributes.append(("addl_vlan_ids" + operator, ",".join(map(str, values))))
        elif baseline:
            attributes.append(("addl_vlan_ids-", ",".join(map(str, baseline))))
        baseline = sorted(result)
    if selected.get("tagged") is False and (baseline is None or baseline):
        raise ValueError(
            "disabling tagging requires a positively empty resulting VLAN set"
        )
    return expected, attributes


def _network_update_observation(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]] | None,
    adapter_uuid: str,
    expected: dict[str, object],
    selected: dict[str, Any],
) -> tuple[NetworkUpdateVerification, NetworkUpdatePreservation]:
    scope = "parsed_adapter_resources_except_metadata_and_selected_fields"
    unavailable = NetworkUpdateVerification(
        "unavailable", expected, {}, (), tuple(expected)
    )
    if after is None:
        return unavailable, NetworkUpdatePreservation("unavailable", scope, ())
    try:
        _network_inventory_identities(after)
    except HMCError:
        return unavailable, NetworkUpdatePreservation("unavailable", scope, ())
    old = {entry["UUID"].casefold(): entry["Resource"] for entry in before}
    new = {entry["UUID"].casefold(): entry.get("Resource") for entry in after}
    changed = set(old) ^ set(new)
    incomplete = False
    closure = {
        _NETWORK_CREATION_ELEMENTS[name]
        for name in expected
        if name in _NETWORK_CREATION_ELEMENTS
    }
    if "tagged" in expected:
        closure.add("TaggedVLANSupported")
    if "qos_priority" in expected:
        closure.add("QualityOfServicePriorityEnabled")
    if "remove_vsi_profile" in expected:
        closure.update(
            _NETWORK_CREATION_ELEMENTS[name]
            for name in ("vsi_manager_id", "vsi_type_id", "vsi_type_version")
        )
    for identity in set(old) & set(new):
        observed_resource = new[identity]
        if not isinstance(observed_resource, dict):
            incomplete = True
            continue
        excluded = {"Metadata"} | (closure if identity == adapter_uuid else set())
        left = {
            key: value for key, value in old[identity].items() if key not in excluded
        }
        right = {
            key: value
            for key, value in observed_resource.items()
            if key not in excluded
        }
        if left != right:
            changed.add(identity)
    preservation = NetworkUpdatePreservation(
        "drifted" if changed else "unavailable" if incomplete else "unchanged",
        scope,
        tuple(sorted(changed)),
    )
    target = new.get(adapter_uuid)
    if not isinstance(target, dict) or _network_integer(
        target.get("VirtualSlotNumber"), 0, 65535
    ) != _network_integer(old[adapter_uuid].get("VirtualSlotNumber"), 0, 65535):
        return unavailable, preservation
    observed = {name: _network_update_value(name, target) for name in expected}
    missing = tuple(
        name
        for name in expected
        if observed[name] is None or name == "remove_vsi_profile"
    )
    mismatched = tuple(
        name
        for name in expected
        if name not in missing and observed[name] != expected[name]
    )
    verification = NetworkUpdateVerification(
        "mismatched" if mismatched else "unavailable" if missing else "verified",
        expected,
        observed,
        mismatched,
        missing,
    )
    return verification, preservation


async def _network_update_submission(
    hmc: HMCClient,
    system_name: str,
    partition_id: int,
    slot: int,
    attributes: list[tuple[str, object]],
) -> tuple[Literal["completed", "rejected", "unknown"], str | None, tuple[str, ...]]:
    try:
        await _set_ethernet_adapter(
            hmc.config, system_name, partition_id, slot, attributes
        )
    except HMCCLIError as error:
        if error.remote_nonzero_completed:
            return (
                "rejected",
                error.remote_refusal_code,
                ("native command returned nonzero; inspect readback before retrying",),
            )
        return (
            "unknown",
            None,
            ("native completion is unknown; inspect readback before retrying",),
        )
    except HMCError:
        return (
            "unknown",
            None,
            ("native completion is unknown; inspect readback before retrying",),
        )
    return "completed", None, ()


async def _network_update_after(
    hmc: HMCClient,
    system: str,
    lpar: str,
    parent: tuple[str, int],
    before: list[dict[str, Any]],
    identity: str,
    expected: dict[str, object],
    selected: dict[str, Any],
) -> tuple[NetworkUpdateVerification, NetworkUpdatePreservation, tuple[str, ...]]:
    errors: tuple[str, ...] = ()
    try:
        after_parent = await _network_update_parent(hmc, system, lpar)
        parent_matches = after_parent[:2] == parent
    except (HMCError, ValueError):
        parent_matches = False
    try:
        after = await hmc._network_adapter_creation_feed(lpar)
    except HMCError:
        after = None
        errors += ("adapter readback is unavailable",)
    verification, preservation = _network_update_observation(
        before, after, identity, expected, selected
    )
    if not parent_matches:
        errors += (
            "partition identity changed or is unavailable in independent readback",
        )
        verification = NetworkUpdateVerification(
            "unavailable", expected, verification.observed, (), tuple(expected)
        )
        if preservation.status != "drifted":
            preservation = NetworkUpdatePreservation(
                "unavailable", preservation.scope, ()
            )
    return verification, preservation, errors


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
