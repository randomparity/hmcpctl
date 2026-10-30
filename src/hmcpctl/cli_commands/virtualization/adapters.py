"""CLI commands for virtual adapters (network / storage) on LPARs."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal

import typer
from rich.text import Text

from ...client.client_contracts import ADAPTER_TYPES, AdapterType
from ...documents.adapters import (
    validate_network_adapter_creation_fields,
    validate_network_adapter_update_fields,
)
from ...operations.virtualization.adapters import (
    AdapterResult,
    _validate_network_update_target,
    add_network_adapter,
    add_vfc_adapter,
    add_vscsi_adapter,
    delete_adapter,
    list_adapters,
    update_network_adapter,
)
from ..output import console, output, print_json, terminal_field
from ..runtime import with_client

_ADAPTER_TYPES = " | ".join(sorted(ADAPTER_TYPES))


def adapters_list(
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    adapter_type: AdapterType = typer.Option(
        "ClientNetworkAdapter", "--type", "-t", help=_ADAPTER_TYPES
    ),
    as_json: bool = typer.Option(False, "--json"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """List an LPAR's virtual adapters of a given type."""

    adapters = with_client(
        lambda hmc: list_adapters(hmc, lpar, adapter_type, system_name_or_uuid=system)
    )

    output(adapters, as_json, None, f"No {adapter_type} adapters on {lpar}")


def adapters_add_network(
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    vlan: int = typer.Option(..., "--vlan", help="Port VLAN ID (PVID)"),
    slot: int | None = typer.Option(
        None, "--slot", help="Virtual slot (auto if omitted)"
    ),
    virtual_switch_id: int | None = typer.Option(
        None, "--virtual-switch-id", help="VirtualSwitch numeric SwitchID"
    ),
    tagged: bool = typer.Option(
        False, "--tagged", help="VLAN-tagged (trunking) adapter"
    ),
    mac: str | None = typer.Option(
        None, "--mac", help="Pin the MAC address: 12 hex digits, no separators"
    ),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    *,
    addl_vlan_ids: str | None = typer.Option(
        None,
        "--addl-vlan-ids",
        help="JSON array of additional VLAN IDs (1–4094, max20); requires --tagged",
    ),
    allowed_os_mac_addrs: str | None = typer.Option(
        None,
        "--allowed-os-mac-addrs",
        help="all, none, or JSON array of one to four 12-hex MACs",
    ),
    qos_priority: str | None = typer.Option(
        None,
        "--qos-priority",
        help="0–7, or none to disable QoS; omitted leaves defaults",
    ),
    vsi_manager_id: int | None = typer.Option(
        None, "--vsi-manager-id", help="VSI manager ID; HMC validates semantic limits"
    ),
    vsi_type_id: int | None = typer.Option(
        None, "--vsi-type-id", help="VSI type ID; HMC validates semantic limits"
    ),
    vsi_type_version: int | None = typer.Option(
        None,
        "--vsi-type-version",
        help="VSI type version; HMC validates semantic limits",
    ),
) -> None:
    """Add a Virtual Ethernet (network) adapter to an LPAR."""
    selected = _network_cli_creation_fields(
        tagged,
        {
            "addl_vlan_ids": addl_vlan_ids,
            "allowed_os_mac_addrs": allowed_os_mac_addrs,
            "qos_priority": qos_priority,
            "vsi_manager_id": vsi_manager_id,
            "vsi_type_id": vsi_type_id,
            "vsi_type_version": vsi_type_version,
        },
    )

    if not yes and not typer.confirm(
        f"Add network adapter (VLAN {terminal_field(vlan)}) to '{terminal_field(lpar)}'?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: add_network_adapter(
            hmc,
            lpar,
            vlan,
            slot_number=slot,
            virtual_switch_id=virtual_switch_id,
            tagged=tagged,
            mac_address=mac,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
            **selected,
        )
    )
    _adapter_mutation(result, lpar, "network")


def adapters_update_network(
    lpar: str = typer.Argument(..., help="Running LPAR name or UUID"),
    adapter_uuid: str = typer.Option(..., "--uuid", help="Contained adapter UUID"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Managed system name or UUID"
    ),
    *,
    exclusive_writer_window: bool = typer.Option(
        False,
        "--exclusive-writer-window",
        help="Acknowledge coordinated exclusive writers; no HMC lock",
    ),
    tagged: bool | None = typer.Option(None, "--tagged/--untagged"),
    addl_vlan_ids: str | None = typer.Option(
        None,
        "--addl-vlan-ids",
        help="JSON VLAN array; empty replacement removes the known baseline",
    ),
    vlan_action: Literal["replace", "add", "remove"] = typer.Option(
        "replace", "--vlan-action"
    ),
    qos_priority: str | None = typer.Option(
        None, "--qos-priority", help="0–7 or none to disable"
    ),
    vsi_manager_id: int | None = typer.Option(None, "--vsi-manager-id"),
    vsi_type_id: int | None = typer.Option(None, "--vsi-type-id"),
    vsi_type_version: int | None = typer.Option(None, "--vsi-type-version"),
    remove_vsi_profile: bool = typer.Option(False, "--remove-vsi-profile"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Update one running client Ethernet adapter; never retry uncertainty blindly."""
    if not exclusive_writer_window:
        raise typer.BadParameter(
            "--exclusive-writer-window is required, including with --yes"
        )
    try:
        _validate_network_update_target(lpar, adapter_uuid, system)
        vlans = json.loads(addl_vlan_ids) if addl_vlan_ids is not None else None
        qos = (
            int(qos_priority, 10)
            if qos_priority not in (None, "none")
            else qos_priority
        )
        fields: dict[str, Any] = {
            "tagged": tagged,
            "addl_vlan_ids": vlans,
            "vlan_action": vlan_action,
            "qos_priority": qos,
            "vsi_manager_id": vsi_manager_id,
            "vsi_type_id": vsi_type_id,
            "vsi_type_version": vsi_type_version,
            "remove_vsi_profile": remove_vsi_profile,
        }
        validate_network_adapter_update_fields(**fields)
    except (ValueError, TypeError) as exc:
        raise typer.BadParameter(terminal_field(str(exc), err=True)) from exc
    if not yes and not typer.confirm(
        f"Update network adapter '{terminal_field(adapter_uuid)}' on '{terminal_field(lpar)}'?"
    ):
        raise typer.Abort()
    result = with_client(
        lambda hmc: update_network_adapter(
            hmc,
            lpar,
            adapter_uuid,
            system_name_or_uuid=system,
            exclusive_writer_window=True,
            ownership_override=ownership_override,
            **fields,
        )
    )
    print_json(asdict(result))
    verified = (
        result.verification.status == "verified"
        and result.preservation.status == "unchanged"
    )
    completed = result.submission == "completed" and result.mutation_dispatched
    no_write = (
        result.submission == "not_dispatched"
        and not result.mutation_dispatched
        and result.reason == "already_satisfied"
    )
    if not verified or not (completed or no_write):
        console.print(
            "Change may have occurred. Inspect the independent observations; do not retry blindly.",
            markup=False,
        )
        raise typer.Exit(1)


def _network_cli_creation_fields(
    tagged: bool, values: dict[str, Any]
) -> dict[str, Any]:
    selected = {name: value for name, value in values.items() if value is not None}
    for name in ("addl_vlan_ids", "allowed_os_mac_addrs"):
        if name not in selected:
            continue
        raw = selected[name]
        if name == "allowed_os_mac_addrs" and raw in ("all", "none"):
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise typer.BadParameter(
                terminal_field(f"{name} must be a JSON array", err=True)
            ) from exc
        if not isinstance(parsed, list):
            raise typer.BadParameter(
                terminal_field(f"{name} must be a JSON array", err=True)
            )
        selected[name] = parsed
    if "qos_priority" in selected and selected["qos_priority"] != "none":
        try:
            selected["qos_priority"] = int(selected["qos_priority"], 10)
        except (ValueError, TypeError) as exc:
            raise typer.BadParameter("qos_priority must be 0–7 or none") from exc
    if selected:
        try:
            validate_network_adapter_creation_fields(tagged=tagged, **selected)
        except ValueError as exc:
            raise typer.BadParameter(terminal_field(str(exc), err=True)) from exc
    return selected


def adapters_add_vscsi(
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    vios_id: int = typer.Option(..., "--vios-id", help="VIOS PartitionID (integer)"),
    vios_slot: int = typer.Option(..., "--vios-slot", help="VIOS server-side slot"),
    slot: int | None = typer.Option(
        None, "--slot", help="Client virtual slot (auto if omitted)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Add a Virtual SCSI client adapter, paired to a VIOS."""

    if not yes and not typer.confirm(
        f"Add vSCSI adapter to '{terminal_field(lpar)}' via VIOS {terminal_field(vios_id)}?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: add_vscsi_adapter(
            hmc,
            lpar,
            vios_id,
            vios_slot,
            slot_number=slot,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
        )
    )
    _adapter_mutation(result, lpar, "vSCSI")


def adapters_add_vfc(
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    vios_id: int = typer.Option(..., "--vios-id", help="VIOS PartitionID (integer)"),
    vios_slot: int = typer.Option(..., "--vios-slot", help="VIOS server-side FC slot"),
    slot: int | None = typer.Option(
        None, "--slot", help="Client virtual slot (auto if omitted)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Add a Virtual Fibre Channel (NPIV) client adapter, paired to a VIOS."""

    if not yes and not typer.confirm(
        f"Add vFC adapter to '{terminal_field(lpar)}' via VIOS {terminal_field(vios_id)}?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: add_vfc_adapter(
            hmc,
            lpar,
            vios_id,
            vios_slot,
            slot_number=slot,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
        )
    )
    _adapter_mutation(result, lpar, "vFC")


def adapters_delete(
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    adapter_type: AdapterType = typer.Option(..., "--type", "-t", help=_ADAPTER_TYPES),
    adapter_uuid: str = typer.Option(
        ..., "--uuid", help="Adapter UUID (from `adapters list`)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Remove a virtual adapter from an LPAR."""

    if not yes and not typer.confirm(
        f"Delete {terminal_field(adapter_type)} {terminal_field(adapter_uuid)} from '"
        f"{terminal_field(lpar)}'?"
    ):
        raise typer.Abort()

    location = with_client(
        lambda hmc: delete_adapter(
            hmc,
            lpar,
            adapter_type,
            adapter_uuid,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
        )
    )

    console.print(
        Text.assemble(
            (
                f"Deleted {terminal_field(adapter_type)} {terminal_field(adapter_uuid)}",
                "green",
            ),
            f" from {terminal_field(lpar)}",
        )
    )
    console.print(terminal_field(location.summary()), markup=False)


def _adapter_mutation(result: AdapterResult, lpar: str, kind: str) -> None:
    if result.creation_verification is None:
        console.print(
            Text.assemble(
                (f"Added {terminal_field(kind)} adapter", "green"),
                f" to {terminal_field(result.lpar_uuid)}",
            )
        )
    else:
        console.print(
            "Creation acknowledged; selected-field verification follows.", markup=False
        )
    print_json(result.resource)
    console.print(terminal_field(result.change_location.summary()), markup=False)
    if result.creation_verification is not None:
        print_json(result.creation_verification)
        if result.creation_verification["status"] != "verified":
            console.print(
                "Creation may have occurred. Inspect the adapter; do not retry blindly.",
                markup=False,
            )
            raise typer.Exit(code=1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(adapters_list)
    group.command("add-network")(adapters_add_network)
    group.command("update-network")(adapters_update_network)
    group.command("add-vscsi")(adapters_add_vscsi)
    group.command("add-vfc")(adapters_add_vfc)
    group.command("delete")(adapters_delete)
