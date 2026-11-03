"""``hmc_reconfigure_lpar``'s planner, operation body and classifiers (ADRs 0314, 0322).

The planner turns one partition read and one profile read into a JSON plan that names
every change and where it lands (``live``, ``profile``, ``pending_activation``) before
anything is written. The body records that plan as an effect, then replays the writes
it names: the default profile, the partition's current configuration, the rename, disk
detaches and adds; after verification an add force-saves the default profile.
Messages name partitions and profiles by UUID and never echo a caller's selector
(``engine`` module note).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from ...client.core import HMCClient
from ...documents.lpar import LparResources, partition_updates
from ...documents.partition_profile import ProfilePatch
from ...documents.storage import validate_virtual_disk
from ...errors import HMCError, LparUpdateError
from ...resource_identity import resolve_system_uuid, resolve_vios_uuid
from ...xmlutil import console_version, escape_xml, leaf_text
from ..logical import engine, profile_write, store
from ..logical.engine import (
    BodyResult,
    Classification,
    EffectNotApplied,
    OperationContext,
    OperationFailed,
)
from ..logical.models import EffectRecord
from ..storage.resources import storage_mapping, volume_group
from ..systems.fleet import read_system_entry
from . import profile_persist
from .memory_bound import require_memory_within_selected_system
from .ownership import resolve_and_authorize_lpar_mutation
from .power import open_client as _power_client
from .profiles import read_partition_profile, update_partition_profile
from .provision import PROFILE_JOB_MINIMUM, virtual_disk_names
from .provision_operation import DISK_LEDGER, _ledgered_elsewhere

# ADR 0314 Decision 6: the specialists whose authority each patch field uses.
DELEGATED: Mapping[str, tuple[str, ...]] = {
    "name": ("hmc_modify_lpar", "hmc_rename_lpar"),
    "memory_mib": ("hmc_dlpar_mem", "hmc_update_partition_profile"),
    "processors": ("hmc_dlpar_proc", "hmc_update_partition_profile"),
}
# ADR 0322 Decision 6: the specialists a disk add or detach uses, authorized per VIOS.
DISK_ADD_TOOLS = (
    "hmc_create_virtual_disk",
    "hmc_map_storage_to_lpar",
    "hmc_save_current_partition_profile",
)
DISK_DETACH_TOOLS = ("hmc_detach_storage_mapping",)
# Persisted contract (ADR 0195 Consequences): never change while an operation can be open.
PLAN_KEY, PLAN_KIND = "plan", "lpar.reconfigure_plan"
PROFILE_KEY = "profile"
CURRENT_KEY, CURRENT_KIND = "current", "lpar.current_config"
RENAME_KEY, RENAME_KIND = "rename", "lpar.rename"
UNMAP_KIND, DISK_KIND, MAP_KIND = (
    "vios.storage_unmap",
    "vios.virtual_disk",
    "vios.storage_map",
)
SAVE_KEY = "profile_save"
MAX_DISKS = 4
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
SETTLE_SECONDS = 120
POLL_SECONDS = 5
_NOT_ACTIVATED = "not activated"
_SYNC_OFF = frozenset({"disabled", "suspended"})
_RESOURCES = ("memory", "processors")
_FIELD = {"memory": "memory_mib", "processors": "processors"}
_LEVELS = ("min", "desired", "max")
Write = Callable[[], Awaitable[Mapping[str, Any]]]


class DiskAdd(BaseModel):
    """A virtual disk to create in a volume group and map to the partition."""

    model_config = ConfigDict(extra="forbid")
    vios_uuid: StrictStr = Field(pattern=_UUID, description="UUID of the VIOS.")
    vg_uuid: StrictStr = Field(
        pattern=_UUID, description="UUID of the volume group on that VIOS."
    )
    name: StrictStr = Field(
        min_length=1,
        description="New virtual disk name; a name the VIOS already has is refused, "
        "so no disk is ever resized.",
    )
    capacity_mib: StrictInt = Field(
        description="Size in MiB, a positive multiple of 1024."
    )

    @model_validator(mode="after")
    def _creatable(self) -> DiskAdd:
        if not self.name.strip():
            raise ValueError("disks.add name must not be blank")
        validate_virtual_disk(self.name, self.capacity_mib)
        return self


class DiskDetach(BaseModel):
    """A virtual disk whose one mapping to the partition is removed; the disk stays."""

    model_config = ConfigDict(extra="forbid")
    vios_uuid: StrictStr = Field(pattern=_UUID, description="UUID of the VIOS.")
    name: StrictStr = Field(min_length=1, description="The mapped virtual disk's name.")


class DiskPatch(BaseModel):
    """Disks to add and to detach (ADR 0322 Decision 1)."""

    model_config = ConfigDict(extra="forbid")
    add: list[DiskAdd] = Field(default_factory=list, max_length=MAX_DISKS)
    detach: list[DiskDetach] = Field(default_factory=list, max_length=MAX_DISKS)

    @model_validator(mode="after")
    def _distinct(self) -> DiskPatch:
        if not self.add and not self.detach:
            raise ValueError("disks needs at least one add or detach")
        pairs = [(d.vios_uuid.lower(), d.name) for d in (*self.add, *self.detach)]
        if len(set(pairs)) != len(pairs):
            raise ValueError("disks names the same VIOS and disk name twice")
        return self


class ReconfigurePatch(BaseModel):
    """What to change; an omitted field is never read into a write (ADR 0314 Decision 1)."""

    model_config = ConfigDict(extra="forbid")
    # Bounded for the recorded plan; reconfigure_body still checks its encoded size.
    name: StrictStr | None = Field(
        default=None,
        max_length=256,
        description="New partition name; profile names never change.",
    )
    processors: StrictInt | None = Field(
        default=None,
        gt=0,
        description="Desired dedicated processors, or desired virtual processors in "
        "shared mode; processing units and the processor mode never change.",
    )
    memory_mib: StrictInt | None = Field(
        default=None, gt=0, description="Desired memory in MiB."
    )
    disks: DiskPatch | None = Field(
        default=None,
        description="Virtual disks to add (created, mapped, owned and saved into the "
        "default profile) and to detach (the mapping only).",
    )

    @model_validator(mode="after")
    def _not_empty(self) -> ReconfigurePatch:
        fields = (self.name, self.processors, self.memory_mib, self.disks)
        if all(value is None for value in fields):
            raise ValueError(
                "patch needs at least one of name, processors, memory_mib, disks"
            )
        if self.name is not None and not self.name.strip():
            raise ValueError("patch.name must not be blank")
        return self

    @property
    def writes_profile(self) -> bool:
        return self.processors is not None or self.memory_mib is not None

    @property
    def needs_window(self) -> bool:
        """Whether the caller must assert ``exclusive_writer_window`` (ADRs 0314, 0322)."""
        return self.writes_profile or self.disks is not None


def delegated_tools(patch: ReconfigurePatch) -> tuple[str, ...]:
    """The specialists *patch* can use, in a stable order, each once."""
    names = [field for field in DELEGATED if getattr(patch, field) is not None]
    tools = [tool for field in names for tool in DELEGATED[field]]
    if patch.disks is not None:
        tools += DISK_ADD_TOOLS if patch.disks.add else ()
        tools += DISK_DETACH_TOOLS if patch.disks.detach else ()
    return tuple(dict.fromkeys(tools))


def disk_vios_uuids(patch: ReconfigurePatch, tool: str) -> tuple[str, ...]:
    """The VIOSes disk specialist *tool* writes for *patch*; empty for any other tool."""
    if patch.disks is None:
        return ()
    items = (
        patch.disks.add
        if tool in DISK_ADD_TOOLS
        else patch.disks.detach
        if tool in DISK_DETACH_TOOLS
        else []
    )
    return tuple(dict.fromkeys(item.vios_uuid.lower() for item in items))


def open_client(connection: str) -> HMCClient:
    """A client for the operation's recorded connection; a seam for tests."""
    return _power_client(connection)


# ---------------------------------------------------------------------------
# Reading the partition and its profile
# ---------------------------------------------------------------------------


def _text(container: Mapping[str, Any], *path: str) -> str:
    value: Any = container
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    value = leaf_text(value)
    return value.strip() if isinstance(value, str) else ""


def _number(container: Mapping[str, Any], *path: str) -> int:
    text = _text(container, *path)
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{path[-1]} is unavailable or not a whole number") from None


def _units(container: Mapping[str, Any], *path: str) -> float:
    try:
        return float(_text(container, *path))
    except ValueError:
        raise ValueError(f"{path[-1]} is unavailable or malformed") from None


def _dedicated(text: str) -> bool:
    if text not in {"true", "false"}:
        raise ValueError("the processor mode is unavailable or malformed")
    return text == "true"


def partition_view(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """The values the plan rests on: current ones if activated, pending ones if not."""
    resource = (entry or {}).get("Resource")
    if not isinstance(resource, Mapping):
        raise TypeError("the partition read carried no resource")
    state = _text(resource, "PartitionState").lower()
    active = state != _NOT_ACTIVATED
    memory = resource.get("PartitionMemoryConfiguration") or {}
    cpu = resource.get("PartitionProcessorConfiguration") or {}
    prefix = "Current" if active else ""
    dedicated = _dedicated(_text(cpu, f"{prefix}HasDedicatedProcessors").lower())
    if active:
        names = ("CurrentMinimumMemory", "CurrentMemory", "CurrentMaximumMemory")
        container = (
            "CurrentDedicatedProcessorConfiguration"
            if dedicated
            else "CurrentSharedProcessorConfiguration"
        )
        procs = (
            (
                "CurrentMinimumProcessors",
                "CurrentProcessors",
                "CurrentMaximumProcessors",
            )
            if dedicated
            else (
                "CurrentMinimumVirtualProcessors",
                "AllocatedVirtualProcessors",
                "CurrentMaximumVirtualProcessors",
            )
        )
        units = (
            "CurrentMinimumProcessingUnits",
            "CurrentProcessingUnits",
            "CurrentMaximumProcessingUnits",
        )
    else:
        names = ("MinimumMemory", "DesiredMemory", "MaximumMemory")
        container = (
            "DedicatedProcessorConfiguration"
            if dedicated
            else "SharedProcessorConfiguration"
        )
        word = "Processors" if dedicated else "VirtualProcessors"
        procs = tuple(f"{w}{word}" for w in ("Minimum", "Desired", "Maximum"))
        units = tuple(f"{w}ProcessingUnits" for w in ("Minimum", "Desired", "Maximum"))
    return {
        "name": _text(resource, "PartitionName"),
        "state": state,
        "rmc": _text(resource, "ResourceMonitoringControlState").lower(),
        "sync": _text(resource, "CurrentProfileSync").lower(),
        "default_profile": _text(resource, "DefaultProfileName"),
        "last_profile": _text(resource, "LastActivatedProfile"),
        "dedicated": dedicated,
        "memory": [_number(memory, n) for n in names],
        "processors": [_number(cpu, container, n) for n in procs],
        "units": None if dedicated else [_units(cpu, container, n) for n in units],
    }


def profile_view(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The saved minimum, desired and maximum values of a profile read."""
    attrs = ("ProcessorAttributes",)
    dedicated = _dedicated(_text(fields, *attrs, "HasDedicatedProcessors").lower())
    container = (
        "DedicatedProcessorConfiguration"
        if dedicated
        else "SharedProcessorConfiguration"
    )
    word = "Processors" if dedicated else "VirtualProcessors"
    levels = ("Minimum", "Desired", "Maximum")
    return {
        "dedicated": dedicated,
        "memory": [_number(fields, "ProfileMemory", f"{w}Memory") for w in levels],
        "processors": [
            _number(fields, *attrs, container, f"{w}{word}") for w in levels
        ],
        "units": None
        if dedicated
        else [_units(fields, *attrs, container, f"{w}ProcessingUnits") for w in levels],
    }


# ---------------------------------------------------------------------------
# Planning (ADR 0314 Decision 3)
# ---------------------------------------------------------------------------


def _widen(triple: list[int], value: int) -> list[int]:
    return [min(triple[0], value), value, max(triple[2], value)]


def _check_units(
    units: list[float] | None, vcpus: list[int], where: str, floor: float
) -> None:
    """Refuse shared units outside ``floor x VPs <= units <= VPs`` at any level."""
    if units is None:
        return
    least = Decimal(str(floor))
    for unit, count in zip(units, vcpus, strict=True):
        if not least * count <= Decimal(str(unit)) <= count:
            raise OperationFailed(
                f"the {where} processing units would leave the managed system's "
                "units-per-virtual-processor bounds at minimum, desired or maximum; "
                "change the units first"
            )


def _classify(
    resource: str,
    value: int,
    view: dict[str, Any],
    widened: bool,
    allow_disruption: bool,
) -> tuple[str, str | None]:
    if widened:
        return "pending_activation", "outside the profile's minimum or maximum"
    if view["state"] == _NOT_ACTIVATED:
        return "profile", None
    low, current, high = view[resource]
    reason = None
    if view["state"] != "running":
        reason = f"the partition is {view['state']!r}"
    elif view["rmc"] != "active":
        reason = "RMC is not active"
    elif not low <= value <= high:
        reason = "outside the running partition's minimum or maximum"
    elif value < current and not allow_disruption:
        reason = "a live removal needs allow_disruption=true"
    return ("pending_activation", reason) if reason else ("live", None)


def _suffix(resource: str, view: dict[str, Any]) -> str:
    """The ``LparResources`` field suffix that writes *resource* in the partition's mode."""
    if resource == "memory":
        return "memory"
    return "procs" if view["dedicated"] else "vcpus"


def _profile_patch(
    resource: str, saved: list[int], new_saved: list[int]
) -> tuple[dict[str, int], dict[str, int]]:
    """The profile fields to write for one resource, and their earlier values."""
    if new_saved == saved:
        return {}, {}
    kept = [
        (level, old, new)
        for level, old, new in zip(_LEVELS, saved, new_saved, strict=True)
        if old != new or level == "desired"
    ]
    patch = {f"{level}_{resource}": new for level, _, new in kept}
    before = {f"{level}_{resource}": old for level, old, _ in kept}
    return patch, before


def _current_write(
    resource: str, value: int, view: dict[str, Any], applied: str
) -> dict[str, int]:
    """The current-configuration fields to write for one resource, if any."""
    current = view[resource]
    suffix = _suffix(resource, view)
    if view["state"] == _NOT_ACTIVATED:
        pending = _widen(current, value)
        if resource == "processors":
            _check_units(view["units"], pending, "partition's", view.get("floor", 0))
        return {
            f"{level}_{suffix}": new
            for level, old, new in zip(_LEVELS, current, pending, strict=True)
            if old != new
        }
    if applied == "live" and value != current[1]:
        if resource == "processors":
            triple = [current[0], value, current[2]]
            _check_units(view["units"], triple, "partition's", view.get("floor", 0))
        return {f"desired_{suffix}": value}
    return {}


def _resource_plan(
    resource: str,
    value: int,
    view: dict[str, Any],
    profile: dict[str, Any],
    allow_disruption: bool,
) -> tuple[dict[str, Any], dict[str, int], dict[str, int], dict[str, int]]:
    """One resource's change, profile patch, profile before-values and current write."""
    saved, current = profile[resource], view[resource]
    new_saved = _widen(saved, value)
    widened = new_saved != [saved[0], value, saved[2]]
    if view["state"] == _NOT_ACTIVATED:
        pending = _widen(current, value)
        widened = widened or (pending[0], pending[2]) != (current[0], current[2])
    applied, reason = _classify(resource, value, view, widened, allow_disruption)
    patch, before = _profile_patch(resource, saved, new_saved)
    write = _current_write(resource, value, view, applied)
    if resource == "processors":
        limit = view.get("count_limit")
        if limit is not None and value > limit:
            raise OperationFailed(
                "processors exceeds the managed system's per-partition processor limit"
            )
        _check_units(profile["units"], new_saved, "profile's", view.get("floor", 0))
    change = {
        "field": _FIELD[resource],
        "before": {"profile": saved[1], "current": current[1]},
        "after": value,
        "applied": applied,
        "reason": reason,
    }
    return change, patch, before, write


def plan_changes(
    patch: ReconfigurePatch,
    view: dict[str, Any],
    profile: dict[str, Any] | None,
    allow_disruption: bool,
) -> dict[str, Any]:
    """Every change *patch* makes and where it lands, computed before any write."""
    if not view["state"]:
        raise OperationFailed("the partition state is unavailable; nothing was written")
    changes: list[dict[str, Any]] = []
    profile_patch: dict[str, int] = {}
    profile_before: dict[str, int] = {}
    current_write: dict[str, int] = {}
    for resource in _RESOURCES:
        value = getattr(patch, _FIELD[resource])
        if value is None:
            continue
        assert profile is not None
        if value == profile[resource][1] == view[resource][1] and (
            profile[resource][0] <= value <= profile[resource][2]
        ):
            continue
        change, saved, before, write = _resource_plan(
            resource, value, view, profile, allow_disruption
        )
        changes.append(change)
        profile_patch |= saved
        profile_before |= before
        current_write |= write
    rename = None
    if patch.name is not None and patch.name != view["name"]:
        rename = {"before": view["name"], "after": patch.name}
        changes.append(
            {
                "field": "name",
                "before": {"current": view["name"]},
                "after": patch.name,
                "applied": "live",
                "reason": None,
            }
        )
    pending = any(c["applied"] == "pending_activation" for c in changes)
    live_writes = bool(rename) or (
        bool(current_write) and view["state"] != _NOT_ACTIVATED
    )
    if (
        view["state"] != _NOT_ACTIVATED
        and view["sync"] not in _SYNC_OFF
        and pending
        and live_writes
    ):
        raise OperationFailed(
            "profile sync is not disabled or suspended, so a live change or rename "
            "would copy the current configuration over the pending profile values. "
            "Set it with hmc_sync_lpar_profile mode=suspend, or split the patch"
        )
    return {
        "basis": basis(view),
        "profile_patch": profile_patch,
        "profile_before": profile_before,
        "current": current_write or None,
        "rename": rename,
        "changes": changes,
        "activation": None,
    }


def basis(view: dict[str, Any]) -> dict[str, Any]:
    """What a plan rests on; a difference before a write means the plan is stale."""
    return {
        key: view[key]
        for key in ("name", "state", "rmc", "sync", "memory", "processors")
    }


def require_persistable(
    view: Mapping[str, Any], profile: Mapping[str, Any], changes: list[dict[str, Any]]
) -> None:
    """Refuse a force-save that would discard pending profile values (ADR 0322 Decision 4).

    The save copies the current configuration over the whole default profile, so a
    planned ``pending_activation`` change, or a desired value the profile already holds
    apart from the partition's, would be lost.
    """
    if any(change["applied"] == "pending_activation" for change in changes):
        raise OperationFailed(
            "a disk add saves the current configuration over the default profile, which "
            "would discard this patch's pending_activation change; split the patch"
        )
    for resource in ("memory", "processors", "units"):
        saved, current = profile[resource], view[resource]
        if saved is not None and current is not None and saved[1] != current[1]:
            raise OperationFailed(
                f"the default profile's desired {resource} differs from the partition's; "
                "a disk add saves the current configuration over it and would discard "
                "the profile's value. Activate the profile first, or change the value"
            )


def _mapping_ids(entries: list[dict[str, Any]], name: str, lpar_uuid: str) -> list[str]:
    """The ids of the mappings of virtual disk *name* to partition *lpar_uuid*."""
    ids = []
    for entry in entries:
        found = storage_mapping(entry)
        if (
            found.backing_kind == "VirtualDisk"
            and found.backing_name == name
            and (found.lpar_uuid or "").lower() == lpar_uuid
        ):
            ids.append(found.id or "")
    return ids


async def _disk_plan(
    hmc: HMCClient,
    disks: DiskPatch,
    view: Mapping[str, Any],
    system_uuid: str,
    lpar_uuid: str,
    allow_disruption: bool,
) -> dict[str, Any]:
    """The disk writes *disks* needs, checked against live VIOS state (ADR 0322)."""
    active = view["state"] != _NOT_ACTIVATED
    if disks.add and active and (view["state"], view["rmc"]) != ("running", "active"):
        raise OperationFailed(
            f"LPAR {lpar_uuid} is {view['state']!r} with RMC {view['rmc']!r}; a disk add "
            "to an activated partition needs it running with RMC active"
        )
    if disks.detach and active and not allow_disruption:
        raise OperationFailed(
            f"a disk detach from activated LPAR {lpar_uuid} removes a disk the guest may "
            "be using; pass allow_disruption=true"
        )
    vioses = dict.fromkeys(d.vios_uuid.lower() for d in (*disks.add, *disks.detach))
    for vios_uuid in vioses:
        try:
            await resolve_vios_uuid(hmc, vios_uuid, system_name_or_uuid=system_uuid)
        except (HMCError, ValueError, LookupError) as exc:
            raise OperationFailed(
                f"VIOS {vios_uuid} is not on managed system {system_uuid}, or could not "
                f"be read ({type(exc).__name__})"
            ) from exc
    claimed: dict[str, int] = {}
    try:
        adds = [
            await _planned_add(hmc, item, index, claimed)
            for index, item in enumerate(disks.add)
        ]
        detaches = [
            await _planned_detach(hmc, item, index, lpar_uuid)
            for index, item in enumerate(disks.detach)
        ]
    except (HMCError, ValueError, TypeError) as exc:
        raise OperationFailed(
            f"the VIOS storage a disk change names could not be read "
            f"({type(exc).__name__})"
        ) from exc
    if adds:
        await _require_profile_jobs(hmc)
    return {"add": adds, "detach": detaches}


async def _planned_add(
    hmc: HMCClient, item: DiskAdd, index: int, claimed: dict[str, int]
) -> dict[str, Any]:
    """One add, checked; *claimed* sums the MiB earlier adds take from each group."""
    vios_uuid, vg_uuid = item.vios_uuid.lower(), item.vg_uuid.lower()
    groups = await hmc.list_volume_groups(vios_uuid)
    group = [g for g in groups if str(g.get("UUID") or "").lower() == vg_uuid]
    if not group:
        raise OperationFailed(
            f"disks.add[{index}]: volume group {vg_uuid} is not on VIOS {vios_uuid}"
        )
    claimed[vg_uuid] = claimed.get(vg_uuid, 0) + item.capacity_mib
    free = volume_group(group[0]).free_space_gib
    if free is not None and free * 1024 < claimed[vg_uuid]:
        raise OperationFailed(
            f"disks.add[{index}]: volume group {vg_uuid} has {free} GiB free, less than "
            "the patch's adds to it need"
        )
    if any(item.name in virtual_disk_names(group) for group in groups):
        raise OperationFailed(
            f"disks.add[{index}]: VIOS {vios_uuid} already has a virtual disk of that "
            "name; reconfigure never resizes a disk"
        )
    return {
        "vios_uuid": vios_uuid,
        "vg_uuid": vg_uuid,
        "name": item.name,
        "capacity_mib": item.capacity_mib,
    }


async def _planned_detach(
    hmc: HMCClient, item: DiskDetach, index: int, lpar_uuid: str
) -> dict[str, Any]:
    vios_uuid = item.vios_uuid.lower()
    ids = _mapping_ids(await hmc.list_storage_mappings(vios_uuid), item.name, lpar_uuid)
    if len(ids) != 1 or not ids[0]:
        raise OperationFailed(
            f"disks.detach[{index}]: VIOS {vios_uuid} has {len(ids)} identifiable "
            f"mappings of that virtual disk to LPAR {lpar_uuid}; exactly one is needed"
        )
    return {"vios_uuid": vios_uuid, "name": item.name, "mapping_id": ids[0]}


async def _require_profile_jobs(hmc: HMCClient) -> None:
    """A disk add's force-save needs #637's profile jobs (ADR 0313 Decision 10)."""
    console = await hmc.get_console_info()
    resource = console.get("Resource", {}) if isinstance(console, dict) else {}
    version = console_version(resource)
    if version is None or version < PROFILE_JOB_MINIMUM:
        raise OperationFailed(
            "a disk add saves the partition's profile through a profile job, which needs "
            "HMC 10.3.1060 or later; this console's level is "
            f"{'unreadable' if version is None else '.'.join(map(str, version))}"
        )


def _disk_changes(disks: Mapping[str, Any], active: bool) -> list[dict[str, Any]]:
    applied = "live" if active else "profile"
    changes = [
        {
            "field": "disks.add",
            "before": None,
            "after": {
                k: add[k] for k in ("vios_uuid", "vg_uuid", "name", "capacity_mib")
            },
            "applied": applied,
            "reason": None,
        }
        for add in disks["add"]
    ]
    changes += [
        {
            "field": "disks.detach",
            "before": {"vios_uuid": item["vios_uuid"], "name": item["name"]},
            "after": None,
            "applied": applied,
            "reason": None,
        }
        for item in disks["detach"]
    ]
    return changes


# ---------------------------------------------------------------------------
# The operation body
# ---------------------------------------------------------------------------


async def _resolve(hmc: HMCClient, system: str, lpar: str) -> tuple[str, str]:
    """Resolve and authorize the target (ADR 0011); refusals carry no selector."""
    try:
        system_uuid = (await resolve_system_uuid(hmc, system)).lower()
        lpar_uuid = await resolve_and_authorize_lpar_mutation(
            hmc, lpar, ownership_override=False, system_name_or_uuid=system_uuid
        )
    except (HMCError, ValueError, LookupError, PermissionError) as exc:
        raise OperationFailed(
            "the partition could not be resolved on its managed system, or the ADR 0011 "
            f"ownership check refused it ({type(exc).__name__}); see "
            "hmc_list_lpar_ownership"
        ) from exc
    return system_uuid, lpar_uuid.lower()


async def _read_view(hmc: HMCClient, lpar_uuid: str) -> dict[str, Any]:
    try:
        return partition_view(await hmc.get_logical_partition(lpar_uuid))
    except (HMCError, ValueError, TypeError) as exc:
        raise OperationFailed(
            f"LPAR {lpar_uuid} could not be read ({type(exc).__name__})"
        ) from exc


async def _read_profile(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str, name: str
) -> tuple[Any, dict[str, Any]]:
    try:
        record = await read_partition_profile(hmc, system_uuid, lpar_uuid, name)
        return record, profile_view(record.fields)
    except (HMCError, ValueError, TypeError) as exc:
        raise OperationFailed(
            f"the default profile of LPAR {lpar_uuid} could not be read "
            f"({type(exc).__name__})"
        ) from exc


async def _processor_limits(
    hmc: HMCClient, system_uuid: str, dedicated: bool
) -> dict[str, float]:
    """The system's per-partition processor count and units-per-VP floor, when reported.

    The fields ``hmc_dlpar_proc`` checks (``operations/lpar/processors.py``); a sparse
    entry from a system that is not operating leaves them unchecked.
    """
    try:
        entry = await read_system_entry(hmc, system_uuid)
    except HMCError as exc:
        raise OperationFailed(
            f"managed system {system_uuid} could not be read ({type(exc).__name__})"
        ) from exc
    resource = (entry or {}).get("Resource") or {}
    config = resource.get("AssociatedSystemProcessorConfiguration") or {}
    fields = {
        "count_limit": "CurrentMaximumAllowedProcessorsPerPartition"
        if dedicated
        else "MaximumAllowedVirtualProcessorsPerPartition",
    }
    if not dedicated:
        fields["floor"] = "MinimumProcessorUnitsPerVirtualProcessor"
    limits = {}
    for key, name in fields.items():
        try:
            limits[key] = float(_text(config, name))
        except ValueError:
            continue
    return limits


async def _default_profile(
    hmc: HMCClient, view: dict[str, Any], system_uuid: str, lpar_uuid: str
) -> tuple[Any, dict[str, Any]]:
    """The profile a change must persist in (ADR 0314 Decision 2), and its view."""
    name = view["default_profile"]
    if not name:
        raise OperationFailed(f"LPAR {lpar_uuid} has no default profile")
    if view["last_profile"] and view["last_profile"] != name:
        raise OperationFailed(
            f"LPAR {lpar_uuid} last activated a profile other than its default; "
            "a change to one would not persist through activation of the other"
        )
    record, profile = await _read_profile(hmc, system_uuid, lpar_uuid, name)
    if profile["dedicated"] != view["dedicated"]:
        raise OperationFailed(
            f"LPAR {lpar_uuid}'s processor mode differs from its default profile's"
        )
    return record, profile


async def _make_plan(
    hmc: HMCClient,
    patch: ReconfigurePatch,
    allow_disruption: bool,
    system_uuid: str,
    lpar_uuid: str,
) -> dict[str, Any]:
    view = await _read_view(hmc, lpar_uuid)
    if patch.processors is not None:
        view |= await _processor_limits(hmc, system_uuid, view["dedicated"])
    if patch.name is not None:
        escape_xml(patch.name)
        others = await _other_names(hmc, system_uuid, lpar_uuid)
        if patch.name != view["name"] and patch.name in others:
            raise OperationFailed(
                f"another partition on managed system {system_uuid} already has the new name"
            )
    profile = record = None
    if patch.writes_profile or patch.disks is not None:
        record, profile = await _default_profile(hmc, view, system_uuid, lpar_uuid)
        if patch.memory_mib is not None:
            try:
                await require_memory_within_selected_system(
                    hmc, system_uuid, LparResources(desired_memory=patch.memory_mib)
                )
            except (HMCError, ValueError) as exc:
                raise OperationFailed(
                    f"memory_mib exceeds managed system {system_uuid}'s configurable "
                    f"memory, or that bound could not be read ({type(exc).__name__})"
                ) from exc
    plan = plan_changes(patch, view, profile, allow_disruption)
    disks = None
    if patch.disks is not None:
        assert profile is not None
        disks = await _disk_plan(
            hmc, patch.disks, view, system_uuid, lpar_uuid, allow_disruption
        )
        if disks["add"]:
            require_persistable(view, profile, plan["changes"])
        plan["changes"] += _disk_changes(disks, view["state"] != _NOT_ACTIVATED)
    pending = any(c["applied"] == "pending_activation" for c in plan["changes"])
    plan |= {
        "system_uuid": system_uuid,
        "lpar_uuid": lpar_uuid,
        "profile_uuid": None if record is None else record.uuid,
        "profile_name": None if record is None else record.name,
        "profile_etag": None if record is None else record.etag,
        "activation": record.name if record is not None and pending else None,
        "disks": disks,
        "save": record.name if record is not None and disks and disks["add"] else None,
    }
    return plan


async def _other_names(hmc: HMCClient, system_uuid: str, lpar_uuid: str) -> list[str]:
    try:
        entries = await hmc.list_logical_partitions(system_uuid)
    except HMCError as exc:
        raise OperationFailed(
            f"the partition list could not be read ({type(exc).__name__})"
        ) from exc
    return [
        _text(entry.get("Resource") or {}, "PartitionName")
        for entry in entries
        if str(entry.get("UUID") or "").lower() != lpar_uuid
    ]


def _profile_write(hmc: HMCClient, plan: Mapping[str, Any]) -> Write:
    async def write() -> Mapping[str, Any]:
        try:
            outcome = await update_partition_profile(
                hmc,
                plan["system_uuid"],
                plan["lpar_uuid"],
                plan["profile_uuid"],
                plan["profile_etag"] or "",
                ProfilePatch(**plan["profile_patch"]),
                exclusive_writer_window=True,
            )
        except (HMCError, ValueError, TypeError, LookupError, PermissionError) as exc:
            raise EffectNotApplied(
                f"the profile write was refused before writing ({type(exc).__name__}); "
                "the profile may have changed since planning"
            ) from exc
        if outcome.execution == "rejected":
            raise EffectNotApplied(
                "the HMC refused the profile write: the profile changed since planning"
            )
        if outcome.execution == "completed" and outcome.requested_change == "verified":
            return {
                "profile_uuid": plan["profile_uuid"],
                "after_etag": outcome.after_etag,
                "preservation": outcome.preservation,
            }
        raise HMCError(
            f"the profile write ended execution={outcome.execution}, "
            f"requested_change={outcome.requested_change}; read the profile before resuming"
        )

    return write


def _partition_write(
    hmc: HMCClient, lpar_uuid: str, subject: str, **changes: Any
) -> Write:
    async def write() -> Mapping[str, Any]:
        try:
            await hmc.update_logical_partition(
                lpar_uuid, lambda lpar: partition_updates(lpar, **changes), subject
            )
        except LparUpdateError as exc:
            if exc.submission == "rejected":
                raise EffectNotApplied(
                    f"the HMC refused the write to {subject}: the partition changed "
                    "since it was read"
                ) from exc
            # The HMC's text can quote the submitted name, so only the shape is kept.
            raise HMCError(
                f"the write to {subject} ended {exc.submission} "
                f"(HTTP {exc.status_code}); inspect LPAR {lpar_uuid} before resuming",
                exc.status_code,
            ) from exc
        except (HMCError, ValueError, TypeError) as exc:
            # Raised by the read or the edit, before the single POST.
            raise EffectNotApplied(
                f"the write to {subject} was refused before submission "
                f"({type(exc).__name__})"
            ) from exc
        return {}

    return write


def _unmap_write(hmc: HMCClient, lpar_uuid: str, item: Mapping[str, Any]) -> Write:
    vios_uuid, mapping_id = item["vios_uuid"], item["mapping_id"]

    async def write() -> Mapping[str, Any]:
        try:
            ids = _mapping_ids(
                await hmc.list_storage_mappings(vios_uuid), item["name"], lpar_uuid
            )
        except (HMCError, ValueError, TypeError) as exc:
            raise EffectNotApplied(
                f"the mappings of VIOS {vios_uuid} could not be read "
                f"({type(exc).__name__})"
            ) from exc
        if ids != [mapping_id]:
            raise EffectNotApplied(
                f"mapping {mapping_id} on VIOS {vios_uuid} no longer maps the planned "
                f"disk to LPAR {lpar_uuid}; nothing was detached"
            )
        try:
            await hmc.delete_storage_mapping(vios_uuid, mapping_id, lpar_uuid)
        except ValueError as exc:
            # The ADR 0168 checks before the single POST refused it.
            raise EffectNotApplied(
                f"the detach of mapping {mapping_id} was refused before submission"
            ) from exc
        return {"vios_uuid": vios_uuid, "mapping_id": mapping_id}

    return write


def _disk_write(hmc: HMCClient, add: Mapping[str, Any]) -> Write:
    async def write() -> Mapping[str, Any]:
        try:
            await hmc.create_virtual_disk(
                add["vios_uuid"], add["vg_uuid"], add["name"], add["capacity_mib"]
            )
        except ValueError as exc:
            raise EffectNotApplied(
                f"the disk create on VIOS {add['vios_uuid']} was refused before "
                "submission"
            ) from exc
        return {
            "vios_uuid": add["vios_uuid"],
            "vg_uuid": add["vg_uuid"],
            "disk_name": add["name"],
            "capacity_mib": add["capacity_mib"],
        }

    return write


def _map_write(hmc: HMCClient, lpar_uuid: str, add: Mapping[str, Any]) -> Write:
    async def write() -> Mapping[str, Any]:
        try:
            await hmc.map_storage_to_lpar(
                add["vios_uuid"], "VirtualDisk", add["name"], lpar_uuid, None
            )
        except ValueError as exc:
            raise EffectNotApplied(
                f"the mapping on VIOS {add['vios_uuid']} was refused before submission"
            ) from exc
        return {"vios_uuid": add["vios_uuid"], "storage_name": add["name"]}

    return write


def _ledger(ctx: OperationContext, plan: Mapping[str, Any], index: int) -> None:
    """Own a disk this operation created (ADR 0322 Decision 2); a replay re-inserts."""
    add = plan["disks"]["add"][index]
    ctx.ledger_add(
        f"added_disk:{index}",
        DISK_LEDGER,
        plan["system_uuid"],
        plan["lpar_uuid"],
        {
            "vios_uuid": add["vios_uuid"],
            "vg_uuid": add["vg_uuid"],
            "disk_name": add["name"],
        },
    )


def _index(key: str) -> int:
    return int(key.rpartition(":")[2])


def _steps(plan: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The writes the plan needs, in ADR 0314 Decision 4's then ADR 0322's order."""
    lpar_uuid = plan["lpar_uuid"]
    steps = []
    if plan["profile_patch"]:
        target = profile_write.target(lpar_uuid, plan["profile_uuid"])
        steps.append((PROFILE_KEY, profile_write.KIND, target))
    if plan["current"]:
        steps.append((CURRENT_KEY, CURRENT_KIND, f"lpar:{lpar_uuid}"))
    if plan["rename"]:
        steps.append((RENAME_KEY, RENAME_KIND, f"lpar:{lpar_uuid}"))
    disks = plan.get("disks") or {"add": [], "detach": []}
    for index, item in enumerate(disks["detach"]):
        steps.append((f"unmap:{index}", UNMAP_KIND, f"vios:{item['vios_uuid']}"))
    for index, add in enumerate(disks["add"]):
        steps.append((f"disk:{index}", DISK_KIND, f"vios:{add['vios_uuid']}"))
        steps.append((f"map:{index}", MAP_KIND, f"lpar:{lpar_uuid}"))
    return steps


def _stale(
    ctx: OperationContext, plan: Mapping[str, Any], view: dict[str, Any]
) -> bool:
    """Whether the partition differs from the plan's basis in what no applied write moved.

    Once the plan's own ``current`` write is applied, the resources it changed are its
    result rather than the basis, and a DLPAR may still be settling, so they are left out;
    once its ``rename`` is applied, the name is likewise its result (the disk writes and
    the save follow it).
    """
    observed, expected = basis(view), dict(plan["basis"])
    moved = {CURRENT_KEY: _RESOURCES, RENAME_KEY: ("name",)}
    for key, fields in moved.items():
        applied = ctx.recorded(key)
        if applied is not None and applied.status == "applied":
            for field in fields:
                observed.pop(field)
                expected.pop(field)
    return observed != expected


async def _apply(
    ctx: OperationContext, hmc: HMCClient, plan: Mapping[str, Any]
) -> None:
    lpar_uuid, system_uuid = plan["lpar_uuid"], plan["system_uuid"]
    for key, kind, target in _steps(plan):
        recorded = ctx.recorded(key)
        if recorded is not None and recorded.status == "applied":
            if kind == DISK_KIND:
                _ledger(ctx, plan, _index(key))
            continue
        # ADR 0314 Decisions 6 and 7: target, owner and the plan's basis before every
        # write not yet applied.
        await _resolve(hmc, system_uuid, lpar_uuid)
        if _stale(ctx, plan, await _read_view(hmc, lpar_uuid)):
            raise OperationFailed(
                f"LPAR {lpar_uuid} changed since the plan was made (state, RMC, profile "
                "sync, name or resources); this run wrote nothing, and the effects list "
                "shows what earlier runs applied. Start a new request"
            )
        if key == PROFILE_KEY:
            write = _profile_write(hmc, plan)
        elif key == CURRENT_KEY:
            write = _partition_write(
                hmc,
                lpar_uuid,
                "the current configuration",
                resources=LparResources(**plan["current"]),
            )
        elif key == RENAME_KEY:
            write = _partition_write(
                hmc, lpar_uuid, "the partition name", name=plan["rename"]["after"]
            )
        elif kind == UNMAP_KIND:
            write = _unmap_write(hmc, lpar_uuid, plan["disks"]["detach"][_index(key)])
        elif kind == DISK_KIND:
            write = _disk_write(hmc, plan["disks"]["add"][_index(key)])
        else:
            write = _map_write(hmc, lpar_uuid, plan["disks"]["add"][_index(key)])
        await ctx.effect(key, kind, target, write)
        if kind == DISK_KIND:
            _ledger(ctx, plan, _index(key))


async def _persist(
    ctx: OperationContext, hmc: HMCClient, plan: Mapping[str, Any]
) -> None:
    """Force-save the added disks into the default profile (ADR 0322 Decision 3)."""
    recorded = ctx.recorded(SAVE_KEY)
    if recorded is not None and recorded.status == "applied":
        return
    lpar_uuid, system_uuid, name = plan["lpar_uuid"], plan["system_uuid"], plan["save"]
    await _resolve(hmc, system_uuid, lpar_uuid)
    view = await _read_view(hmc, lpar_uuid)
    record, profile = await _default_profile(hmc, view, system_uuid, lpar_uuid)
    if record.name != name or _stale(ctx, plan, view):
        raise OperationFailed(
            f"LPAR {lpar_uuid}'s default profile or state changed since the plan was "
            "made; the added disks were not saved into a profile"
        )
    require_persistable(view, profile, [])
    await ctx.effect(
        SAVE_KEY,
        profile_persist.KIND,
        profile_persist.target(lpar_uuid, name),
        lambda: profile_persist.save_current_over(
            hmc, system_uuid, lpar_uuid, name, exclusive_writer_window=True
        ),
    )


def _expected(plan: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """The (profile, current) values verification must read, by ``min/desired/max``."""
    index = {"min": 0, "desired": 1, "max": 2}
    profile = {
        f"{key.rsplit('_', 1)[1]}:{index[key.split('_', 1)[0]]}": value
        for key, value in plan["profile_patch"].items()
    }
    current: dict[str, int] = {}
    for key, value in (plan["current"] or {}).items():
        level, kind = key.split("_", 1)
        resource = "memory" if kind == "memory" else "processors"
        current[f"{resource}:{index[level]}"] = value
    return profile, current


def _observed(view: Mapping[str, Any], keys: Mapping[str, int]) -> dict[str, int]:
    observed = {}
    for key in keys:
        resource, level = key.split(":")
        observed[key] = view[resource][int(level)]
    return observed


async def _verify(
    ctx: OperationContext, hmc: HMCClient, plan: Mapping[str, Any]
) -> BodyResult:
    """Poll until the profile and partition read what the plan wrote (Decision 7)."""
    want_profile, want_current = _expected(plan)
    lpar_uuid = plan["lpar_uuid"]
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        view = await _read_view(hmc, lpar_uuid)
        seen_profile: dict[str, int] = {}
        if want_profile:
            _, profile = await _read_profile(
                hmc, plan["system_uuid"], lpar_uuid, plan["profile_name"]
            )
            seen_profile = _observed(profile, want_profile)
        seen_current = _observed(view, want_current)
        renamed = plan["rename"] is None or view["name"] == plan["rename"]["after"]
        done = seen_profile == want_profile and seen_current == want_current and renamed
        if done or time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_SECONDS)
    result = _result(ctx, plan)
    result["observed"] = {
        "profile": seen_profile,
        "current": seen_current,
        "name": view["name"],
    }
    return BodyResult("completed" if done else "needs_attention", result)


def _result(ctx: OperationContext, plan: Mapping[str, Any]) -> dict[str, Any]:
    warnings = []
    profile = ctx.recorded(PROFILE_KEY)
    identity = (profile.identity or {}) if profile is not None else {}
    if identity.get("preservation") == "drifted":
        warnings.append(
            "the profile write was applied with preservation drift: fields other than "
            "the requested ones differ from the earlier profile; inspect it"
        )
    pending = plan["activation"] is not None
    if pending and plan["basis"]["sync"] not in _SYNC_OFF:
        warnings.append(
            "profile sync is on: a later current-configuration change by another writer "
            "can overwrite the pending profile values (ADR 0201)"
        )
    warnings += _disk_warnings(ctx, plan)
    return {
        "system_uuid": plan["system_uuid"],
        "lpar_uuid": plan["lpar_uuid"],
        "profile_name": plan["profile_name"],
        "changes": plan["changes"],
        "activation": plan["activation"],
        "warnings": warnings,
    }


def _disk_warnings(ctx: OperationContext, plan: Mapping[str, Any]) -> list[str]:
    """Where added disks persist, and the recovery profiles the save kept (ADR 0322)."""
    if not plan.get("save"):
        return []
    saved = ctx.recorded(SAVE_KEY)
    if saved is not None and saved.status == "applied":
        kept = (saved.identity or {}).get("recovery_profiles") or []
        return [
            (
                f"the default profile {plan['save']} was overwritten with the current "
                "configuration; its earlier content is kept in recovery profile(s) "
                f"{', '.join(kept) or 'none reported'}. Delete them with "
                "hmc_delete_partition_profile when no longer needed"
            )
        ]
    adds = len(plan["disks"]["add"])
    if not any(_touched(ctx, f"disk:{index}") for index in range(adds)):
        return []
    return [
        (
            "the added disks are in the current configuration only: resume to save them "
            f"into profile {plan['save']}; abandon leaves them out of it, so a profile "
            "activation drops their client adapters while the disks stay owned"
        )
    ]


def reconfigure_body(
    patch: ReconfigurePatch, allow_disruption: bool, system: str, lpar: str
) -> engine.Body:
    """The operation body for one ``hmc_reconfigure_lpar`` request."""

    async def body(ctx: OperationContext) -> BodyResult:
        recorded = ctx.recorded(PLAN_KEY)
        async with open_client(ctx.connection) as hmc:
            if recorded is not None and recorded.status == "applied":
                plan = dict(recorded.identity or {})
            else:
                ctx.set_phase("validating")
                system_uuid, lpar_uuid = await _resolve(hmc, system, lpar)
                ctx.guard(system_uuid, lpar_uuid)
                made = await _make_plan(
                    hmc, patch, allow_disruption, system_uuid, lpar_uuid
                )

                if len(json.dumps(made, sort_keys=True)) > store.MAX_IDENTITY_BYTES:
                    raise OperationFailed(
                        "the plan exceeds the operation store's effect identity bound; "
                        "use a shorter or ASCII partition name"
                    )

                async def record() -> Mapping[str, Any]:
                    return made

                plan = dict(
                    await ctx.effect(PLAN_KEY, PLAN_KIND, f"lpar:{lpar_uuid}", record)
                )
            ctx.set_phase("applying")
            try:
                await _apply(ctx, hmc, plan)
                verified = await _verify(ctx, hmc, plan)
                if verified.outcome != "completed" or not plan.get("save"):
                    return verified
                await _persist(ctx, hmc, plan)
                result = _result(ctx, plan) | {"observed": verified.result["observed"]}
                return BodyResult("completed", result)
            except Exception as exc:
                if not any(_touched(ctx, key) for key, _, _ in _steps(plan)):
                    raise
                # Something already landed, so "failed" would hide it: pause, report
                # what the plan changed, and let resume retry and re-verify.
                result = _result(ctx, plan)
                result["warnings"].append(_failure_note(exc, plan["lpar_uuid"]))
                return BodyResult("needs_attention", result)

    return body


async def _classify_plan(
    _ctx: OperationContext, _effect: EffectRecord
) -> Classification:
    # Recording the plan writes nothing to the HMC, so an open one is recomputed.
    return Classification("not_applied")


def _failure_note(exc: Exception, lpar_uuid: str) -> str:
    """A warning naming the failure's type, never its text (``engine`` module note)."""
    if isinstance(exc, (OperationFailed, EffectNotApplied)):
        return f"{type(exc).__name__}: {exc}"[:512]
    return (
        f"{type(exc).__name__} while reconfiguring LPAR {lpar_uuid}; resume or abandon"
    )


def _touched(ctx: OperationContext, key: str) -> bool:
    """Whether the write under *key* landed or may have (applied or still open)."""
    recorded = ctx.recorded(key)
    return recorded is not None and recorded.status != "not_applied"


def _plan_of(ctx: OperationContext) -> Mapping[str, Any]:
    recorded = ctx.recorded(PLAN_KEY)
    if recorded is None or recorded.status != "applied":
        raise ValueError("the operation has no recorded plan")
    return recorded.identity or {}


def _profile_values(
    ctx: OperationContext, _effect: EffectRecord
) -> tuple[ProfilePatch, ProfilePatch]:
    plan = _plan_of(ctx)
    return ProfilePatch(**plan["profile_patch"]), ProfilePatch(**plan["profile_before"])


async def _classify_rename(
    ctx: OperationContext, effect: EffectRecord
) -> Classification:
    rename = _plan_of(ctx)["rename"]
    _, _, lpar_uuid = effect.target.partition(":")
    async with open_client(ctx.connection) as hmc:
        entry = await hmc.get_logical_partition(lpar_uuid)
    name = _text((entry or {}).get("Resource") or {}, "PartitionName")
    if name == rename["after"]:
        return Classification("applied", {})
    if name == rename["before"]:
        return Classification("not_applied")
    return Classification(
        "needs_attention",
        reason=f"LPAR {lpar_uuid} has neither the old nor the new name; inspect it",
    )


def _planned(ctx: OperationContext, effect: EffectRecord, part: str) -> dict[str, Any]:
    return dict(_plan_of(ctx)["disks"][part][_index(effect.key)])


async def _classify_unmap(
    ctx: OperationContext, effect: EffectRecord
) -> Classification:
    item, lpar_uuid = _planned(ctx, effect, "detach"), _plan_of(ctx)["lpar_uuid"]
    async with open_client(ctx.connection) as hmc:
        entries = await hmc.list_storage_mappings(item["vios_uuid"])
    if item["mapping_id"] in _mapping_ids(entries, item["name"], lpar_uuid):
        return Classification("not_applied")
    return Classification(
        "applied", {"vios_uuid": item["vios_uuid"], "mapping_id": item["mapping_id"]}
    )


async def _classify_disk(ctx: OperationContext, effect: EffectRecord) -> Classification:
    add, plan = _planned(ctx, effect, "add"), _plan_of(ctx)
    async with open_client(ctx.connection) as hmc:
        groups = await hmc.list_volume_groups(add["vios_uuid"])
    group = [g for g in groups if str(g.get("UUID") or "").lower() == add["vg_uuid"]]
    if len(group) != 1:
        return Classification(
            "needs_attention", reason="the volume group is unreadable"
        )
    if add["name"] not in virtual_disk_names(group[0]):
        return Classification("not_applied")
    identity = {
        "vios_uuid": add["vios_uuid"],
        "vg_uuid": add["vg_uuid"],
        "disk_name": add["name"],
    }
    if _ledgered_elsewhere(ctx, DISK_LEDGER, plan["system_uuid"], identity):
        return Classification(
            "needs_attention", reason="the disk belongs to another operation"
        )
    return Classification("applied", {**identity, "capacity_mib": add["capacity_mib"]})


async def _classify_map(ctx: OperationContext, effect: EffectRecord) -> Classification:
    add, lpar_uuid = _planned(ctx, effect, "add"), _plan_of(ctx)["lpar_uuid"]
    async with open_client(ctx.connection) as hmc:
        entries = await hmc.list_storage_mappings(add["vios_uuid"])
    if _mapping_ids(entries, add["name"], lpar_uuid):
        return Classification(
            "applied", {"vios_uuid": add["vios_uuid"], "storage_name": add["name"]}
        )
    return Classification("not_applied")


# lpar.current_config has none: a DLPAR write cannot be told from a concurrent
# change, so an open one always pauses for attention (H1 spec *Resume*).
RECONFIGURE_CLASSIFIERS: Mapping[str, engine.Classifier] = {
    PLAN_KIND: _classify_plan,
    profile_write.KIND: profile_write.profile_write_classifier(
        _profile_values, lambda connection: open_client(connection)
    ),
    RENAME_KIND: _classify_rename,
    UNMAP_KIND: _classify_unmap,
    DISK_KIND: _classify_disk,
    MAP_KIND: _classify_map,
    profile_persist.KIND: profile_persist.profile_save_classifier(
        lambda connection: open_client(connection)
    ),
}
