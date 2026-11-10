"""SSH commands for virtual-NIC inventory, backing-device and policy mutation."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any, TypedDict

from ..config import HMCConfig
from ..documents.native_records import build_attribute_record, build_filter
from .commands import HMC_NO_RESULTS, _parse_lshwres_output, parse_hmc_result_rows
from .transport import run_hmc_command


class ViosIdentity(TypedDict):
    name: str
    lpar_id: str
    lpar_env: str


_VNIC_FIELDS = (
    "lpar_name",
    "lpar_id",
    "slot_num",
    "desired_mode",
    "curr_mode",
    "auto_priority_failover",
    "port_vlan_id",
    "pvid_priority",
    "allowed_vlan_ids",
    "mac_addr",
    "allowed_os_mac_addrs",
    "backing_devices",
    "backing_device_states",
)
_VNIC_BACKING_FIELDS = (
    "lpar_name",
    "lpar_id",
    "type",
    "adapter_id",
    "physical_port_id",
    "logical_port_id",
    "capacity",
    "desired_capacity",
    "max_capacity",
    "desired_max_capacity",
    "failover_priority",
    "is_active",
    "status",
)
_VIOS_IDENTITY_FIELDS = ("name", "lpar_id", "lpar_env")


async def list_vnics(
    config: HMCConfig, system_name: str, lpar_name: str
) -> list[dict[str, Any]]:
    command = f"lshwres -r virtualio --rsubtype vnic --level lpar -m {shlex.quote(system_name)} --filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))}"
    output = await run_hmc_command(config, command)
    if not output.strip() or output.strip() == HMC_NO_RESULTS:
        return []
    return _parse_lshwres_output(output)


async def list_vnic_rows(
    config: HMCConfig, system_name: str, lpar_name: str
) -> list[dict[str, str]]:
    command = f"lshwres -r virtualio --rsubtype vnic --level lpar -m {shlex.quote(system_name)} --filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} -F {','.join(_VNIC_FIELDS)} --header"
    return parse_hmc_result_rows(
        await run_hmc_command(config, command), _VNIC_FIELDS, "vNIC inventory"
    )


async def list_vnic_backing_rows(
    config: HMCConfig, system_name: str
) -> list[dict[str, str]]:
    command = f"lshwres -r virtualio --rsubtype vnicbkdev -m {shlex.quote(system_name)} -F {','.join(_VNIC_BACKING_FIELDS)} --header"
    return parse_hmc_result_rows(
        await run_hmc_command(config, command),
        _VNIC_BACKING_FIELDS,
        "vNIC backing-device inventory",
    )


async def read_vios_identity(
    config: HMCConfig, system_name: str, vios_name: str
) -> ViosIdentity:
    command = f"lssyscfg -r lpar -m {shlex.quote(system_name)} --filter {shlex.quote(build_filter([('lpar_names', vios_name)]))} -F {','.join(_VIOS_IDENTITY_FIELDS)} --header"
    rows = parse_hmc_result_rows(
        await run_hmc_command(config, command),
        _VIOS_IDENTITY_FIELDS,
        "VIOS identity read",
    )
    if len(rows) != 1:
        raise ValueError(
            f"VIOS identity read for {vios_name!r} returned {len(rows)} rows; expected 1"
        )
    row = rows[0]
    return ViosIdentity(
        name=row["name"], lpar_id=row["lpar_id"], lpar_env=row["lpar_env"]
    )


def build_vnic_add_attributes(
    backing_device: str, port_vlan_id: int, policy: dict[str, str] | None = None
) -> str:
    payload = build_attribute_record(
        [
            ("port_vlan_id", port_vlan_id),
            ("backing_devices", backing_device),
            *(policy or {}).items(),
        ],
        quoted=("backing_devices", "allowed_vlan_ids", "allowed_os_mac_addrs"),
        surface="`chhwres -a`",
        allow_nontrailing_quoted=bool(policy),
    )
    if policy and len(payload.encode("utf-8")) > 65_536:
        raise ValueError(
            "vNIC creation attribute record exceeds the 65536-byte application limit"
        )
    return payload


async def add_vnic_backing(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    backing_device: str,
    port_vlan_id: int,
    *,
    policy: dict[str, str] | None = None,
) -> str:
    payload = build_vnic_add_attributes(backing_device, port_vlan_id, policy)
    command = f"chhwres -r virtualio --rsubtype vnic -o a -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)} -a {shlex.quote(payload)}"
    return await run_hmc_command(config, command)


async def remove_vnic_slot(
    config: HMCConfig, system_name: str, lpar_name: str, slot_num: str
) -> str:
    command = f"chhwres -r virtualio --rsubtype vnic -o r -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)} -s {shlex.quote(slot_num)}"
    return await run_hmc_command(config, command)


def build_vnic_update_attributes(pairs: Sequence[tuple[str, str]]) -> str:
    """Render a ``chhwres -o s`` record; a quoted list add must be the last pair."""
    payload = build_attribute_record(
        pairs,
        quoted=("allowed_vlan_ids+", "allowed_os_mac_addrs+"),
        surface="`chhwres -a`",
    )
    return payload


async def update_vnic_slot_policy(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    slot_num: str,
    pairs: Sequence[tuple[str, str]],
) -> str:
    record = build_vnic_update_attributes(pairs)
    command = f"chhwres -r virtualio --rsubtype vnic -o s -m {shlex.quote(system_name)} -p {shlex.quote(lpar_name)} -s {shlex.quote(slot_num)} -a {shlex.quote(record)}"
    return await run_hmc_command(config, command)
