"""``hmc_reconfigure_lpar``'s planner, operation body and classifiers (ADR 0314).

The planner turns one partition read and one profile read into a JSON plan that names
every change and where it lands (``live``, ``profile``, ``pending_activation``) before
anything is written. The body records that plan as an effect, then replays the writes
it names: the default profile, the partition's current configuration, the rename.
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
from ...errors import HMCError, LparUpdateError
from ...resource_identity import resolve_system_uuid
from ...xmlutil import escape_xml, leaf_text
from ..logical import engine, profile_write, store
from ..logical.engine import (
    BodyResult,
    Classification,
    EffectNotApplied,
    OperationContext,
    OperationFailed,
)
from ..logical.models import EffectRecord
from ..systems.fleet import read_system_entry
from .memory_bound import require_memory_within_selected_system
from .ownership import resolve_and_authorize_lpar_mutation
from .power import open_client as _power_client
from .profiles import read_partition_profile, update_partition_profile

# ADR 0314 Decision 6: the specialists whose authority each patch field uses.
DELEGATED: Mapping[str, tuple[str, ...]] = {
    "name": ("hmc_modify_lpar", "hmc_rename_lpar"),
    "memory_mib": ("hmc_dlpar_mem", "hmc_update_partition_profile"),
    "processors": ("hmc_dlpar_proc", "hmc_update_partition_profile"),
}
# Persisted contract (ADR 0195 Consequences): never change while an operation can be open.
PLAN_KEY, PLAN_KIND = "plan", "lpar.reconfigure_plan"
PROFILE_KEY = "profile"
CURRENT_KEY, CURRENT_KIND = "current", "lpar.current_config"
RENAME_KEY, RENAME_KIND = "rename", "lpar.rename"
SETTLE_SECONDS = 120
POLL_SECONDS = 5
_NOT_ACTIVATED = "not activated"
_SYNC_OFF = frozenset({"disabled", "suspended"})
_RESOURCES = ("memory", "processors")
_FIELD = {"memory": "memory_mib", "processors": "processors"}
_LEVELS = ("min", "desired", "max")
Write = Callable[[], Awaitable[Mapping[str, Any]]]


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

    @model_validator(mode="after")
    def _not_empty(self) -> ReconfigurePatch:
        if self.name is None and self.processors is None and self.memory_mib is None:
            raise ValueError("patch needs at least one of name, processors, memory_mib")
        if self.name is not None and not self.name.strip():
            raise ValueError("patch.name must not be blank")
        return self

    @property
    def writes_profile(self) -> bool:
        return self.processors is not None or self.memory_mib is not None


def delegated_tools(patch: ReconfigurePatch) -> tuple[str, ...]:
    """The specialists *patch* can use, in a stable order, each once."""
    names = [field for field in DELEGATED if getattr(patch, field) is not None]
    return tuple(dict.fromkeys(tool for field in names for tool in DELEGATED[field]))


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
            f"LPAR {lpar_uuid} could not be read ({type(exc).__name__}: {exc})"
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
    if patch.writes_profile:
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
    pending = any(c["applied"] == "pending_activation" for c in plan["changes"])
    plan |= {
        "system_uuid": system_uuid,
        "lpar_uuid": lpar_uuid,
        "profile_uuid": None if record is None else record.uuid,
        "profile_name": None if record is None else record.name,
        "profile_etag": None if record is None else record.etag,
        "activation": record.name if record is not None and pending else None,
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
            raise
        except (HMCError, ValueError, TypeError) as exc:
            # Raised by the read or the edit, before the single POST.
            raise EffectNotApplied(
                f"the write to {subject} was refused before submission "
                f"({type(exc).__name__})"
            ) from exc
        return {}

    return write


def _steps(plan: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The writes the plan needs, in ADR 0314 Decision 4's order."""
    lpar_uuid = plan["lpar_uuid"]
    steps = []
    if plan["profile_patch"]:
        target = profile_write.target(lpar_uuid, plan["profile_uuid"])
        steps.append((PROFILE_KEY, profile_write.KIND, target))
    if plan["current"]:
        steps.append((CURRENT_KEY, CURRENT_KIND, f"lpar:{lpar_uuid}"))
    if plan["rename"]:
        steps.append((RENAME_KEY, RENAME_KIND, f"lpar:{lpar_uuid}"))
    return steps


def _stale(
    ctx: OperationContext, plan: Mapping[str, Any], view: dict[str, Any]
) -> bool:
    """Whether the partition differs from the plan's basis in what no applied write moved.

    Once the plan's own ``current`` write is applied, the resources it changed are its
    result rather than the basis, and a DLPAR may still be settling, so they are left out.
    """
    observed, expected = basis(view), dict(plan["basis"])
    applied = ctx.recorded(CURRENT_KEY)
    if applied is not None and applied.status == "applied":
        for resource in _RESOURCES:
            observed.pop(resource)
            expected.pop(resource)
    return observed != expected


async def _apply(
    ctx: OperationContext, hmc: HMCClient, plan: Mapping[str, Any]
) -> None:
    lpar_uuid, system_uuid = plan["lpar_uuid"], plan["system_uuid"]
    for key, kind, target in _steps(plan):
        recorded = ctx.recorded(key)
        if recorded is not None and recorded.status == "applied":
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
        else:
            write = _partition_write(
                hmc, lpar_uuid, "the partition name", name=plan["rename"]["after"]
            )
        await ctx.effect(key, kind, target, write)


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
    return {
        "system_uuid": plan["system_uuid"],
        "lpar_uuid": plan["lpar_uuid"],
        "profile_name": plan["profile_name"],
        "changes": plan["changes"],
        "activation": plan["activation"],
        "warnings": warnings,
    }


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
                return await _verify(ctx, hmc, plan)
            except Exception as exc:
                if not any(_touched(ctx, key) for key, _, _ in _steps(plan)):
                    raise
                # Something already landed, so "failed" would hide it: pause, report
                # what the plan changed, and let resume retry and re-verify.
                result = _result(ctx, plan)
                result["warnings"].append(f"{type(exc).__name__}: {exc}"[:512])
                return BodyResult("needs_attention", result)

    return body


async def _classify_plan(
    _ctx: OperationContext, _effect: EffectRecord
) -> Classification:
    # Recording the plan writes nothing to the HMC, so an open one is recomputed.
    return Classification("not_applied")


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


# lpar.current_config has none: a DLPAR write cannot be told from a concurrent
# change, so an open one always pauses for attention (H1 spec *Resume*).
RECONFIGURE_CLASSIFIERS: Mapping[str, engine.Classifier] = {
    PLAN_KIND: _classify_plan,
    profile_write.KIND: profile_write.profile_write_classifier(
        _profile_values, lambda connection: open_client(connection)
    ),
    RENAME_KIND: _classify_rename,
}
