"""The read-only partition inspection behind ``hmc_inspect_lpar`` (ADR 0200).

Reads one partition from its system's partition list, then each included section.
Each section is authorized through *admit* as the tool it delegates to, before it
is read, so this module never sees the access policy. The contract is
``docs/workflow/specs/2026-10-03-lpar-inspect-design.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from ...errors import HMCError, HMCTransportError
from ...resource_identity import resolve_system_uuid
from ...ssh.refcodes import list_lpar_refcodes
from ...ssh.selectors import resolve_ssh_names
from ..inventory.composite import _container, _processor_figures
from ..inventory.logical import _float, _int, _text
from .decommission import collect_storage_records, resolve_target_lpar

Section = Literal["resources", "rmc", "profile_drift", "refcodes"]
DEFAULT_INCLUDE: tuple[Section, ...] = ("rmc", "refcodes")
BASE_TOOL = "hmc_get_lpar"
REFCODES_TOOL = "hmc_read_lpar_refcodes"
VIOS_LIST_TOOL = "hmc_list_vios"
VIOS_DETAIL_TOOL = "hmc_get_vios_storage_detail"
MAX_REFCODES = 20
MAX_VIOS = 16
_MAX_DETAIL = 500
_HEALTHY_RMC = frozenset({"active", "busy"})
_TRANSITIONAL = frozenset(
    {
        "starting",
        "shutting down",
        "suspending",
        "resuming",
        "migrating not active",
        "migrating running",
        "hardware discovery",
    }
)
_PROFILE_DRIFT = (
    "no read-only partition-profile tool exists yet (#637), "
    "so profile drift is not checked"
)

Admit = Callable[[str, Mapping[str, str | None]], str | None]
"""Asks whether a delegated tool may run for the given targets by kind.

Returns ``None`` when admitted, otherwise the denial text.
"""

SourceState = Literal["ok", "unavailable", "denied"]
_T = TypeVar("_T")


@dataclass(frozen=True)
class SectionSource:
    """Whether a section answered, and through which delegated tool."""

    status: SourceState
    tool: str | None
    detail: str | None = None


@dataclass(frozen=True)
class Rmc:
    source: SectionSource
    state: str | None


@dataclass(frozen=True)
class Refcodes:
    """At most 20 reference codes, newest first."""

    source: SectionSource
    codes: list[dict[str, Any]]


@dataclass(frozen=True)
class ViosRead:
    uuid: str | None
    status: SourceState
    detail: str | None = None


@dataclass(frozen=True)
class Resources:
    """Figures from the partition read, and the storage its VIOSes map to it.

    The figures are always present; ``storage_source`` covers ``vios``,
    ``mappings`` and ``unresolved_mappings`` only.
    """

    current_memory_mib: int | None
    desired_memory_mib: int | None
    current_proc_units: float | None
    desired_proc_units: float | None
    desired_vcpus: int | None
    dedicated_procs: bool | None
    storage_source: SectionSource
    vios: list[ViosRead]
    mappings: list[dict[str, str]]
    unresolved_mappings: int


@dataclass(frozen=True)
class LparInspection:
    """One partition. ``id`` is ``<connection>/<system uuid>/<partition uuid>``."""

    connection: str
    id: str
    system_uuid: str
    uuid: str
    name: str | None
    partition_id: int | None
    state: str | None
    rmc: Rmc | None
    resources: Resources | None
    refcodes: Refcodes | None
    profile_drift: SectionSource | None
    next_actions: list[str]


def _detail(text: str) -> str:
    return text[:_MAX_DETAIL]


def _error_text(exc: Exception) -> str:
    return _detail(str(exc) or type(exc).__name__)


async def _read(tool: str, read: Awaitable[_T]) -> tuple[_T | None, SectionSource]:
    """Await *read*, reporting an HMC or input failure as ``unavailable``."""
    try:
        return await read, SectionSource("ok", tool)
    except (HMCError, ValueError) as exc:
        return None, SectionSource("unavailable", tool, _error_text(exc))


async def _partition(hmc: Any, system: str, lpar: str) -> tuple[str, dict[str, Any]]:
    system_uuid = await resolve_system_uuid(hmc, system)
    return system_uuid.lower(), await resolve_target_lpar(hmc, system_uuid, lpar)


def _rmc(resource: dict[str, Any]) -> Rmc:
    state = _text(resource.get("ResourceMonitoringControlState"))
    if not state:
        text = "the partition read carried no RMC state"
        return Rmc(SectionSource("unavailable", BASE_TOOL, text), None)
    return Rmc(SectionSource("ok", BASE_TOOL), state)


async def _refcodes(
    hmc: Any, admit: Admit, system: str, lpar: str, name: str | None
) -> Refcodes:
    denial = admit(REFCODES_TOOL, {"lpar": lpar, "managed_system": system})
    if denial is not None:
        return Refcodes(SectionSource("denied", REFCODES_TOOL, _detail(denial)), [])

    async def read() -> list[dict[str, Any]]:
        system_name, lpar_name = await resolve_ssh_names(
            hmc.config, system, name or lpar
        )
        return await list_lpar_refcodes(
            hmc.config, system_name, lpar_name, MAX_REFCODES
        )

    codes, source = await _read(REFCODES_TOOL, read())
    return Refcodes(source, (codes or [])[:MAX_REFCODES])


_STALLED = "the HMC stopped answering"


async def _vios_reads(
    hmc: Any,
    admit: Admit,
    system: str,
    entries: list[dict[str, Any]],
    target: tuple[str, str | None],
) -> tuple[list[ViosRead], list[dict[str, str]], int]:
    """Read each VIOS's mappings for *target*, stopping once the HMC stops answering."""
    reads: list[ViosRead] = []
    mappings: list[dict[str, str]] = []
    unresolved = 0
    stalled = False
    for entry in entries[:MAX_VIOS]:
        uuid = _text(entry.get("UUID"))
        if uuid is None or stalled:
            reads.append(
                ViosRead(uuid, "unavailable", _STALLED if stalled else "no VIOS UUID")
            )
            continue
        denial = admit(VIOS_DETAIL_TOOL, {"vios": uuid, "managed_system": system})
        if denial is not None:
            reads.append(ViosRead(uuid, "denied", _detail(denial)))
            continue
        try:
            detail = await hmc.get_vios_storage_detail(uuid)
        except HMCTransportError:
            stalled = True
            reads.append(ViosRead(uuid, "unavailable", _STALLED))
            continue
        except (HMCError, ValueError) as exc:
            reads.append(ViosRead(uuid, "unavailable", _error_text(exc)))
            continue
        if detail is None:
            reads.append(ViosRead(uuid, "unavailable", "no storage detail"))
            continue
        reads.append(ViosRead(uuid, "ok"))
        found, unmatched = collect_storage_records(
            detail.get("Resource") or {}, uuid, *target
        )
        mappings.extend(found)
        unresolved += unmatched
    return reads, mappings, unresolved


def _storage_source(
    listing: SectionSource, reads: list[ViosRead], extra: bool
) -> SectionSource:
    if listing.status != "ok":
        return listing
    statuses = {read.status for read in reads}
    if "denied" in statuses:
        return SectionSource(
            "denied", VIOS_DETAIL_TOOL, "a VIOS read was denied; see vios"
        )
    if "unavailable" in statuses or extra:
        text = (
            f"only {MAX_VIOS} VIOSes read" if extra else "a VIOS was not read; see vios"
        )
        return SectionSource("unavailable", VIOS_DETAIL_TOOL, text)
    return listing


async def _resources(
    hmc: Any,
    admit: Admit,
    system: str,
    system_uuid: str,
    entry: dict[str, Any],
) -> Resources:
    resource = entry.get("Resource") or {}
    memory = _container(resource, "PartitionMemoryConfiguration")
    processors = _processor_figures(resource)
    reads: list[ViosRead] = []
    mappings: list[dict[str, str]] = []
    unresolved = 0
    extra = False
    denial = admit(VIOS_LIST_TOOL, {"managed_system": system})
    if denial is not None:
        listing = SectionSource("denied", VIOS_LIST_TOOL, _detail(denial))
    else:
        entries, listing = await _read(VIOS_LIST_TOOL, hmc.list_vios(system_uuid))
        entries = entries or []
        extra = len(entries) > MAX_VIOS
        target = (str(entry["UUID"]), _text(resource.get("PartitionID")))
        reads, mappings, unresolved = await _vios_reads(
            hmc, admit, system, entries, target
        )
    return Resources(
        current_memory_mib=_int(memory.get("CurrentMemory")),
        desired_memory_mib=_int(memory.get("DesiredMemory")),
        current_proc_units=_float(processors["current_proc_units"]),
        desired_proc_units=_float(processors["desired_proc_units"]),
        desired_vcpus=_int(processors["desired_vcpus"]),
        dedicated_procs=processors["dedicated_procs"],
        storage_source=_storage_source(listing, reads, extra),
        vios=reads,
        mappings=mappings,
        unresolved_mappings=unresolved,
    )


def next_actions(state: str | None, rmc: Rmc | None) -> list[str]:
    """The tools that would explain or advance *state*, by the spec's table."""
    observed = (state or "").lower()
    if observed == "not activated":
        return ["hmc_power_lpar"]
    if observed in {"error", "open firmware"}:
        return ["hmc_capture_lpar_console"]
    if observed in _TRANSITIONAL:
        return ["hmc_inspect_lpar"]
    rmc_down = rmc is not None and (rmc.state or "").lower() not in _HEALTHY_RMC
    if observed == "running" and rmc_down:
        return ["hmc_capture_lpar_console"]
    return []


async def inspect_lpar(
    hmc: Any,
    *,
    connection: str,
    admit: Admit,
    system: str,
    lpar: str,
    include: Sequence[Section],
) -> LparInspection:
    """Inspect one partition: the base read, then each section in *include*."""
    system_uuid, entry = await _partition(hmc, system, lpar)
    resource = entry.get("Resource") or {}
    uuid = str(entry["UUID"]).lower()
    name = _text(resource.get("PartitionName"))
    state = _text(resource.get("PartitionState"))
    rmc = _rmc(resource) if "rmc" in include else None
    return LparInspection(
        connection=connection,
        id=f"{connection}/{system_uuid}/{uuid}",
        system_uuid=system_uuid,
        uuid=uuid,
        name=name,
        partition_id=_int(resource.get("PartitionID")),
        state=state,
        rmc=rmc,
        resources=(
            await _resources(hmc, admit, system, system_uuid, entry)
            if "resources" in include
            else None
        ),
        refcodes=(
            await _refcodes(hmc, admit, system, lpar, name)
            if "refcodes" in include
            else None
        ),
        profile_drift=(
            SectionSource("unavailable", None, _PROFILE_DRIFT)
            if "profile_drift" in include
            else None
        ),
        next_actions=next_actions(state, rmc),
    )
