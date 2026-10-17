"""CLI commands for PCIe and SR-IOV resources."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import typer

from ...operations.virtualization.pcie import (
    InventorySelector,
    assign_dedicated_pcie_slot,
    assign_sriov_logical_port,
    list_dedicated_slots,
    list_sriov_adapters,
    list_sriov_logical_ports,
    list_sriov_physical_ports,
    set_sriov_adapter_mode,
    unassign_dedicated_pcie_slot,
    unassign_sriov_logical_port,
)
from ...ssh.io_inventory import PciClass, list_io_slots
from ...ssh.sriov import SriovMode
from ..output import VerbatimTable, console, output, print_json, terminal_field
from ..runtime import run_cli_coroutine, ssh_config, with_client


def _print_pcie_inventory(result, as_json: bool) -> None:
    if as_json:
        print_json(asdict(result))
        return
    if result.capability == "capability-unavailable":
        console.print(
            f"Capability unavailable: {terminal_field(result.unavailable_reason)}",
            markup=False,
        )
        return
    if not result.items:
        console.print(
            f"{terminal_field(result.resource_kind)} available; no items found",
            markup=False,
        )
        return

    rows = [asdict(item) for item in result.items]
    table = VerbatimTable(title=f"{result.resource_kind} inventory on {result.system}")
    for field_name in rows[0]:
        table.add_column(field_name)
    for row in rows:
        table.add_row(
            *(str(value) if value is not None else "-" for value in row.values())
        )
    console.print(table)


def network_list_dedicated_pcie_slots(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List normalized dedicated PCIe slots on a managed system."""
    result = with_client(lambda hmc: list_dedicated_slots(hmc, system_name))
    _print_pcie_inventory(result, as_json)


def network_assign_dedicated_pcie_slot(
    system_name: str,
    lpar_name: str,
    profile_name: str,
    drc_index: str,
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Assign a dedicated slot when safe profile readback is available."""
    with_client(
        lambda hmc: assign_dedicated_pcie_slot(
            hmc,
            system_name,
            lpar_name,
            profile_name,
            drc_index,
            ownership_override=ownership_override,
        )
    )


def network_unassign_dedicated_pcie_slot(
    system_name: str,
    lpar_name: str,
    profile_name: str,
    drc_index: str,
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Unassign a dedicated slot when safe profile readback is available."""
    with_client(
        lambda hmc: unassign_dedicated_pcie_slot(
            hmc,
            system_name,
            lpar_name,
            profile_name,
            drc_index,
            ownership_override=ownership_override,
        )
    )


def network_list_sriov_adapters(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    adapter_id: str | None = typer.Option(None, "--adapter-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List normalized SR-IOV adapters or their unavailable capability."""
    result = with_client(lambda hmc: list_sriov_adapters(hmc, system_name, adapter_id))
    _print_pcie_inventory(result, as_json)


def network_list_sriov_physical_ports(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    adapter_id: str | None = typer.Option(None, "--adapter-id"),
    physical_port_id: str | None = typer.Option(None, "--physical-port-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List normalized SR-IOV physical ports or their unavailable capability."""
    result = with_client(
        lambda hmc: list_sriov_physical_ports(
            hmc, system_name, adapter_id, physical_port_id
        )
    )
    _print_pcie_inventory(result, as_json)


def network_list_sriov_logical_ports(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    adapter_id: str | None = typer.Option(None, "--adapter-id"),
    physical_port_id: str | None = typer.Option(None, "--physical-port-id"),
    logical_port_id: str | None = typer.Option(None, "--logical-port-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List normalized SR-IOV logical ports or their unavailable capability."""
    result = with_client(
        lambda hmc: list_sriov_logical_ports(
            hmc,
            system_name,
            adapter_id,
            physical_port_id,
            logical_port_id,
        )
    )
    _print_pcie_inventory(result, as_json)


def network_assign_sriov_logical_port(
    system_name: str,
    lpar_name: str,
    adapter_id: str,
    physical_port_id: str,
    logical_port_id: str,
    capacity_percent: float,
    profile_name: str = typer.Option(..., "--profile-name"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Assign an evidence-backed Ethernet SR-IOV logical port."""
    result = with_client(
        lambda hmc: assign_sriov_logical_port(
            hmc,
            system_name,
            lpar_name,
            InventorySelector(adapter_id, physical_port_id, logical_port_id),
            Decimal(str(capacity_percent)),
            profile_name=profile_name,
            ownership_override=ownership_override,
        )
    )
    print_json(asdict(result))


def network_unassign_sriov_logical_port(
    system_name: str,
    lpar_name: str,
    adapter_id: str,
    physical_port_id: str,
    logical_port_id: str,
    profile_name: str = typer.Option(..., "--profile-name"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Unassign a profile logical port on a Not Activated LPAR."""
    result = with_client(
        lambda hmc: unassign_sriov_logical_port(
            hmc,
            system_name,
            lpar_name,
            InventorySelector(adapter_id, physical_port_id, logical_port_id),
            profile_name=profile_name,
            ownership_override=ownership_override,
        )
    )
    print_json(asdict(result))


def network_list_io_slots(
    system_name: str = typer.Argument(..., help="Managed system name"),
    pci_class: PciClass = typer.Option(
        "all", "--pci-class", help="Filter by PCI class: all, eth, sas, san, nvme"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List physical I/O slots on a managed system (HMC CLI via SSH)."""

    slots = run_cli_coroutine(
        lambda: list_io_slots(ssh_config(), system_name, pci_class)
    )

    output(slots, as_json, None, "No I/O slots found")


def network_set_sriov_mode(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    adapter_id: str = typer.Argument(
        ...,
        help="SR-IOV adapter ID (from `hmcpctl network list-sriov-adapters`)",
    ),
    mode: SriovMode = typer.Argument(..., help="'sriov' or 'dedicated'"),
) -> None:
    """Verify an adapter's current mode; transitions fail closed."""
    result = with_client(
        lambda hmc: set_sriov_adapter_mode(hmc, system_name, adapter_id, mode)
    )

    console.print(
        (
            f"Adapter {terminal_field(adapter_id)} verified in '{terminal_field(mode)}"
            f"' mode on '{terminal_field(system_name)}'"
        ),
        style="green",
        markup=False,
    )
    if result.strip():
        console.print(terminal_field(result.strip()), markup=False)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list-dedicated-pcie-slots")(network_list_dedicated_pcie_slots)
    group.command("assign-dedicated-pcie-slot")(network_assign_dedicated_pcie_slot)
    group.command("unassign-dedicated-pcie-slot")(network_unassign_dedicated_pcie_slot)
    group.command("list-sriov-adapters")(network_list_sriov_adapters)
    group.command("list-sriov-physical-ports")(network_list_sriov_physical_ports)
    group.command("list-sriov-logical-ports")(network_list_sriov_logical_ports)
    group.command("assign-sriov-logical-port")(network_assign_sriov_logical_port)
    group.command("unassign-sriov-logical-port")(network_unassign_sriov_logical_port)
    group.command("list-io-slots")(network_list_io_slots)
    group.command("set-sriov-mode")(network_set_sriov_mode)
