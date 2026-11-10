"""SSH inventory commands for physical I/O, Fibre Channel, and SEA resources."""

from __future__ import annotations

import shlex
from typing import Any, Literal, get_args

from ..config import HMCConfig
from ..documents.native_records import build_filter
from .commands import HMC_NO_RESULTS, _parse_lshwres_output, parse_hmc_result_rows
from .transport import run_hmc_command

# The default slot listing prints pci_class as four upper-case hex digits (V10R3).
_IO_SLOT_PCI_CLASS = {"eth": "0200", "sas": "0104", "san": "0C04", "nvme": "0108"}
PciClass = Literal["all", "eth", "sas", "san", "nvme"]
_VALID_PCI_CLASSES = frozenset(get_args(PciClass))


async def list_io_slots(
    config: HMCConfig, system_name: str, pci_class: PciClass = "all"
) -> list[dict[str, Any]]:
    """List physical I/O slots on *system_name* via SSH."""
    if pci_class not in _VALID_PCI_CLASSES:
        valid = ", ".join(sorted(_VALID_PCI_CLASSES))
        raise ValueError(f"Invalid pci_class {pci_class!r}. Must be one of: {valid}")
    command = f"lshwres -r io --rsubtype slot -m {shlex.quote(system_name)}"
    slots = _parse_lshwres_output(await run_hmc_command(config, command))
    if pci_class == "all":
        return slots
    return [
        slot for slot in slots if slot.get("pci_class") == _IO_SLOT_PCI_CLASS[pci_class]
    ]


async def list_dedicated_pcie_slot_rows(
    config: HMCConfig, system_name: str
) -> list[dict[str, str]]:
    """Read the exact dedicated-slot projection admitted by ADR 0053."""
    fields = ("drc_index", "description", "lpar_name")
    command = f"lshwres -r io --rsubtype slot -m {shlex.quote(system_name)} -F {','.join(fields)} --header"
    return parse_hmc_result_rows(
        await run_hmc_command(config, command), fields, "dedicated PCIe slot inventory"
    )


async def list_fc_ports(
    config: HMCConfig, system_name: str, lpar_name: str | None = None
) -> list[dict[str, str]]:
    """List Virtual Fibre Channel adapters via SSH."""
    command = (
        f"lshwres -r virtualio --rsubtype fc --level lpar -m {shlex.quote(system_name)}"
    )
    if lpar_name:
        command += f" --filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))}"
    return _parse_lshwres_output(await run_hmc_command(config, command))


async def list_sea_adapters(
    config: HMCConfig, system_name: str, lpar_name: str | None = None
) -> list[dict[str, str]]:
    """List Shared Ethernet Adapter virtual Ethernet ports via SSH."""
    fields = "lpar_name,port_vlan_id,vswitch,state,trunk_priority"
    command = f"lshwres -r virtualio --rsubtype eth --level lpar -m {shlex.quote(system_name)} -F {fields}"
    if lpar_name:
        command += f" --filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))}"
    raw = await run_hmc_command(config, command)
    if raw.strip() in {"", HMC_NO_RESULTS}:
        return []
    keys = fields.split(",")
    return [
        dict(zip(keys, line.split(",", len(keys) - 1)))
        for line in raw.strip().splitlines()
    ]
