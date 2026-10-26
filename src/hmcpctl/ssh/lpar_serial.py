"""Strict serial saved-profile observations; no serial mutation or profile lifecycle."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from ..config import HMCConfig
from .profiles import read_lpar_profile_record

SERIAL_OBSERVATION_BYTES = 1048576


@dataclass(frozen=True)
class SerialProfile:
    """A profile bound to native identity, without a pairing or active-state claim."""

    name: str
    partition_id: int
    profile_name: str
    maximum: int
    records: dict[int, tuple[str, ...]]
    other_virtual_configuration: bool


def _decimal(value: str, label: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"serial observation has invalid {label}")
    return int(value)


def _profile_attributes(record: str) -> dict[str, str]:
    if (
        len(record) > SERIAL_OBSERVATION_BYTES
        or len(record.encode("utf-8")) > SERIAL_OBSERVATION_BYTES
    ):
        raise ValueError("serial profile observation exceeds 1 MiB")
    try:
        items = next(csv.reader([record], strict=True), [])
    except csv.Error as error:
        raise ValueError("malformed serial profile CSV") from error
    rendered = io.StringIO()
    csv.writer(rendered, lineterminator="").writerow(items)
    if not items or rendered.getvalue() != record:
        raise ValueError("noncanonical serial profile CSV")
    values = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key or key in values:
            raise ValueError("malformed or duplicate profile attribute")
        if not key.isascii() or not key.replace("_", "a").isalnum():
            raise ValueError("invalid profile attribute name")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("profile attribute contains a control character")
        values[key] = value
    return values


def parse_serial_profile(
    record: str, name: str, partition_id: int, profile_name: str
) -> SerialProfile:
    """Bind complete native CSV to its requested profile and partition identity."""
    values = _profile_attributes(record)
    if (
        values.get("name") != profile_name
        or values.get("lpar_name") != name
        or _decimal(values.get("lpar_id", ""), "lpar_id") != partition_id
    ):
        raise ValueError("serial profile identity does not match contained partition")
    maximum = _decimal(values.get("max_virtual_slots", ""), "maximum")
    if maximum < 2:
        raise ValueError("serial profile maximum cannot contain reserved slots")
    raw = values.get("virtual_serial_adapters")
    if raw is None:
        raise ValueError("serial profile attribute is unavailable")
    records = {}
    for text in [] if raw in {"", "none"} else raw.split(","):
        fields = tuple(text.split("/"))
        if len(fields) != 7:
            raise ValueError("serial observation must have seven components")
        slot = _decimal(fields[0], "local slot")
        if slot in records or slot >= maximum:
            raise ValueError("duplicate or out-of-range observed serial slot")
        if fields[1] not in {"client", "server"} or fields[6] not in {"0", "1"}:
            raise ValueError("invalid observed serial role or required flag")
        if fields[2] not in {"", "0", "1"}:
            raise ValueError("unknown observed HMC flag")
        for index in (3, 5):
            if fields[index] not in {"", "any"}:
                _decimal(fields[index], "remote component")
        if slot < 2 and fields != (str(slot), "server", "1", "any", "", "any", "1"):
            raise ValueError("unrecognized reserved serial configuration")
        records[slot] = fields
    other = any(
        values.get(key, "none") not in {"", "none"}
        for key in (
            "virtual_eth_adapters",
            "virtual_fc_adapters",
            "virtual_scsi_adapters",
            "vnic_adapters",
        )
    )
    return SerialProfile(name, partition_id, profile_name, maximum, records, other)


async def read_serial_profile(
    config: HMCConfig, system: str, name: str, partition_id: int, profile_name: str
) -> SerialProfile:
    """Use the existing profile reader, then reject unbound or malformed evidence."""
    return parse_serial_profile(
        await read_lpar_profile_record(
            config,
            system,
            name,
            profile_name,
            max_output_bytes=SERIAL_OBSERVATION_BYTES,
        ),
        name,
        partition_id,
        profile_name,
    )
