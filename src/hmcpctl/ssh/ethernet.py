"""One fixed running-client Ethernet command; policy and observations live in operations."""

from __future__ import annotations

import math
import shlex

from ..config import HMCConfig
from .commands import build_attribute_record
from .transport import run_hmc_command


async def _set_ethernet_adapter(
    config: HMCConfig,
    system_name: str,
    partition_id: int,
    slot_number: int,
    attributes: list[tuple[str, object]],
) -> str:
    """Submit one validated attribute record without retry or disruptive options."""
    if not math.isfinite(config.ssh_timeout) or config.ssh_timeout <= 0:
        raise ValueError("Ethernet update requires a finite positive ssh_timeout")
    if (
        not isinstance(system_name, str)
        or not system_name.strip()
        or any(
            ord(character) < 32 or ord(character) == 127 for character in system_name
        )
    ):
        raise ValueError(
            "Ethernet update requires a nonblank system name without controls"
        )
    if type(partition_id) is not int or partition_id <= 0:
        raise ValueError("partition_id must be a positive integer")
    if type(slot_number) is not int or not 0 <= slot_number <= 65535:
        raise ValueError("slot_number must be an unsigned-short integer")
    allowed = {
        "ieee_virtual_eth",
        "qos_priority",
        "vsi_manager_id",
        "vsi_type_id",
        "vsi_type_version",
        "addl_vlan_ids",
        "addl_vlan_ids+",
        "addl_vlan_ids-",
    }
    names = [name for name, _ in attributes]
    if not names or any(name not in allowed for name in names):
        raise ValueError(
            "Ethernet update requires only the six supported native fields"
        )
    if len({name.rstrip("+-") for name in names}) != len(names):
        raise ValueError(
            "Ethernet update cannot repeat a field or mix its list actions"
        )
    scalars = [pair for pair in attributes if not pair[0].startswith("addl_vlan_ids")]
    vlans = [pair for pair in attributes if pair[0].startswith("addl_vlan_ids")]
    record = build_attribute_record(
        scalars + vlans, quoted=[name for name, _ in vlans], surface="`chhwres -a`"
    )
    command = (
        "chhwres -r virtualio --rsubtype eth -o s "
        f"-m {shlex.quote(system_name)} --id {partition_id} -s {slot_number} "
        f"-a {shlex.quote(record)}"
    )
    return await run_hmc_command(config, command, max_output_bytes=65536)
