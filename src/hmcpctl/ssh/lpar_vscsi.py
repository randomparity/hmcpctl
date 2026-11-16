"""Strict vSCSI client saved-profile observation; no VIOS read or pairing claim."""

from __future__ import annotations

from typing import NamedTuple

from ..config import HMCConfig
from .lpar_serial import SERIAL_OBSERVATION_BYTES, bound_profile_attributes
from .profiles import read_lpar_profile_record


def _slot(value: str, label: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"vSCSI observation has invalid {label}")
    return int(value)


def parse_vscsi_profile(
    record: str, name: str, partition_id: int, profile_name: str
) -> dict[int, tuple[str, ...]]:
    """Return the bound profile's vSCSI records keyed by local slot."""
    values = bound_profile_attributes(record, name, partition_id, profile_name)
    raw = values.get("virtual_scsi_adapters")
    if raw is None:
        raise ValueError("vSCSI profile attribute is unavailable")
    records: dict[int, tuple[str, ...]] = {}
    for text in [] if raw in {"", "none"} else raw.split(","):
        fields = tuple(text.split("/"))
        if len(fields) != 6:
            raise ValueError("vSCSI observation must have six components")
        slot = _slot(fields[0], "local slot")
        if slot in records:
            raise ValueError("duplicate observed vSCSI slot")
        _slot(fields[4], "remote slot")
        records[slot] = fields
    return records


class VscsiProfile(NamedTuple):
    """The bound profile's vSCSI records and its complete attribute map."""

    records: dict[int, tuple[str, ...]]
    attributes: dict[str, str]


async def read_vscsi_profile(
    config: HMCConfig, system: str, name: str, partition_id: int, profile_name: str
) -> VscsiProfile:
    """Use the existing profile reader, then reject unbound or malformed evidence."""
    record = await read_lpar_profile_record(
        config,
        system,
        name,
        profile_name,
        max_output_bytes=SERIAL_OBSERVATION_BYTES,
    )
    return VscsiProfile(
        parse_vscsi_profile(record, name, partition_id, profile_name),
        bound_profile_attributes(record, name, partition_id, profile_name),
    )
