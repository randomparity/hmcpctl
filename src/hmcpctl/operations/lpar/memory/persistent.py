"""Virtual persistent-memory volumes: list, read, create, rename, delete (ADR 0373).

Every mutation names the partition the volume is assigned to, runs the ADR 0092
ownership guard on it, checks the advertised preconditions before sending
anything, submits once, and reports an independent readback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ....client.core import HMCClient
from ....documents.persistent_memory import (
    PersistentMemoryVolume,
    build_volume_document,
    parse_volume,
    validate_volume_name,
)
from ....errors import HMCError
from ....resource_identity import (
    ResourceNotFoundError,
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_uuid,
)
from ....xmlutil import leaf_text
from ..ownership import resolve_and_authorize_lpar_mutation

PmemStatus = Literal[
    "verified", "mismatch", "readback-unavailable", "write-outcome-unknown"
]
_UNCHANGED = "nothing was changed"
_PENDING = (
    "An accepted write may still be completing; re-read the volumes before retrying."
)


@dataclass(frozen=True)
class PersistentMemoryVolumeResult:
    """Requested and observed effects of one non-retried submission."""

    status: PmemStatus
    requested: dict[str, object]
    observed: PersistentMemoryVolume | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Partition:
    uuid: str
    partition_id: int
    partition_type: object
    state: object
    resource: Mapping[str, Any]


def _require_bools(**values: object) -> None:
    for name, value in values.items():
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")


def _require_str(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")


def _advertised_int(container: object, field: str, subject: str) -> int:
    value = leaf_text(container.get(field)) if isinstance(container, Mapping) else None
    if not isinstance(value, str) or not value.strip().isdigit() or int(value) <= 0:
        raise ValueError(f"{subject} does not advertise {field}; {_UNCHANGED}")
    return int(value)


def _count(container: object, field: str, subject: str) -> int:
    value = leaf_text(container.get(field)) if isinstance(container, Mapping) else None
    if not isinstance(value, str) or not value.strip().isdigit():
        raise ValueError(f"{subject} does not advertise {field}; {_UNCHANGED}")
    return int(value)


async def _volumes(
    hmc: HMCClient, system_uuid: str, partition_id: int | None = None
) -> list[PersistentMemoryVolume]:
    entries = await hmc.list_persistent_memory_volumes(system_uuid, partition_id)
    return [parse_volume(entry) for entry in entries]


async def _partition(hmc: HMCClient, lpar_uuid: str) -> _Partition:
    entry = await hmc.get_logical_partition(lpar_uuid)
    resource = (entry or {}).get("Resource")
    if not isinstance(resource, Mapping):
        resource = {}
    raw_id = leaf_text(resource.get("PartitionID"))
    if not isinstance(raw_id, str) or not raw_id.strip().isdigit():
        raise ValueError(f"Partition {lpar_uuid} has no readable PartitionID")
    return _Partition(
        lpar_uuid,
        int(raw_id),
        leaf_text(resource.get("PartitionType")),
        leaf_text(resource.get("PartitionState")),
        resource,
    )


def _select(
    volumes: list[PersistentMemoryVolume], selector: str
) -> PersistentMemoryVolume:
    """Exactly one volume by UUID or exact name."""
    if is_uuid(selector):
        matches = [v for v in volumes if v.uuid.casefold() == selector.casefold()]
    else:
        matches = [v for v in volumes if v.name == selector]
    if not matches:
        raise ResourceNotFoundError(
            "persistent-memory volume",
            selector,
            f"No persistent-memory volume {selector!r} on the selected system. "
            "Use hmc_list_persistent_memory_volumes to list them.",
        )
    if len(matches) > 1:
        uuids = ", ".join(v.uuid for v in matches)
        raise ValueError(
            f"Volume name {selector!r} matches {len(matches)} volumes ({uuids}); "
            "select one by UUID"
        )
    return matches[0]


def _require_assigned(volume: PersistentMemoryVolume, partition: _Partition) -> None:
    if volume.partition_id is None:
        raise ValueError(
            f"Volume {volume.name!r} is not assigned to a partition, so no ownership "
            f"token authorizes changing it; use the HMC directly; {_UNCHANGED}"
        )
    if volume.partition_id != partition.partition_id:
        raise ValueError(
            f"Volume {volume.name!r} is assigned to partition ID "
            f"{volume.partition_id}, not the selected partition "
            f"(ID {partition.partition_id}); {_UNCHANGED}"
        )


def _require_inactive(partition: _Partition, action: str) -> None:
    if partition.state != "not activated":
        raise ValueError(
            f"{action} requires an inactive partition (state 'not activated'), "
            f"but it reads {partition.state!r}; {_UNCHANGED}"
        )


def _require_unused(volumes: list[PersistentMemoryVolume], name: str) -> None:
    if any(v.name == name for v in volumes):
        raise ValueError(
            f"A volume named {name!r} already exists on the system; {_UNCHANGED}"
        )


async def _system_block_size(hmc: HMCClient, system_uuid: str) -> int:
    entry = await hmc.get_managed_system(system_uuid)
    resource = (entry or {}).get("Resource")
    resource = resource if isinstance(resource, Mapping) else {}
    capabilities = resource.get("AssociatedSystemCapabilities")
    capable = (
        leaf_text(capabilities.get("PersistentMemoryCapable"))
        if isinstance(capabilities, Mapping)
        else None
    )
    if capable != "true":
        raise ValueError(
            f"Selected system does not advertise PersistentMemoryCapable; {_UNCHANGED}"
        )
    configuration = resource.get("AssociatedPersistentMemoryConfiguration")
    types = (
        leaf_text(configuration.get("SupportedPersistentMemoryDeviceTypes"))
        if isinstance(configuration, Mapping)
        else None
    )
    if not isinstance(types, str) or "dram" not in types.split():
        raise ValueError(
            "Selected system does not advertise the dram persistent-memory "
            f"device type; {_UNCHANGED}"
        )
    return _advertised_int(
        configuration, "DramPersistentMemoryVolumeBlockSize", "Selected system"
    )


def _admit_partition_for_create(partition: _Partition) -> None:
    if partition.partition_type != "AIX/Linux":
        raise ValueError(
            "Persistent-memory volumes can be assigned only to an AIX/Linux "
            f"partition, not {partition.partition_type!r}; {_UNCHANGED}"
        )
    _require_inactive(partition, "Creating a volume")
    configuration = partition.resource.get("AssociatedPersistentMemoryConfiguration")
    maximum = _advertised_int(
        configuration, "MaximumDramPersistentMemoryVolumes", "Selected partition"
    )
    current = _count(
        configuration, "CurrentDramPersistentMemoryVolumes", "Selected partition"
    )
    if current >= maximum:
        raise ValueError(
            f"Selected partition already has {current} of its {maximum} DRAM "
            f"persistent-memory volumes; {_UNCHANGED}"
        )


async def _readback_by_name(
    hmc: HMCClient, system_uuid: str, name: str
) -> list[PersistentMemoryVolume]:
    return [v for v in await _volumes(hmc, system_uuid) if v.name == name]


def _unknown_outcome(
    requested: dict[str, object],
    error: HMCError,
    observed: PersistentMemoryVolume | None,
) -> PersistentMemoryVolumeResult:
    return PersistentMemoryVolumeResult(
        "write-outcome-unknown",
        requested,
        observed,
        (
            (
                f"Write did not confirm completion: {error}. "
                "Inspect before retrying; no replay performed."
            ),
        ),
    )


def _unreadable(
    requested: dict[str, object], error: HMCError | ValueError
) -> PersistentMemoryVolumeResult:
    return PersistentMemoryVolumeResult(
        "readback-unavailable",
        requested,
        None,
        (f"Write returned successfully but readback failed: {error}",),
    )


async def list_persistent_memory_volumes(
    hmc: HMCClient, system_name_or_uuid: str, lpar_name_or_uuid: str | None = None
) -> list[PersistentMemoryVolume]:
    """Every volume on a system, or only those assigned to one partition."""
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if lpar_name_or_uuid is None:
        return await _volumes(hmc, system_uuid)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
    )
    partition = await _partition(hmc, lpar_uuid)
    volumes = await _volumes(hmc, system_uuid, partition.partition_id)
    foreign = [v.uuid for v in volumes if v.partition_id != partition.partition_id]
    if foreign:
        raise ValueError(
            f"The HMC answered the partition ID {partition.partition_id} query with "
            f"volumes assigned elsewhere: {', '.join(foreign)}"
        )
    return volumes


async def get_persistent_memory_volume(
    hmc: HMCClient, system_name_or_uuid: str, volume: str
) -> PersistentMemoryVolume:
    """One volume by UUID, or by exact name through the system collection."""
    _require_str("volume", volume)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if not is_uuid(volume):
        return _select(await _volumes(hmc, system_uuid), volume)
    entry = await hmc.get_persistent_memory_volume(system_uuid, volume)
    if entry is None:
        raise ResourceNotFoundError(
            "persistent-memory volume",
            volume,
            f"No persistent-memory volume {volume!r} on the selected system.",
        )
    return parse_volume(entry)


def _create_status(
    requested: dict[str, object],
    matches: list[PersistentMemoryVolume],
    partition_id: int,
) -> PersistentMemoryVolumeResult:
    if len(matches) != 1:
        return PersistentMemoryVolumeResult(
            "mismatch",
            requested,
            None,
            (
                f"Readback found {len(matches)} volumes with the requested name.",
                _PENDING,
            ),
        )
    observed = matches[0]
    differences = [
        label
        for label, wanted, actual in (
            ("partition", partition_id, observed.partition_id),
            ("size_mib", requested["size_mib"], observed.size_mib),
            ("affinity", requested["affinity"], observed.affinity),
        )
        if wanted != actual
    ]
    if differences:
        return PersistentMemoryVolumeResult(
            "mismatch",
            requested,
            observed,
            (
                f"Readback differs in: {', '.join(differences)}; inspect before retrying",
            ),
        )
    return PersistentMemoryVolumeResult("verified", requested, observed, ())


async def create_persistent_memory_volume(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    name: str,
    size_mib: int,
    *,
    affinity: bool = False,
    ownership_override: bool = False,
) -> PersistentMemoryVolumeResult:
    """Create a DRAM volume assigned to an inactive AIX/Linux partition."""
    _require_bools(affinity=affinity, ownership_override=ownership_override)
    if type(size_mib) is not int or size_mib <= 0:
        raise ValueError(f"size_mib must be a positive integer; {_UNCHANGED}")
    validate_volume_name(name)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        system_name_or_uuid=system_uuid,
        ownership_override=ownership_override,
    )
    block = await _system_block_size(hmc, system_uuid)
    if size_mib % block:
        raise ValueError(
            f"size_mib {size_mib} is not a multiple of the system's DRAM volume "
            f"block size {block}; {_UNCHANGED}"
        )
    partition = await _partition(hmc, lpar_uuid)
    _admit_partition_for_create(partition)
    _require_unused(await _volumes(hmc, system_uuid), name)
    requested: dict[str, object] = {
        "name": name,
        "size_mib": size_mib,
        "affinity": affinity,
        "partition_uuid": lpar_uuid,
    }
    document = build_volume_document(
        name, size_mib, affinity=affinity, partition_id=partition.partition_id
    )
    try:
        await hmc.create_persistent_memory_volume(system_uuid, document)
    except HMCError as error:
        try:
            matches = await _readback_by_name(hmc, system_uuid, name)
        except (HMCError, ValueError):
            matches = []
        return _unknown_outcome(requested, error, matches[0] if matches else None)
    try:
        matches = await _readback_by_name(hmc, system_uuid, name)
    except (HMCError, ValueError) as error:
        return _unreadable(requested, error)
    return _create_status(requested, matches, partition.partition_id)


async def _authorized_assigned_volume(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    volume: str,
    ownership_override: bool,
) -> tuple[str, _Partition, PersistentMemoryVolume, list[PersistentMemoryVolume]]:
    _require_str("volume", volume)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        system_name_or_uuid=system_uuid,
        ownership_override=ownership_override,
    )
    partition = await _partition(hmc, lpar_uuid)
    volumes = await _volumes(hmc, system_uuid)
    target = _select(volumes, volume)
    _require_assigned(target, partition)
    return system_uuid, partition, target, volumes


async def rename_persistent_memory_volume(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    volume: str,
    new_name: str,
    *,
    ownership_override: bool = False,
) -> PersistentMemoryVolumeResult:
    """Rename a volume assigned to the named partition."""
    _require_bools(ownership_override=ownership_override)
    validate_volume_name(new_name)
    system_uuid, partition, target, volumes = await _authorized_assigned_volume(
        hmc, system_name_or_uuid, lpar_name_or_uuid, volume, ownership_override
    )
    if target.name == new_name:
        raise ValueError(f"Volume is already named {new_name!r}; {_UNCHANGED}")
    _require_unused(volumes, new_name)
    requested: dict[str, object] = {"uuid": target.uuid, "name": new_name}
    document = build_volume_document(
        new_name,
        target.size_mib,
        affinity=target.affinity,
        partition_id=partition.partition_id,
    )
    try:
        await hmc.update_persistent_memory_volume(system_uuid, target.uuid, document)
    except HMCError as error:
        try:
            observed = await get_persistent_memory_volume(hmc, system_uuid, target.uuid)
        except (HMCError, ValueError):
            observed = None
        return _unknown_outcome(requested, error, observed)
    try:
        observed = await get_persistent_memory_volume(hmc, system_uuid, target.uuid)
    except (HMCError, ValueError) as error:
        return _unreadable(requested, error)
    if observed.name != new_name:
        return PersistentMemoryVolumeResult(
            "mismatch",
            requested,
            observed,
            (f"Readback shows the name {observed.name!r}.", _PENDING),
        )
    return PersistentMemoryVolumeResult("verified", requested, observed, ())


async def delete_persistent_memory_volume(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    volume: str,
    *,
    acknowledge_data_loss: bool = False,
    ownership_override: bool = False,
) -> PersistentMemoryVolumeResult:
    """Delete a volume of an inactive partition; its data is destroyed."""
    _require_bools(
        acknowledge_data_loss=acknowledge_data_loss,
        ownership_override=ownership_override,
    )
    if not acknowledge_data_loss:
        raise ValueError(
            "Deleting a persistent-memory volume destroys its data; set "
            f"acknowledge_data_loss=True to proceed; {_UNCHANGED}"
        )
    system_uuid, partition, target, _ = await _authorized_assigned_volume(
        hmc, system_name_or_uuid, lpar_name_or_uuid, volume, ownership_override
    )
    _require_inactive(partition, "Deleting a volume")
    requested: dict[str, object] = {"uuid": target.uuid, "name": target.name}
    try:
        await hmc.delete_persistent_memory_volume(system_uuid, target.uuid)
    except HMCError as error:
        try:
            remaining = await _volumes(hmc, system_uuid)
        except (HMCError, ValueError):
            remaining = []
        still = [v for v in remaining if v.uuid == target.uuid]
        return _unknown_outcome(requested, error, still[0] if still else None)
    try:
        remaining = await _volumes(hmc, system_uuid)
    except (HMCError, ValueError) as error:
        return _unreadable(requested, error)
    still = [v for v in remaining if v.uuid.casefold() == target.uuid.casefold()]
    if still:
        return PersistentMemoryVolumeResult(
            "mismatch",
            requested,
            still[0],
            ("Readback still lists the volume.", _PENDING),
        )
    return PersistentMemoryVolumeResult("verified", requested, None, ())
