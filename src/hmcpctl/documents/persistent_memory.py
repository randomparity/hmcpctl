"""VirtualPersistentMemoryVolume parsing and request documents (#643, ADR 0373)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..xmlutil import escapes_string_arguments, leaf_text
from .common import document_envelope

_VOLUME_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# The V10R3 capture: every element names its schema level, or the write fails
# REST0001; a rename must resend Size and AssociatedPartitionId, which are
# create-only (COR) and otherwise unchangeable.
_KSV = 'ksv="V1_10_0" kxe="false"'


@dataclass(frozen=True)
class PersistentMemoryVolume:
    """One volume as the HMC reports it; partition fields are None when unassigned."""

    uuid: str
    name: str
    size_mib: int
    current_size_mib: int | None
    volume_id: int | None
    affinity: bool
    partition_id: int | None
    partition_name: str | None
    partition_uuid: str | None


def _text(resource: Mapping[str, Any], field: str) -> str | None:
    value = leaf_text(resource.get(field))
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"VirtualPersistentMemoryVolume {field} is malformed")
    return value.strip()


def _required_text(resource: Mapping[str, Any], field: str) -> str:
    value = _text(resource, field)
    if value is None:
        raise ValueError(f"VirtualPersistentMemoryVolume has no {field}")
    return value


def _integer(resource: Mapping[str, Any], field: str, *, minimum: int) -> int | None:
    value = _text(resource, field)
    if value is None:
        return None
    if not value.isdigit() or int(value) < minimum:
        raise ValueError(f"VirtualPersistentMemoryVolume {field} is malformed")
    return int(value)


def _affinity(resource: Mapping[str, Any]) -> bool:
    value = _text(resource, "Affinity")
    if value not in (None, "true", "false"):
        raise ValueError("VirtualPersistentMemoryVolume Affinity is malformed")
    return value == "true"


def _partition_uuid(resource: Mapping[str, Any]) -> str | None:
    link = resource.get("AssociatedPartition")
    href = link.get("href") if isinstance(link, Mapping) else None
    if not isinstance(href, str) or "/LogicalPartition/" not in href:
        return None
    return href.rstrip("/").rsplit("/", 1)[-1] or None


def parse_volume(entry: Mapping[str, Any]) -> PersistentMemoryVolume:
    """Map one parsed feed entry; fail closed on a missing or malformed field."""
    resource = entry.get("Resource")
    if not isinstance(resource, Mapping):
        resource = {}
    size = _integer(resource, "Size", minimum=1)
    if size is None:
        raise ValueError("VirtualPersistentMemoryVolume has no Size")
    return PersistentMemoryVolume(
        uuid=_required_text(resource, "Uuid"),
        name=_required_text(resource, "Name"),
        size_mib=size,
        current_size_mib=_integer(resource, "CurrentSize", minimum=0),
        volume_id=_integer(resource, "VolumeId", minimum=0),
        affinity=_affinity(resource),
        partition_id=_integer(resource, "AssociatedPartitionId", minimum=0),
        partition_name=_text(resource, "AssociatedPartitionName"),
        partition_uuid=_partition_uuid(resource),
    )


def validate_volume_name(name: str) -> str:
    """Admit a new volume name; never applied to selectors of existing volumes."""
    if not isinstance(name, str):
        raise TypeError("volume name must be a string")
    if not _VOLUME_NAME.fullmatch(name):
        raise ValueError(
            "volume name must be 1-64 characters of A-Z a-z 0-9 _ . -; "
            "nothing was changed"
        )
    return name


@escapes_string_arguments
def build_volume_document(
    name: str, size_mib: int, *, affinity: bool, partition_id: int
) -> str:
    """The create (PUT) and rename (POST) body; the HMC requires the same fields."""
    body = (
        "  <Metadata><Atom/></Metadata>\n"
        f'  <Name kb="CUR" {_KSV}>{name}</Name>\n'
        f'  <Size kb="COR" {_KSV}>{int(size_mib)}</Size>\n'
        f'  <Affinity kb="COD" {_KSV}>{"true" if affinity else "false"}</Affinity>\n'
        f'  <AssociatedPartitionId kb="COR" {_KSV}>{int(partition_id)}'
        "</AssociatedPartitionId>"
    )
    return document_envelope("VirtualPersistentMemoryVolume", body)
