"""Read partition-shaped collections system by system across the fleet (ADR 0197)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hmcpctl.client.client_resolution import summary_for, system_operating
from hmcpctl.client.core import HMCClient
from hmcpctl.discovery_limits import MAX_PARENT_DISCOVERY_SYSTEMS
from hmcpctl.errors import HMCError
from hmcpctl.resource_identity import is_uuid


@dataclass(frozen=True)
class UnreadableSystem:
    """A managed system a listing skipped, with the state that made it skip."""

    system_name: str | None
    system_uuid: str | None
    state: str | None
    detailed_state: str | None


@dataclass(frozen=True)
class FleetListing:
    """Entries read, and every managed system whose entries are absent from them."""

    entries: list[dict[str, Any]]
    unreadable_systems: list[UnreadableSystem] = field(default_factory=list)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def unreadable_system(system: object) -> UnreadableSystem | None:
    """None when the quick/All *system* summary can be read; otherwise its record."""
    summary = system if isinstance(system, dict) else {}
    uuid = summary.get("UUID")
    if (
        isinstance(uuid, str)
        and is_uuid(uuid)
        and system_operating(summary.get("State"))
    ):
        return None
    return UnreadableSystem(
        system_name=_text(summary.get("SystemName")),
        system_uuid=_text(uuid),
        state=_text(summary.get("State")),
        detailed_state=_text(summary.get("StateDetail")),
    )


def sparse_system_entry(summary: object) -> dict[str, Any]:
    """A ``ManagedSystem`` entry built from a quick/All summary alone (ADR 0197).

    The same envelope a parsed entry has, carrying only ``SystemName``, ``State``
    and ``StateDetail``: its ``State`` says why the rest is absent.
    """
    fields = summary if isinstance(summary, dict) else {}
    return {
        "UUID": fields.get("UUID"),
        "title": None,
        "link": None,
        "ResourceType": "ManagedSystem",
        "Resource": {
            key: fields.get(key) for key in ("SystemName", "State", "StateDetail")
        },
    }


async def system_entry(hmc: HMCClient, summary: object) -> dict[str, Any]:
    """An operating system's full entry; any other system's sparse entry.

    ``ManagedSystem/{uuid}`` timed out for a system with no connection (#1501).
    An operating system whose entry reads back empty also gets the sparse one.
    """
    if not isinstance(summary, dict) or unreadable_system(summary) is not None:
        return sparse_system_entry(summary)
    entry = await hmc.get_managed_system(summary["UUID"])
    return entry if entry is not None else sparse_system_entry(summary)


async def read_system_entry(hmc: HMCClient, system_uuid: str) -> dict[str, Any] | None:
    """:func:`system_entry` for one UUID; None when quick/All does not list it."""
    summary = summary_for(await hmc.list_system_summaries(), system_uuid)
    return await system_entry(hmc, summary) if summary else None


async def operating_system_entry(
    hmc: HMCClient, system_uuid: str, reading: str
) -> dict[str, Any] | None:
    """The full entry of an operating system; refuse any other promptly.

    *reading* names what the caller needs the entry for, in the refusal. None
    when quick/All does not list the system.
    """
    summary = summary_for(await hmc.list_system_summaries(), system_uuid)
    if not summary:
        return None
    skipped = unreadable_system(summary)
    if skipped is not None:
        name = skipped.system_name
        label = system_uuid if name is None else f"{name!r} ({system_uuid})"
        raise HMCError(
            f"Cannot read {reading} of managed system {label}: it is in State "
            f"{skipped.state or 'unknown'!r}; retry when it reconnects"
        )
    return await hmc.get_managed_system(system_uuid)


async def _stopped_operating(hmc: HMCClient, uuid: str) -> UnreadableSystem | None:
    """The record for a system that left `operating` after the inventory read."""
    try:
        summaries = await hmc.list_system_summaries()
    except HMCError:
        return None
    for summary in summaries:
        if isinstance(summary, dict) and summary.get("UUID") == uuid:
            return unreadable_system(summary)
    return None


async def read_fleet(
    hmc: HMCClient,
    read_system: Callable[[str], Awaitable[list[dict[str, Any]]]],
    resources: str,
) -> FleetListing:
    """Read each operating system's scoped feed, naming every system skipped.

    The HMC-wide partition and VIOS feeds time out while any one system has no
    connection, where the other systems' scoped feeds still answer (#1293). So
    does the full ManagedSystem feed, so the systems come from quick/All (#1309).
    """
    systems = await hmc.list_system_summaries()
    if len(systems) > MAX_PARENT_DISCOVERY_SYSTEMS:
        raise ValueError(
            f"Cannot list {resources} across managed systems: discovery exceeds "
            f"{MAX_PARENT_DISCOVERY_SYSTEMS} managed systems; supply managed-system scope"
        )
    entries: list[dict[str, Any]] = []
    unreadable: list[UnreadableSystem] = []
    for system in systems:
        skipped = unreadable_system(system)
        if skipped is None:
            try:
                rows = await read_system(system["UUID"])
                entries.extend(
                    {
                        **entry,
                        "ManagedSystem": {
                            "UUID": system["UUID"],
                            "SystemName": system.get("SystemName"),
                        },
                    }
                    for entry in rows
                )
                continue
            except HMCError:
                skipped = await _stopped_operating(hmc, system["UUID"])
                if skipped is None:
                    raise
        unreadable.append(skipped)
    return FleetListing(entries, unreadable)
