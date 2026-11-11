"""Where an adapter or mapping change lives, and which adapters a profile lacks.

A REST adapter or mapping write changes the partition's current configuration.
The HMC copies it into the partition's current profile only while the
partition's ``CurrentProfileSync`` is ``On``; ``Disabled`` and ``Suspended``
leave the profile untouched, so activating that profile drops the change (#981).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError, HMCTLSFingerprintError

ChangeLivesIn = Literal[
    "current-configuration", "current-configuration-and-profile", "unknown"
]

# The client adapter types a partition profile can carry that the REST adapter
# and mapping commands create (#981); vNIC is not one of them.
PROFILE_CHECKED_ADAPTER_TYPES = (
    "VirtualSCSIClientAdapter",
    "VirtualFibreChannelClientAdapter",
    "ClientNetworkAdapter",
)


@dataclass(frozen=True)
class ChangeLocation:
    """The partition's sync setting and where a current-configuration change lives."""

    current_profile_sync: str | None
    lives_in: ChangeLivesIn
    profile_name: str | None = None

    def summary(self) -> str:
        sync = f"CurrentProfileSync is {self.current_profile_sync or 'not reported'}"
        if self.lives_in == "current-configuration-and-profile":
            named = f" '{self.profile_name}'" if self.profile_name else ""
            return (
                f"{sync}: the change lives in the current configuration and the HMC "
                f"also writes it to the partition's current profile{named}."
            )
        if self.lives_in == "current-configuration":
            return (
                f"{sync}: the change lives only in the current configuration; "
                "activating a partition profile that lacks it discards it."
            )
        return f"{sync}: whether the change reaches a partition profile is unknown."


def resource_with_change_location(
    resource: Mapping[str, Any] | None, location: ChangeLocation
) -> dict[str, Any]:
    """The HMC resource plus a ``change_location`` key.

    The MCP add and mount tools return the resource itself, and existing readers
    (``scripts/live_test``) take its identity from top-level keys, so the
    location rides beside those keys rather than wrapping them.
    """
    return {**(resource or {}), "change_location": asdict(location)}


def _text(value: Any) -> str | None:
    """Leaf text, including a leaf whose kept attributes wrapped it in a dict."""
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def change_location_of(entry: dict[str, Any] | None) -> ChangeLocation:
    """Classify a partition entry's ``CurrentProfileSync``.

    Accepts a GET or a write's response entry; an absent entry is ``unknown``.
    """
    resource = (entry or {}).get("Resource") or {}
    sync = _text(resource.get("CurrentProfileSync"))
    folded = (sync or "").casefold()
    if folded == "on":
        # The HMC syncs to the partition's current profile, the one it last
        # activated or applied.
        profile = _text(resource.get("LastActivatedProfile"))
        return ChangeLocation(sync, "current-configuration-and-profile", profile)
    if folded in {"disabled", "suspended"}:
        return ChangeLocation(sync, "current-configuration")
    return ChangeLocation(sync, "unknown")


async def read_change_location(hmc: HMCClient, lpar_uuid: str) -> ChangeLocation:
    """Read the partition's ``CurrentProfileSync`` once and classify it.

    Callers read it before their write, so a failed read fails the command
    before anything changes rather than hiding a completed write.
    """
    return change_location_of(await hmc.get_logical_partition(lpar_uuid))


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _profile_slots(profile: dict[str, Any]) -> set[str]:
    """Virtual slots of the profile's adapters, whatever their subclass name.

    The adapters sit under ``IOConfigurationInstance`` (V10R3, 2026-09-24).
    Only each adapter's direct ``VirtualSlotNumber`` counts; a nested partner
    slot must not make a missing client slot look present.
    """
    io_configuration = (profile.get("Resource") or {}).get("IOConfigurationInstance")
    adapters = (
        io_configuration.get("ProfileVirtualIOAdapters")
        if isinstance(io_configuration, dict)
        else None
    )
    if not isinstance(adapters, dict):
        return set()
    slots: set[str] = set()
    for subclass in _as_list(adapters.get("ProfileVirtualIOAdapterSubclass")):
        if not isinstance(subclass, dict):
            continue
        for value in subclass.values():
            slots.update(
                slot
                for adapter in _as_list(value)
                if isinstance(adapter, dict)
                and (slot := _text(adapter.get("VirtualSlotNumber"))) is not None
            )
    return slots


async def adapters_missing_from_profile(
    hmc: HMCClient, lpar_uuid: str, profile: dict[str, Any]
) -> list[str]:
    """Describe each current client adapter whose virtual slot the profile lacks.

    An adapter reporting no slot cannot be matched, so it counts as missing:
    a false warning costs less than a missed one.
    """
    slots = _profile_slots(profile)
    missing: list[str] = []
    for adapter_type in PROFILE_CHECKED_ADAPTER_TYPES:
        # The child feed, not list_adapters: the HMC answers a partition's empty
        # collection with 204, which the feed reads as no adapter, while the network
        # creation feed refuses it as missing creation-identity proof (#1846).
        for adapter in await hmc.list_child(
            "LogicalPartition", lpar_uuid, adapter_type
        ):
            slot = _text((adapter.get("Resource") or {}).get("VirtualSlotNumber"))
            if slot is None or slot not in slots:
                missing.append(
                    f"{adapter_type} in virtual slot {slot or 'not reported'}"
                )
    return missing


async def profile_adapter_warnings(
    hmc: HMCClient, lpar_uuid: str, profile: dict[str, Any]
) -> tuple[str, ...]:
    """Power-on warnings for adapters activating *profile* would remove.

    Ordinary adapter-feed failures become advisory warnings. A TLS certificate
    identity failure aborts activation before any PowerOn submission.
    """
    try:
        missing = await adapters_missing_from_profile(hmc, lpar_uuid, profile)
    except HMCTLSFingerprintError:
        raise
    except HMCError as exc:
        return (f"Partition profile adapter check not run: {exc}",)
    return tuple(
        f"The partition profile lacks the current configuration's {adapter}; "
        "activating it removes that adapter. The job is still submitted: recreate "
        "the adapter and add it to the profile before the next profile activation, "
        "or power on without a partition profile."
        for adapter in missing
    )
