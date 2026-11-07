"""Presentation-neutral fleet utilization survey and roll-ups (ADRs 0184 and 0185).

Allocation is read from partitions' current configurations: the hypervisor reserves
a not-activated partition's current configuration, so runtime figures under-report.
Every figure is ``None`` when the HMC did not report it or a read failed; nothing
defaults to zero.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, fields
from typing import Any, Protocol

from hmcpctl.client.client_resolution import system_operating
from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.systems.fleet import UnreadableSystem, unreadable_system
from hmcpctl.xmlutil import leaf_text, mtms_parts

_logger = logging.getLogger(__name__)

_NOT_ACTIVATED = "not activated"
_RUNNING = "running"
_SYSTEM_MEMORY = "AssociatedSystemMemoryConfiguration"
_SYSTEM_PROCESSORS = "AssociatedSystemProcessorConfiguration"
_MEMORY = "PartitionMemoryConfiguration"
_PROCESSORS = "PartitionProcessorConfiguration"
_VOLUMES = "PhysicalVolumes"
_SLOTS = "IOSlots"
_SRIOV_ADAPTERS = "SRIOVAdapters"
_UNCONFIGURED_PORTS = "UnconfiguredLogicalPorts"


class SurveyClient(Protocol):
    """The HMC reads a survey makes; ``HMCClient`` satisfies it."""

    async def list_system_summaries(self) -> list[Any]: ...

    async def get_managed_system(self, uuid: str) -> dict[str, Any] | None: ...

    async def list_logical_partitions(
        self, system_uuid: str
    ) -> list[dict[str, Any]]: ...

    async def list_vios(self, system_uuid: str) -> list[dict[str, Any]]: ...

    async def list_child(
        self, parent_type: str, parent_uuid: str, child_type: str
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CpuFigures:
    """Processor units of one system, or their sum over a roll-up."""

    installed: float | None
    configurable: float | None
    vios: float | None
    client_active: float | None
    idle_reserved: float | None
    other_reserved: float | None
    free: float | None
    dedicated: float | None
    shared: float | None


@dataclass(frozen=True)
class MemoryFigures:
    """Memory in MiB of one system, or its sum over a roll-up."""

    installed: int | None
    configurable: int | None
    hypervisor: int | None
    vios: int | None
    client_active: int | None
    idle_reserved: int | None
    other_reserved: int | None
    free: int | None


@dataclass(frozen=True)
class PartitionFigures:
    """Client partition counts by state, and never-applied partitions' claims."""

    running: int | None
    not_activated: int | None
    other: int | None
    profile_claims: int | None
    profile_claim_memory: int | None
    profile_claim_units: float | None


@dataclass(frozen=True)
class DiskFigures:
    """VIOS physical-volume capacity in MiB, each volume counted once per system."""

    internal_total: int | None
    internal_assigned: int | None
    internal_free: int | None
    san_total: int | None
    san_assigned: int | None
    san_free: int | None


@dataclass(frozen=True)
class AdapterFigures:
    """I/O slot occupancy and SR-IOV logical-port capacity of one system."""

    slots_assigned: int | None
    slots_unassigned: int | None
    slots_sriov: int | None
    slots_empty: int | None
    sriov_adapters: int | None
    sriov_logical_ports: int | None
    sriov_logical_ports_free: int | None


#: Every figure group a reading and a roll-up carry, by attribute name.
FIGURE_GROUPS: dict[str, type] = {
    "cpu": CpuFigures,
    "memory": MemoryFigures,
    "partitions": PartitionFigures,
    "disk": DiskFigures,
    "adapters": AdapterFigures,
}


@dataclass(frozen=True)
class SystemReading:
    """One managed system as one profile's HMC reported it."""

    profile: str
    name: str
    machine_type: str | None
    model: str | None
    serial: str | None
    firmware: str | None
    state: str | None
    cpu: CpuFigures
    memory: MemoryFigures
    partitions: PartitionFigures
    disk: DiskFigures
    adapters: AdapterFigures
    shared_pools: tuple[int, ...]
    gaps: tuple[str, ...]

    @property
    def unknown_figures(self) -> int:
        """How many figures of every group are unknown."""
        return sum(
            getattr(group, item.name) is None
            for group in (getattr(self, name) for name in FIGURE_GROUPS)
            for item in fields(group)
        )


@dataclass(frozen=True)
class ProfileFailure:
    """A profile that could not be surveyed, and why."""

    profile: str
    reason: str


@dataclass(frozen=True)
class FleetSurvey:
    """Every surveyed profile's readings, and the profiles that failed."""

    profiles: tuple[str, ...]
    readings: tuple[SystemReading, ...]
    failures: tuple[ProfileFailure, ...]


@dataclass(frozen=True)
class FleetSystem:
    """One physical system: its chosen reading and every profile managing it."""

    reading: SystemReading
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class Rollup:
    """Per-figure sums over ``systems`` readings, and each figure's shortfall.

    ``unknown`` holds ``(group, figure, count)`` for every figure ``count`` of the
    readings lack; ``group`` is a ``FIGURE_GROUPS`` name.
    """

    systems: int
    cpu: CpuFigures
    memory: MemoryFigures
    partitions: PartitionFigures
    disk: DiskFigures
    adapters: AdapterFigures
    cpu_allocated: float | None
    cpu_util_pct: float | None
    mem_allocated: int | None
    mem_util_pct: float | None
    disk_util_pct: float | None
    slots_util_pct: float | None
    sriov_util_pct: float | None
    unknown: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class _Size:
    memory: int | None
    units: float | None
    dedicated: bool | None
    pool: int | None


def _leaf(container: object, *path: str) -> str | None:
    value = container
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    text = leaf_text(value)
    return text.strip() if isinstance(text, str) else None


def _present(container: object, name: str) -> bool:
    """Whether *container* has element *name*; a self-closed one parses as ``''``."""
    return isinstance(container, dict) and container.get(name) is not None


def _items(container: object, name: str) -> list[dict[str, Any]]:
    value = container.get(name) if isinstance(container, dict) else None
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, dict)]


def _flag(container: object, name: str) -> bool | None:
    return {"true": True, "false": False}.get(_leaf(container, name) or "")


def _int(text: str | None) -> int | None:
    try:
        return None if text is None else int(text)
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _total(values: Iterable[Any]) -> Any:
    total: Any = 0
    for value in values:
        if value is None:
            return None
        total += value
    return round(total, 4) if isinstance(total, float) else total


def _less(first: Any, *rest: Any) -> Any:
    taken = _total(rest)
    if first is None or taken is None:
        return None
    left = first - taken
    return round(left, 4) if isinstance(left, float) else left


def _current_size(resource: dict[str, Any]) -> _Size:
    memory = _int(_leaf(resource, _MEMORY, "CurrentMemory"))
    flag = _leaf(resource, _PROCESSORS, "CurrentHasDedicatedProcessors")
    if flag == "true":
        dedicated = (_PROCESSORS, "CurrentDedicatedProcessorConfiguration")
        units = _float(_leaf(resource, *dedicated, "CurrentProcessors"))
        return _Size(memory, units, True, None)
    if flag == "false":
        shared = (_PROCESSORS, "CurrentSharedProcessorConfiguration")
        units = _float(_leaf(resource, *shared, "CurrentProcessingUnits"))
        pool = _int(_leaf(resource, *shared, "CurrentSharedProcessorPoolID"))
        return _Size(memory, units, False, pool)
    return _Size(memory, None, None, None)


def _add(sums: dict[str, Any], key: str, value: int | None) -> None:
    sums[key] = None if sums[key] is None or value is None else sums[key] + value


def _backing(volume: dict[str, Any]) -> str | None:
    # IsISCSIBacked exists only from schema V1_8_0 (its ksv), so its absence is "false".
    iscsi = _flag(volume, "IsISCSIBacked") if "IsISCSIBacked" in volume else False
    flags = [_flag(volume, "IsFibreChannelBacked"), iscsi]
    if True in flags:
        return "san"
    if None in flags:
        return None
    return "internal"


def _volume_state(listings: list[dict[str, Any]]) -> str | None:
    """Assigned when any VIOS listing the volume reports it unavailable for use."""
    available = [_flag(listing, "AvailableForUsage") for listing in listings]
    if False in available:
        return "assigned"
    if None in available:
        return None
    return "free"


def _sum_volumes(volumes: Iterable[list[dict[str, Any]]]) -> DiskFigures:
    sums: dict[str, Any] = {item.name: 0 for item in fields(DiskFigures)}
    for listings in volumes:
        kind = _backing(listings[0])
        if kind is None:
            return DiskFigures(None, None, None, None, None, None)
        capacity = _int(_leaf(listings[0], "VolumeCapacity"))
        state = _volume_state(listings)
        _add(sums, f"{kind}_total", capacity)
        for split in ("assigned", "free"):
            if state is None:
                _add(sums, f"{kind}_{split}", None)
            else:
                _add(sums, f"{kind}_{split}", capacity if state == split else 0)
    return DiskFigures(**sums)


def _unreadable(fields: dict[str, object]) -> str:
    return ", ".join(name for name, value in fields.items() if value is None)


def _disk_figures(vios: list[dict[str, Any]] | None, gaps: list[str]) -> DiskFigures:
    """Sum VIOS physical volumes per ADR 0185 decision 4, each volume once."""
    unknown = DiskFigures(None, None, None, None, None, None)
    if vios is None:
        return unknown
    resources = [entry.get("Resource") or {} for entry in vios]
    silent = [resource for resource in resources if not _present(resource, _VOLUMES)]
    for resource in silent:
        gaps.append(
            f"{_VOLUMES}: VIOS {_leaf(resource, 'PartitionName')} "
            f"({_leaf(resource, 'PartitionState')}) reported no storage"
        )
    if silent:
        return unknown
    volumes: dict[object, list[dict[str, Any]]] = {}
    for index, resource in enumerate(resources):
        vios_name = _leaf(resource, "PartitionName")
        for position, volume in enumerate(_items(resource[_VOLUMES], "PhysicalVolume")):
            key: object = _leaf(volume, "UniqueDeviceID") or None
            if key is None:
                key = (index, position)
                gaps.append(
                    f"{_VOLUMES}: VIOS {vios_name} lists a volume without "
                    "UniqueDeviceID; it is not deduplicated"
                )
            unreadable = _unreadable(
                {
                    "VolumeCapacity": _int(_leaf(volume, "VolumeCapacity")),
                    "AvailableForUsage": _flag(volume, "AvailableForUsage"),
                    "IsFibreChannelBacked/IsISCSIBacked": _backing(volume),
                }
            )
            if unreadable:
                gaps.append(
                    f"{_VOLUMES}: VIOS {vios_name} volume "
                    f"{_leaf(volume, 'VolumeName')} has no readable {unreadable}"
                )
            volumes.setdefault(key, []).append(volume)
    return _sum_volumes(volumes.values())


def _free_ports(adapter: dict[str, Any], capacity: int | None) -> int | None:
    if _present(adapter, _UNCONFIGURED_PORTS):
        ports = _items(adapter[_UNCONFIGURED_PORTS], "SRIOVUnconfiguredLogicalPort")
        return len(ports)
    return 0 if capacity == 0 else None


def _slot_kind(slot: dict[str, Any], sriov_slots: set[str | None] | None) -> str | None:
    if _leaf(slot, "Description") == "Empty slot":
        return "empty"
    if _leaf(slot, "PartitionID") is not None:
        return "assigned"
    if sriov_slots is None:
        return None
    drc = _leaf(slot, "SlotDynamicReconfigurationConnectorIndex")
    return "sriov" if drc in sriov_slots else "unassigned"


def _adapter_figures(resource: dict[str, Any], gaps: list[str]) -> AdapterFigures:
    """Classify I/O slots and count SR-IOV ports per ADR 0185 decisions 2 and 3."""
    io = resource.get("AssociatedSystemIOConfiguration")
    io = io if isinstance(io, dict) else {}
    sriov_slots: set[str | None] | None = None
    sriov: tuple[int | None, int | None, int | None] = (None, None, None)
    if _present(io, _SRIOV_ADAPTERS):
        adapters = [
            adapter
            for choice in _items(io[_SRIOV_ADAPTERS], "IOAdapterChoice")
            for adapter in _items(choice, "SRIOVAdapter")
            if _leaf(adapter, "AdapterMode") == "Sriov"
        ]
        sriov_slots = {_leaf(adapter, "AdapterID") for adapter in adapters}
        ports = [_int(_leaf(a, "MaximumLogicalPortsSupported")) for a in adapters]
        free = [_free_ports(a, p) for a, p in zip(adapters, ports, strict=True)]
        for adapter, limit, spare in zip(adapters, ports, free, strict=True):
            unreadable = _unreadable(
                {"MaximumLogicalPortsSupported": limit, _UNCONFIGURED_PORTS: spare}
            )
            if unreadable:
                gaps.append(
                    f"{_SRIOV_ADAPTERS}: adapter {_leaf(adapter, 'AdapterID')} has no "
                    f"readable {unreadable}"
                )
        sriov = (len(adapters), _total(ports), _total(free))
    else:
        gaps.append(f"{_SRIOV_ADAPTERS}: the system reported no SR-IOV adapter list")
    if not _present(io, _SLOTS):
        gaps.append(f"{_SLOTS}: the system reported no I/O slot list")
        return AdapterFigures(None, None, None, None, *sriov)
    kinds = [_slot_kind(slot, sriov_slots) for slot in _items(io[_SLOTS], "IOSlot")]

    def count(kind: str) -> int | None:
        if None in kinds and kind in ("sriov", "unassigned"):
            return None
        return kinds.count(kind)

    return AdapterFigures(
        count("assigned"), count("unassigned"), count("sriov"), count("empty"), *sriov
    )


def _never_applied(state: str | None, size: _Size) -> bool:
    return state == _NOT_ACTIVATED and size.memory == 0 and size.units == 0


async def _read_feed(
    gaps: list[str],
    label: str,
    read: Callable[[str], Any],
    uuid: object,
) -> list[dict[str, Any]] | None:
    if not isinstance(uuid, str):
        gaps.append(f"{label} feed: the system has no UUID")
        return None
    try:
        return await read(uuid)
    except HMCTLSFingerprintError:
        raise
    except HMCError as exc:
        gaps.append(f"{label} feed: {exc}")
        return None


async def _profile_claim(
    hmc: SurveyClient, partition: dict[str, Any], gaps: list[str]
) -> tuple[int | None, float | None]:
    uuid = partition.get("UUID")
    link = (partition.get("Resource") or {}).get("AssociatedPartitionProfile")
    href = link.get("href") if isinstance(link, dict) else None
    if not isinstance(uuid, str) or not isinstance(href, str):
        gaps.append(
            f"LogicalPartitionProfile: partition {uuid} has no "
            "AssociatedPartitionProfile link"
        )
        return None, None
    wanted = href.rstrip("/").rsplit("/", 1)[-1].casefold()
    for profile in await hmc.list_child(
        "LogicalPartition", uuid, "LogicalPartitionProfile"
    ):
        if str(profile.get("UUID") or "").casefold() != wanted:
            continue
        resource = profile.get("Resource") or {}
        memory = _int(_leaf(resource, "ProfileMemory", "DesiredMemory"))
        flag = _leaf(resource, "ProcessorAttributes", "HasDedicatedProcessors")
        if flag == "true":
            path = ("DedicatedProcessorConfiguration", "DesiredProcessors")
        elif flag == "false":
            path = ("SharedProcessorConfiguration", "DesiredProcessingUnits")
        else:
            gaps.append(
                f"LogicalPartitionProfile: the profile of partition {uuid} has no "
                "HasDedicatedProcessors"
            )
            return memory, None
        return memory, _float(_leaf(resource, "ProcessorAttributes", *path))
    gaps.append(
        f"LogicalPartitionProfile: partition {uuid}'s linked profile is not in its feed"
    )
    return None, None


async def _partition_figures(
    hmc: SurveyClient,
    gaps: list[str],
    lpars: list[dict[str, Any]] | None,
) -> tuple[PartitionFigures, list[tuple[str | None, _Size]]]:
    if lpars is None:
        unknown = PartitionFigures(None, None, None, None, None, None)
        return unknown, []
    states = [
        (
            (lpar.get("Resource") or {}).get("PartitionState"),
            _current_size(lpar.get("Resource") or {}),
        )
        for lpar in lpars
    ]
    claims: list[tuple[int | None, float | None]] = []
    for lpar, (state, size) in zip(lpars, states, strict=True):
        if not _never_applied(state, size):
            continue
        try:
            claims.append(await _profile_claim(hmc, lpar, gaps))
        except HMCTLSFingerprintError:
            raise
        except HMCError as exc:
            gaps.append(f"LogicalPartitionProfile feed: {exc}")
            claims.append((None, None))
    running = sum(state == _RUNNING for state, _ in states)
    not_activated = sum(state == _NOT_ACTIVATED for state, _ in states)
    figures = PartitionFigures(
        running=running,
        not_activated=not_activated,
        other=len(states) - running - not_activated,
        profile_claims=len(claims),
        profile_claim_memory=_total(memory for memory, _ in claims),
        profile_claim_units=_total(units for _, units in claims),
    )
    return figures, states


def _split(
    states: list[tuple[str | None, _Size]], lpars_known: bool
) -> tuple[list[_Size], list[_Size]]:
    if not lpars_known:
        return [], []
    active = [size for state, size in states if state != _NOT_ACTIVATED]
    idle = [
        size
        for state, size in states
        if state == _NOT_ACTIVATED and not _never_applied(state, size)
    ]
    return active, idle


def _sum_or_unknown(sizes: list[_Size], known: bool, attribute: str) -> Any:
    return _total(getattr(size, attribute) for size in sizes) if known else None


async def read_system(
    hmc: SurveyClient, profile: str, system: dict[str, Any]
) -> SystemReading:
    """Read one managed system's allocation per ADR 0184."""
    resource = system.get("Resource") or {}
    uuid = system.get("UUID")
    gaps: list[str] = []
    lpars = await _read_feed(
        gaps, "LogicalPartition", hmc.list_logical_partitions, uuid
    )
    vios = await _read_feed(gaps, "VirtualIOServer", hmc.list_vios, uuid)
    partitions, states = await _partition_figures(hmc, gaps, lpars)
    active, idle = _split(states, lpars is not None)
    vios_sizes = [_current_size(entry.get("Resource") or {}) for entry in vios or []]
    every = [size for _, size in states] + vios_sizes
    both = lpars is not None and vios is not None
    flags_known = both and all(size.dedicated is not None for size in every)

    memory_configurable = _int(
        _leaf(resource, _SYSTEM_MEMORY, "ConfigurableSystemMemory")
    )
    memory_free = _int(_leaf(resource, _SYSTEM_MEMORY, "CurrentAvailableSystemMemory"))
    hypervisor = _int(_leaf(resource, _SYSTEM_MEMORY, "MemoryUsedByHypervisor"))
    assigned = _int(
        _leaf(resource, _SYSTEM_MEMORY, "CurrentAssignedMemoryToPartitions")
    )
    memory = MemoryFigures(
        installed=_int(_leaf(resource, _SYSTEM_MEMORY, "InstalledSystemMemory")),
        configurable=memory_configurable,
        hypervisor=hypervisor,
        vios=_sum_or_unknown(vios_sizes, vios is not None, "memory"),
        client_active=_sum_or_unknown(active, lpars is not None, "memory"),
        idle_reserved=_sum_or_unknown(idle, lpars is not None, "memory"),
        other_reserved=_less(memory_configurable, memory_free, hypervisor, assigned),
        free=memory_free,
    )

    units_configurable = _float(
        _leaf(resource, _SYSTEM_PROCESSORS, "ConfigurableSystemProcessorUnits")
    )
    units_free = _float(
        _leaf(resource, _SYSTEM_PROCESSORS, "CurrentAvailableSystemProcessorUnits")
    )
    vios_units = _sum_or_unknown(vios_sizes, vios is not None, "units")
    active_units = _sum_or_unknown(active, lpars is not None, "units")
    idle_units = _sum_or_unknown(idle, lpars is not None, "units")
    cpu = CpuFigures(
        installed=_float(
            _leaf(resource, _SYSTEM_PROCESSORS, "InstalledSystemProcessorUnits")
        ),
        configurable=units_configurable,
        vios=vios_units,
        client_active=active_units,
        idle_reserved=idle_units,
        other_reserved=_less(
            units_configurable, units_free, vios_units, active_units, idle_units
        ),
        free=units_free,
        dedicated=_sum_or_unknown(
            [size for size in every if size.dedicated], flags_known, "units"
        ),
        shared=_sum_or_unknown(
            [size for size in every if size.dedicated is False], flags_known, "units"
        ),
    )
    pools = sorted(
        {
            size.pool
            for size in every
            if size.dedicated is False and size.pool is not None and size.units
        }
    )
    disk = _disk_figures(vios, gaps)
    adapters = _adapter_figures(resource, gaps)
    mtms = mtms_parts(resource)
    return SystemReading(
        profile=profile,
        name=_leaf(resource, "SystemName") or str(uuid or "unknown system"),
        machine_type=mtms[0] if mtms else None,
        model=mtms[1] if mtms else None,
        serial=mtms[2] if mtms else None,
        firmware=_leaf(resource, "SystemFirmware"),
        state=_leaf(resource, "State"),
        cpu=cpu,
        memory=memory,
        partitions=partitions,
        disk=disk,
        adapters=adapters,
        shared_pools=tuple(pools),
        gaps=tuple(gaps),
    )


def _all_unknown(kind: type[Any]) -> Any:
    return kind(**dict.fromkeys(item.name for item in fields(kind)))


def _summary_mtms(summary: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(type, model, serial) from quick/All's ``MTMS``, captured on V11R2 as ``TTTT-MMM*SERIAL``."""
    mtms = summary.get("MTMS")
    match = (
        re.fullmatch(r"([^-*\s]+)-([^*\s]+)\*(\S+)", mtms)
        if isinstance(mtms, str)
        else None
    )
    if match is None:
        return None, None, None
    machine_type, model, serial = match.groups()
    return machine_type, model, serial


def _unread_system(
    profile: str, summary: object, skipped: UnreadableSystem
) -> SystemReading:
    """A system quick/All lists but that is not read, from its summary alone.

    The machine type, model and serial keep a frame that one HMC can read and
    another cannot counted once (ADR 0184).
    """
    fields_ = summary if isinstance(summary, dict) else {}
    machine_type, model, serial = _summary_mtms(fields_)
    firmware = fields_.get("SystemFirmware")
    if skipped.state is None:
        reason = "quick/All reports no State"
    elif system_operating(skipped.state):
        reason = "quick/All reports no valid UUID"
    else:
        reason = f"the system is in State {skipped.state!r}"
    unknown = {name: _all_unknown(kind) for name, kind in FIGURE_GROUPS.items()}
    return SystemReading(
        profile=profile,
        name=skipped.system_name or skipped.system_uuid or "unknown system",
        machine_type=machine_type,
        model=model,
        serial=serial,
        firmware=firmware if isinstance(firmware, str) and firmware.strip() else None,
        state=skipped.state,
        shared_pools=(),
        gaps=(f"ManagedSystem: not read, because {reason}",),
        **unknown,
    )


async def survey_hmc(hmc: SurveyClient, profile: str) -> list[SystemReading]:
    """Read every managed system one profile's HMC reports, one at a time.

    The systems come from ``ManagedSystem/quick/All``: the full ``ManagedSystem``
    feed, and a non-operating system's own entry, time out while a system has no
    connection (#1501). A system that is not operating is not read; its reading
    carries every figure unknown and a gap naming its ``State``.
    """
    readings = []
    for summary in await hmc.list_system_summaries():
        skipped = unreadable_system(summary)
        if skipped is not None:
            readings.append(_unread_system(profile, summary, skipped))
            continue
        uuid = summary["UUID"]
        system = await hmc.get_managed_system(uuid)
        if system is None:
            raise HMCError(f"Managed system {uuid} listed by quick/All has no entry")
        readings.append(await read_system(hmc, profile, system))
    return readings


async def survey_fleet(
    profiles: Sequence[str],
    open_client: Callable[[str], AbstractAsyncContextManager[SurveyClient]],
    *,
    concurrency: int = 4,
    hmc_timeout: float = 300.0,
) -> FleetSurvey:
    """Survey *profiles*, at most *concurrency* at once, *hmc_timeout* s each.

    A profile whose client, logon, reads or deadline fail becomes a
    ``ProfileFailure`` and contributes no readings. A failure closing the
    session after every read finished keeps the readings and logs a warning.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    if not math.isfinite(hmc_timeout) or hmc_timeout <= 0:
        raise ValueError(f"hmc_timeout must be positive, got {hmc_timeout}")
    names = tuple(dict.fromkeys(profiles))
    gate = asyncio.Semaphore(concurrency)

    async def survey(profile: str) -> list[SystemReading] | ProfileFailure:
        readings: list[SystemReading] | None = None
        async with gate:
            try:
                async with asyncio.timeout(hmc_timeout):
                    async with open_client(profile) as hmc:
                        readings = await survey_hmc(hmc, profile)
            except TimeoutError:
                failure = f"no answer within {hmc_timeout:g} s"
            except Exception as exc:  # noqa: BLE001 - every profile failure is reported as a row, never dropped (ADR 0184)
                failure = f"{type(exc).__name__}: {exc}"
            else:
                return readings
        if readings is None:
            return ProfileFailure(profile, failure)
        _logger.warning(
            "%s: every read finished, but closing the session failed: %s",
            profile,
            failure,
        )
        return readings

    results = await asyncio.gather(*(survey(name) for name in names))
    readings = tuple(
        reading for result in results if isinstance(result, list) for reading in result
    )
    failures = tuple(result for result in results if isinstance(result, ProfileFailure))
    return FleetSurvey(names, readings, failures)


def fleet_systems(readings: Iterable[SystemReading]) -> tuple[FleetSystem, ...]:
    """Count each machine type-model-serial once, keeping its most complete reading."""
    groups: dict[tuple[object, ...], list[SystemReading]] = {}
    for index, reading in enumerate(readings):
        key: tuple[object, ...] = (
            ("mtms", reading.machine_type, reading.model, reading.serial)
            if reading.serial is not None
            else ("reading", index)
        )
        groups.setdefault(key, []).append(reading)
    systems = [
        FleetSystem(
            reading=min(group, key=lambda item: (item.unknown_figures, item.profile)),
            profiles=tuple(sorted({item.profile for item in group})),
        )
        for group in groups.values()
    ]
    return tuple(
        sorted(systems, key=lambda item: (item.reading.name, item.reading.serial or ""))
    )


def _sum_figures(
    label: str, kind: type[Any], groups: list[Any]
) -> tuple[Any, list[tuple[str, str, int]]]:
    sums: dict[str, Any] = {}
    unknown: list[tuple[str, str, int]] = []
    for item in fields(kind):
        values = [getattr(group, item.name) for group in groups]
        known = [value for value in values if value is not None]
        sums[item.name] = _total(known) if known or not values else None
        if len(known) < len(values):
            unknown.append((label, item.name, len(values) - len(known)))
    return kind(**sums), unknown


def _pooled(pairs: Iterable[tuple[Any, Any]]) -> tuple[Any, float | None]:
    """Allocated capacity and its percentage over the readings reporting both figures."""
    every = list(pairs)
    known = [(total, free) for total, free in every if None not in (total, free)]
    if not every:
        return 0, None
    if not known:
        return None, None
    total = _total(configurable for configurable, _ in known)
    free = _total(available for _, available in known)
    return allocated(total, free), utilization_pct(total, free)


def capacity_pairs(reading: SystemReading) -> dict[str, tuple[Any, Any]]:
    """Each utilization's (capacity, free) pair: cpu, mem, disk, slots and sriov."""
    disk, adapters = reading.disk, reading.adapters
    occupied = [
        adapters.slots_assigned,
        adapters.slots_unassigned,
        adapters.slots_sriov,
    ]
    return {
        "cpu": (reading.cpu.configurable, reading.cpu.free),
        "mem": (reading.memory.configurable, reading.memory.free),
        "disk": (
            _total([disk.internal_total, disk.san_total]),
            _total([disk.internal_free, disk.san_free]),
        ),
        "slots": (_total(occupied), adapters.slots_unassigned),
        "sriov": (adapters.sriov_logical_ports, adapters.sriov_logical_ports_free),
    }


def rollup(readings: Iterable[SystemReading]) -> Rollup:
    """Sum each figure over the readings that reported it (ADR 0184 decision 4)."""
    every = tuple(readings)
    sums: dict[str, Any] = {}
    unknown: list[tuple[str, str, int]] = []
    for group, kind in FIGURE_GROUPS.items():
        sums[group], missing = _sum_figures(
            group, kind, [getattr(reading, group) for reading in every]
        )
        unknown.extend(missing)
    pairs = [capacity_pairs(reading) for reading in every]
    pooled = {
        name: _pooled(pair[name] for pair in pairs)
        for name in ("cpu", "mem", "disk", "slots", "sriov")
    }
    return Rollup(
        systems=len(every),
        **sums,
        cpu_allocated=pooled["cpu"][0],
        cpu_util_pct=pooled["cpu"][1],
        mem_allocated=pooled["mem"][0],
        mem_util_pct=pooled["mem"][1],
        disk_util_pct=pooled["disk"][1],
        slots_util_pct=pooled["slots"][1],
        sriov_util_pct=pooled["sriov"][1],
        unknown=tuple(unknown),
    )


def allocated(configurable: Any, free: Any) -> Any:
    """Configurable minus free capacity, or ``None`` when either is unknown."""
    return _less(configurable, free)


def utilization_pct(configurable: float | None, free: float | None) -> float | None:
    """Allocated share of configurable capacity, in percent to one decimal."""
    if configurable is None or free is None or configurable == 0:
        return None
    return round(100 * (configurable - free) / configurable, 1)
