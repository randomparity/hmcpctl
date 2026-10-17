"""CLI commands for virtual networks, switches, and bridges."""

from __future__ import annotations

import typer

from ...operations.virtualization.network import (
    create_virtual_network,
    delete_virtual_network,
    list_network_bridges,
    list_virtual_networks,
    list_virtual_switches,
)
from ..output import (
    VerbatimTable,
    console,
    first_field,
    output,
    print_json,
    terminal_field,
)
from ..runtime import with_client


def network_list_switches(
    system: str = typer.Argument(..., help="Managed system UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List VirtualSwitches on a managed system."""

    switches = with_client(lambda hmc: list_virtual_switches(hmc, system))

    table = None
    if not as_json:
        table = VerbatimTable(title=f"Virtual Switches on {system}")
        for col in ("Name", "SwitchID", "Mode", "UUID"):
            table.add_column(col)
        for s in switches:
            table.add_row(
                first_field(s, "SwitchName"),
                first_field(s, "SwitchID"),
                first_field(s, "SwitchMode"),
                s.get("UUID") or "-",
            )
    output(switches, as_json, table, "No virtual switches found")


def network_list_networks(
    system: str = typer.Argument(..., help="Managed system UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List Virtual Networks (VLANs) on a managed system."""

    nets = with_client(lambda hmc: list_virtual_networks(hmc, system))

    table = None
    if not as_json:
        table = VerbatimTable(title=f"Virtual Networks on {system}")
        for col in ("Name", "VLAN", "VswitchID", "Tagged", "UUID"):
            table.add_column(col)
        for n in nets:
            table.add_row(
                first_field(n, "NetworkName"),
                first_field(n, "NetworkVLANID"),
                first_field(n, "VswitchID"),
                first_field(n, "TaggedNetwork"),
                n.get("UUID") or "-",
            )
    output(nets, as_json, table, "No virtual networks found")


def network_create(
    system: str = typer.Argument(..., help="Managed system UUID"),
    name: str = typer.Option(..., "--name", "-n", help="Network name"),
    vlan: int = typer.Option(..., "--vlan", help="VLAN ID"),
    virtual_switch_id: int = typer.Option(
        ..., "--virtual-switch-id", help="Backing VirtualSwitch numeric SwitchID"
    ),
    tagged: bool = typer.Option(False, "--tagged", help="Tagged (bridged) network"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Create a Virtual Network (VLAN) on a managed system."""
    if not yes and not typer.confirm(
        f"Create network '{terminal_field(name)}' (VLAN {terminal_field(vlan)}"
        f", switch ID {terminal_field(virtual_switch_id)}) on "
        f"{terminal_field(system)}?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: create_virtual_network(
            hmc, system, name, vlan, virtual_switch_id, tagged=tagged
        )
    )

    console.print(
        f"Created virtual network '{terminal_field(name)}'", style="green", markup=False
    )
    print_json(result.resource)


def network_delete(
    system: str = typer.Argument(..., help="Managed system UUID"),
    uuid: str = typer.Option(..., "--uuid", help="Virtual Network UUID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a Virtual Network from a managed system."""
    if not yes and not typer.confirm(
        f"Delete virtual network {terminal_field(uuid)} from {terminal_field(system)}?"
    ):
        raise typer.Abort()

    with_client(lambda hmc: delete_virtual_network(hmc, system, uuid))

    console.print(
        f"Deleted virtual network {terminal_field(uuid)}", style="green", markup=False
    )


def network_list_bridges(
    system: str = typer.Argument(..., help="Managed system UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List NetworkBridges (Shared Ethernet Adapters) on a managed system."""

    bridges = with_client(lambda hmc: list_network_bridges(hmc, system))

    output(bridges, as_json, None, "No network bridges found")


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list-switches")(network_list_switches)
    group.command("list-networks")(network_list_networks)
    group.command("create")(network_create)
    group.command("delete")(network_delete)
    group.command("list-bridges")(network_list_bridges)
