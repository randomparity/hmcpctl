"""CLI commands for virtual adapters (network / storage) on LPARs."""

from __future__ import annotations

import typer
from rich.text import Text

from ...client.client_contracts import ADAPTER_TYPES, AdapterType
from ...operations.virtualization.adapters import (
    AdapterResult,
    add_network_adapter,
    add_vfc_adapter,
    add_vscsi_adapter,
    delete_adapter,
    list_adapters,
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

    adapters = with_client(lambda hmc: list_adapters(hmc, system, lpar, adapter_type))

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
) -> None:
    """Add a Virtual Ethernet (network) adapter to an LPAR."""

    if not yes and not typer.confirm(
        f"Add network adapter (VLAN {terminal_field(vlan)}) to '{terminal_field(lpar)}'?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: add_network_adapter(
            hmc,
            system,
            lpar,
            vlan,
            slot_number=slot,
            virtual_switch_id=virtual_switch_id,
            tagged=tagged,
            mac_address=mac,
            ownership_override=ownership_override,
        )
    )
    _adapter_mutation(result, lpar, "network")


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
            system,
            lpar,
            vios_id,
            vios_slot,
            slot_number=slot,
            ownership_override=ownership_override,
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
            system,
            lpar,
            vios_id,
            vios_slot,
            slot_number=slot,
            ownership_override=ownership_override,
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
            system,
            lpar,
            adapter_type,
            adapter_uuid,
            ownership_override=ownership_override,
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
    console.print(
        Text.assemble(
            (f"Added {terminal_field(kind)} adapter", "green"),
            f" to {terminal_field(result.lpar_uuid)}",
        )
    )
    print_json(result.resource)
    console.print(terminal_field(result.change_location.summary()), markup=False)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(adapters_list)
    group.command("add-network")(adapters_add_network)
    group.command("add-vscsi")(adapters_add_vscsi)
    group.command("add-vfc")(adapters_add_vfc)
    group.command("delete")(adapters_delete)
