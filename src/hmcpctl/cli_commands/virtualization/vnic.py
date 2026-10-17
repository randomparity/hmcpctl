"""CLI commands for FC, SEA, and vNIC resources."""

from __future__ import annotations

import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from decimal import Decimal

import typer

from ...client.core import HMCClient
from ...operations.virtualization.vnic import (
    VnicBackingSelector,
    VnicPartialError,
    add_vnic,
    list_fc_ports,
    list_sea_adapters,
    list_vnics,
    remove_vnic,
)
from ..output import output, print_json, terminal_field
from ..runtime import with_client


def _confirm_on_stderr(prompt: str) -> bool:
    """Keep confirmation prompts and terminal input echoes off JSON stdout."""
    with redirect_stdout(sys.stderr):
        return typer.confirm(terminal_field(prompt, err=True), err=True)


def network_list_fc_ports(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar_name: str | None = typer.Option(
        None, "--lpar", help="Filter by LPAR name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List Virtual Fibre Channel (NPIV) adapters on a managed system."""

    ports = with_client(lambda hmc: list_fc_ports(hmc, system_name, lpar_name))

    output(ports, as_json, None, "No FC ports found")


def network_list_sea_adapters(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar_name: str | None = typer.Option(
        None, "--lpar", help="Filter by LPAR name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List Shared Ethernet Adapter (SEA) virtual Ethernet ports on a managed system."""

    adapters = with_client(lambda hmc: list_sea_adapters(hmc, system_name, lpar_name))

    output(adapters, as_json, None, "No SEA adapters found")


def network_list_vnics(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List vNICs (SR-IOV-backed Virtual NICs) on an LPAR (HMC CLI via SSH)."""
    vnics = with_client(lambda hmc: list_vnics(hmc, system_name, lpar))
    output(vnics, as_json, None, "No vNICs found")


def network_add_vnic(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    vios_name: str = typer.Option(..., "--vios-name"),
    vios_lpar_id: str = typer.Option(..., "--vios-lpar-id"),
    adapter_id: str = typer.Option(..., "--adapter-id"),
    physical_port_id: str = typer.Option(..., "--physical-port-id"),
    capacity_percent: float = typer.Option(..., "--capacity-percent"),
    port_vlan_id: int = typer.Option(..., "--port-vlan-id"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Add and verify a vNIC with one typed SR-IOV backing selector."""
    if not yes and not _confirm_on_stderr(
        f"Add vNIC (VIOS={vios_name}, adapter={adapter_id}, "
        f"port={physical_port_id}, capacity={capacity_percent}, vlan={port_vlan_id}) "
        f"to '{lpar}' on '{system_name}'?"
    ):
        raise typer.Abort()

    async def operation(hmc: HMCClient):
        try:
            return await add_vnic(
                hmc,
                system_name,
                lpar,
                VnicBackingSelector(
                    vios_name,
                    vios_lpar_id,
                    adapter_id,
                    physical_port_id,
                    Decimal(str(capacity_percent)),
                ),
                port_vlan_id,
                ownership_override=ownership_override,
            )
        except VnicPartialError as exc:
            return exc

    outcome = with_client(operation)
    result = outcome.result if isinstance(outcome, VnicPartialError) else outcome
    print_json(asdict(result))
    if isinstance(outcome, VnicPartialError):
        raise typer.Exit(1)


def network_remove_vnic(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar: str = typer.Argument(..., help="LPAR name or UUID"),
    slot_num: str = typer.Argument(..., help="vNIC slot number (from list-vnics)"),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove a vNIC from an LPAR (HMC CLI via SSH)."""
    if not yes and not _confirm_on_stderr(
        f"Remove vNIC slot {slot_num} from '{lpar}' on '{system_name}'?"
    ):
        raise typer.Abort()

    async def operation(hmc: HMCClient):
        try:
            return await remove_vnic(
                hmc,
                system_name,
                lpar,
                slot_num,
                ownership_override=ownership_override,
            )
        except VnicPartialError as exc:
            return exc

    outcome = with_client(operation)
    result = outcome.result if isinstance(outcome, VnicPartialError) else outcome
    print_json(asdict(result))
    if isinstance(outcome, VnicPartialError):
        raise typer.Exit(1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list-fc-ports")(network_list_fc_ports)
    group.command("list-sea-adapters")(network_list_sea_adapters)
    group.command("list-vnics")(network_list_vnics)
    group.command("add-vnic")(network_add_vnic)
    group.command("remove-vnic")(network_remove_vnic)
