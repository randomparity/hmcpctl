"""Fixed typed C12 creation grammar; no arbitrary attribute input."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..documents.lpar_io import LparPhysicalIO, decimal_id, drc
from ..operations.virtualization.validation import validate_capacity_percent

if TYPE_CHECKING:
    from ..operations.lpar.assignments import SriovLogicalPortAssignment


def creation_io_record(
    io: LparPhysicalIO, ports: tuple[SriovLogicalPortAssignment, ...] = ()
) -> str:
    """Render only validated C12 fields and fixed direct-SR-IOV declarations.

    P10 mksyscfg:41 / P11:49 define quoted list attributes and colon-separated
    SR-IOV properties. The generic record builder's narrower contract is unchanged.
    """
    if not isinstance(io, LparPhysicalIO):
        raise TypeError("physical_io must be LparPhysicalIO")
    pairs: list[tuple[str, str]] = []
    if io.io_slots:
        pairs.append(
            (
                "io_slots",
                ",".join(
                    f"{s.drc_index}/{s.pool_id or ''}/{int(s.is_required)}"
                    for s in io.io_slots
                ),
            )
        )
    if io.lpar_io_pool_ids:
        pairs.append(("lpar_io_pool_ids", ",".join(io.lpar_io_pool_ids)))
    pairs.extend(io.tagged())
    if io.virtual_opti_pool_id is not None:
        pairs.append(("virtual_opti_pool_id", str(io.virtual_opti_pool_id)))
    records = []
    identities = set()
    if len(ports) > 256:
        raise ValueError("at most 256 direct SR-IOV ports may be created")
    for port in ports:
        adapter, physical, logical = (
            decimal_id(port.adapter_id),
            decimal_id(port.physical_port_id),
            drc(port.logical_port_id),
        )
        if adapter == "0" or port.profile_name != "default_profile":
            raise ValueError(
                "creation SR-IOV requires positive adapter and default_profile"
            )
        if logical in identities:
            raise ValueError("duplicate creation SR-IOV logical DRC")
        identities.add(logical)
        capacity = validate_capacity_percent(port.capacity_percent)
        records.append(
            f"adapter_id={adapter}:phys_port_id={physical}:logical_port_id={logical}:capacity={capacity}"
        )
    if records:
        pairs.append(("sriov_eth_logical_ports", ",".join(records)))
    return ",".join(
        f'"{name}={value}"' if "," in value else f"{name}={value}"
        for name, value in pairs
    )
