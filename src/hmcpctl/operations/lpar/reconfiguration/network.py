"""``hmc_reconfigure_lpar``'s client network adapter legs (ADR 0325).

An add creates a client network adapter on a VLAN; a removal deletes the one adapter
carrying a MAC. Planning reads the partition's adapters once and the plan records what
the writes and classifiers need: each removal's adapter UUID and slot, and for an add
without a pinned MAC the UUIDs already on its VLAN. Adds run before removals, so no new
adapter can take a slot a removal freed and a removed slot left in the profile always
means the save did not land. Messages name partitions by UUID only.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from hmcpctl.operations.lpar.provisioning.core import check_vlan_exists, mac_colon_form

from ....client.core import HMCClient
from ....errors import HMCError
from ....xmlutil import leaf_text
from ...logical import engine
from ...logical.engine import (
    Classification,
    EffectNotApplied,
    OperationContext,
    OperationFailed,
)
from ...logical.models import EffectRecord
from ...virtualization.adapters import classified_add_refusal

if TYPE_CHECKING:
    from .core import PartitionView

# Persisted contract (ADR 0195 Consequences): never change while an operation can be open.
ADD_KIND = "lpar.client_network_adapter"
REMOVE_KIND = "lpar.client_network_adapter_remove"
# ADR 0325 Decision 6: the specialists an add or a removal uses.
ADD_TOOLS = ("hmc_add_network_adapter", "hmc_save_current_partition_profile")
REMOVE_TOOLS = ("hmc_delete_adapter", "hmc_save_current_partition_profile")
MAX_NETWORKS = 4
_NOT_ACTIVATED = "not activated"
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
Write = Callable[[], Awaitable[Mapping[str, Any]]]
Opener = Callable[[str, str], AbstractAsyncContextManager[HMCClient]]


class NetworkAddPlan(TypedDict):
    port_vlan_id: int
    mac: str | None
    baseline: list[str] | None


class NetworkRemovePlan(TypedDict):
    mac: str
    adapter_uuid: str
    port_vlan_id: str | None
    slot: str


class NetworkPlan(TypedDict):
    add: list[NetworkAddPlan]
    remove: list[NetworkRemovePlan]


PlanOf = Callable[[OperationContext], tuple[str, NetworkPlan | None]]


def _unicast(mac: str) -> str:
    """*mac* when it is a unicast lower-case colon MAC (``provisioning.media.check_mac``'s rule)."""
    if not _MAC.fullmatch(mac) or int(mac[:2], 16) & 1:
        raise ValueError("a MAC must be a unicast address in lower-case colon form")
    return mac


class NetworkAdd(BaseModel):
    """A client network adapter to create on a VLAN."""

    model_config = ConfigDict(extra="forbid")
    port_vlan_id: StrictInt = Field(
        ge=1,
        le=4094,
        description="Port VLAN of the new adapter; a virtual network must exist on it.",
    )
    mac: StrictStr | None = Field(
        default=None,
        description="MAC to create the adapter with, lower-case colon form; the HMC "
        "chooses one when omitted.",
    )

    @model_validator(mode="after")
    def _mac_form(self) -> NetworkAdd:
        if self.mac is not None:
            _unicast(self.mac)
        return self


class NetworkRemove(BaseModel):
    """The one client network adapter, selected by MAC, to delete."""

    model_config = ConfigDict(extra="forbid")
    mac: StrictStr = Field(
        description="MAC of the adapter to remove, lower-case colon form."
    )

    @model_validator(mode="after")
    def _mac_form(self) -> NetworkRemove:
        _unicast(self.mac)
        return self


class NetworkPatch(BaseModel):
    """Client network adapters to add and to remove (ADR 0325 Decision 1)."""

    model_config = ConfigDict(extra="forbid")
    add: list[NetworkAdd] = Field(
        default_factory=list,
        max_length=MAX_NETWORKS,
        description="Up to 4 adapters to create and save into the default profile.",
    )
    remove: list[NetworkRemove] = Field(
        default_factory=list,
        max_length=MAX_NETWORKS,
        description="Up to 4 adapters, each selected by MAC, to delete and drop from "
        "the default profile.",
    )

    @model_validator(mode="after")
    def _distinct(self) -> NetworkPatch:
        if not self.add and not self.remove:
            raise ValueError("networks needs at least one add or remove")
        macs = [i.mac for i in (*self.add, *self.remove) if i.mac is not None]
        if len(set(macs)) != len(macs):
            raise ValueError("networks names the same MAC twice")
        vlan_counts = Counter(item.port_vlan_id for item in self.add)
        if any(
            item.mac is None and vlan_counts[item.port_vlan_id] > 1 for item in self.add
        ):
            raise ValueError(
                "an add without mac must not share its VLAN with another add"
            )
        return self


def _text(entry: Mapping[str, Any], name: str) -> str | None:
    value = leaf_text((entry.get("Resource") or {}).get(name))
    return value.strip() if isinstance(value, str) else None


def _adapter(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The UUID, MAC, VLAN and slot of one adapter entry; a missing field is None."""
    uuid = entry.get("UUID")
    return {
        "adapter_uuid": uuid.lower() if isinstance(uuid, str) and uuid else None,
        "mac": mac_colon_form((entry.get("Resource") or {}).get("MACAddress")),
        "port_vlan_id": _text(entry, "PortVLANID"),
        "slot": _text(entry, "VirtualSlotNumber"),
    }


async def current_adapters(hmc: HMCClient, lpar_uuid: str) -> list[dict[str, Any]]:
    """The partition's client network adapters, from a complete feed (absence is proof).

    An empty collection (HTTP 204) is no adapters; a paged feed is refused.
    """
    feed = await hmc._network_adapter_creation_feed(lpar_uuid, allow_empty=True)
    return [_adapter(entry) for entry in feed if isinstance(entry, dict)]


def _on_vlan(adapters: list[dict[str, Any]], vlan: int) -> list[dict[str, Any]]:
    return [a for a in adapters if a["port_vlan_id"] == str(vlan)]


async def plan_networks(
    hmc: HMCClient,
    networks: NetworkPatch,
    view: PartitionView,
    system_uuid: str,
    lpar_uuid: str,
    *,
    allow_disruption: bool,
) -> NetworkPlan:
    """The network writes *networks* needs, checked against live state (ADR 0325)."""
    if view["state"] != _NOT_ACTIVATED:
        if (view["state"], view["rmc"]) != ("running", "active"):
            raise OperationFailed(
                f"LPAR {lpar_uuid} is {view['state']!r} with RMC "
                f"{view['rmc'] or 'unknown'!r}: a network change to an activated "
                "partition needs it running with RMC active, or the HMC can leave it "
                "half applied. Power the partition off, then retry"
            )
        if networks.remove and not allow_disruption:
            raise OperationFailed(
                f"a network removal from activated LPAR {lpar_uuid} cuts the guest's "
                "traffic on that adapter; pass allow_disruption=true"
            )
    try:
        adapters = await current_adapters(hmc, lpar_uuid)
        for vlan in dict.fromkeys(item.port_vlan_id for item in networks.add):
            await check_vlan_exists(hmc, system_uuid, vlan)
    except ValueError as exc:
        raise OperationFailed(
            f"networks.add names a VLAN with no virtual network on managed system "
            f"{system_uuid}; list them with hmc_list_virtual_networks"
        ) from exc
    except HMCError as exc:
        raise OperationFailed(
            f"the client network adapters of LPAR {lpar_uuid} or the virtual networks "
            f"could not be read ({type(exc).__name__})"
        ) from exc
    known = {a["mac"] for a in adapters}
    adds: list[NetworkAddPlan] = []
    for index, item in enumerate(networks.add):
        if item.mac is not None and item.mac in known:
            raise OperationFailed(
                f"networks.add[{index}]: LPAR {lpar_uuid} already has an adapter with "
                "that MAC"
            )
        baseline = sorted(
            str(a["adapter_uuid"]) for a in _on_vlan(adapters, item.port_vlan_id)
        )
        adds.append(
            {
                "port_vlan_id": item.port_vlan_id,
                "mac": item.mac,
                "baseline": None if item.mac is not None else baseline,
            }
        )
    removes: list[NetworkRemovePlan] = []
    for index, item in enumerate(networks.remove):
        hits = [a for a in adapters if a["mac"] == item.mac]
        if len(hits) != 1 or hits[0]["adapter_uuid"] is None:
            raise OperationFailed(
                f"networks.remove[{index}]: LPAR {lpar_uuid} has {len(hits)} client "
                "network adapters with that MAC; exactly one is needed"
            )
        if hits[0]["slot"] is None:
            raise OperationFailed(
                f"networks.remove[{index}]: adapter {hits[0]['adapter_uuid']} reports no "
                "virtual slot, so its removal from the profile could not be confirmed"
            )
        removes.append(
            {
                "mac": item.mac,
                "adapter_uuid": hits[0]["adapter_uuid"],
                "port_vlan_id": hits[0]["port_vlan_id"],
                "slot": hits[0]["slot"],
            }
        )
    return {"add": adds, "remove": removes}


def network_changes(planned: NetworkPlan, active: bool) -> list[dict[str, Any]]:
    """The ``changes[]`` entries of the planned adds and removals."""
    applied = "live" if active else "profile"
    changes = [
        {
            "field": "networks.add",
            "before": None,
            "after": {"port_vlan_id": add["port_vlan_id"], "mac": add["mac"]},
            "applied": applied,
            "reason": None,
        }
        for add in planned["add"]
    ]
    changes += [
        {
            "field": "networks.remove",
            "before": {"mac": item["mac"], "port_vlan_id": item["port_vlan_id"]},
            "after": None,
            "applied": applied,
            "reason": None,
        }
        for item in planned["remove"]
    ]
    return changes


def _planned(networks: NetworkPlan | None) -> NetworkPlan:
    """The plan's network section; a plan recorded before ADR 0325 has none."""
    return networks or {"add": [], "remove": []}


def steps(networks: NetworkPlan | None, lpar_uuid: str) -> list[tuple[str, str, str]]:
    """The network writes, adds before removals (ADR 0325 Decision 5)."""
    target = f"lpar:{lpar_uuid}"
    planned = _planned(networks)
    return [
        (f"network_add:{index}", ADD_KIND, target)
        for index in range(len(planned["add"]))
    ] + [
        (f"network_remove:{index}", REMOVE_KIND, target)
        for index in range(len(planned["remove"]))
    ]


def removed_slots(networks: NetworkPlan | None) -> tuple[str, ...]:
    """The virtual slots the plan's removals free, for the save's readback."""
    return tuple(
        str(item["slot"]) for item in _planned(networks)["remove"] if item["slot"]
    )


def adds(networks: NetworkPlan | None) -> bool:
    return bool(_planned(networks)["add"])


def _index(key: str) -> int:
    return int(key.rpartition(":")[2])


def _matches(add: NetworkAddPlan, adapters: list[dict[str, Any]]) -> list[dict]:
    """The listed adapters that can be *add*'s: by its MAC, else new on its VLAN."""
    if add["mac"] is not None:
        return [a for a in adapters if a["mac"] == add["mac"]]
    baseline = set(add["baseline"] or ())
    return [
        a
        for a in _on_vlan(adapters, add["port_vlan_id"])
        if a["adapter_uuid"] not in baseline
    ]


def _identity(adapter: Mapping[str, Any]) -> dict[str, Any] | None:
    if adapter["adapter_uuid"] is None or adapter["mac"] is None:
        return None
    return dict(adapter)


def _add_write(hmc: HMCClient, lpar_uuid: str, add: NetworkAddPlan) -> Write:
    async def write() -> Mapping[str, Any]:
        mac = add["mac"]
        try:
            created = await hmc.add_network_adapter(
                lpar_uuid,
                add["port_vlan_id"],
                mac_address=mac.replace(":", "") if mac else None,
            )
        except ValueError as exc:
            raise EffectNotApplied(
                f"the adapter add to LPAR {lpar_uuid} was refused before submission"
            ) from exc
        except HMCError as exc:
            refusal = classified_add_refusal(exc, False)
            if refusal is None:
                raise
            raise EffectNotApplied(refusal) from exc
        identity = _identity(_adapter(created or {}))
        if identity is None:
            found = _matches(add, await current_adapters(hmc, lpar_uuid))
            identity = _identity(found[0]) if len(found) == 1 else None
        if identity is None:
            raise HMCError(
                f"the adapter added to LPAR {lpar_uuid} could not be identified; "
                "resume to classify it"
            )
        return identity

    return write


def _remove_write(hmc: HMCClient, lpar_uuid: str, item: NetworkRemovePlan) -> Write:
    uuid = item["adapter_uuid"]
    identity = {"adapter_uuid": uuid, "mac": item["mac"], "slot": item["slot"]}

    async def write() -> Mapping[str, Any]:
        listed = _listed(await current_adapters(hmc, lpar_uuid), uuid)
        if listed is None:
            return identity
        if listed["mac"] != item["mac"]:
            raise EffectNotApplied(
                f"adapter {uuid} of LPAR {lpar_uuid} no longer carries the planned MAC; "
                "nothing was removed"
            )
        try:
            await hmc.delete_adapter(lpar_uuid, "ClientNetworkAdapter", uuid)
        except HMCError as exc:
            try:
                left = _listed(await current_adapters(hmc, lpar_uuid), uuid)
            except HMCError:
                raise exc from None
            if left is not None:
                raise
            return {**identity, "error_status": exc.status_code}
        return identity

    return write


def _listed(adapters: list[dict[str, Any]], uuid: str) -> dict[str, Any] | None:
    return next((a for a in adapters if a["adapter_uuid"] == uuid), None)


def write_for(
    hmc: HMCClient, networks: NetworkPlan | None, lpar_uuid: str, key: str
) -> Write:
    """The write of the plan's network step *key*."""
    planned = _planned(networks)
    if key.startswith("network_add:"):
        return _add_write(hmc, lpar_uuid, planned["add"][_index(key)])
    return _remove_write(hmc, lpar_uuid, planned["remove"][_index(key)])


def add_classifier(plan_of: PlanOf, opener: Opener) -> engine.Classifier:
    """``lpar.client_network_adapter`` by the pinned MAC, else new adapters on the VLAN."""

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, networks = plan_of(ctx)
        add = _planned(networks)["add"][_index(effect.key)]
        async with opener(ctx.connection, ctx.host) as hmc:
            found = _matches(add, await current_adapters(hmc, lpar_uuid))
        if not found:
            return Classification("not_applied")
        identity = _identity(found[0]) if len(found) == 1 else None
        if identity is None:
            return Classification(
                "needs_attention",
                reason=f"LPAR {lpar_uuid} has {len(found)} candidate adapters "
                "for this add, or one without a UUID or MAC; inspect it",
            )
        return Classification("applied", identity)

    return classify


def remove_classifier(plan_of: PlanOf, opener: Opener) -> engine.Classifier:
    """``lpar.client_network_adapter_remove`` by whether the planned UUID is listed."""

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, networks = plan_of(ctx)
        item = _planned(networks)["remove"][_index(effect.key)]
        async with opener(ctx.connection, ctx.host) as hmc:
            adapters = await current_adapters(hmc, lpar_uuid)
        listed = _listed(adapters, item["adapter_uuid"])
        if listed is None:
            return Classification(
                "applied",
                {
                    "adapter_uuid": item["adapter_uuid"],
                    "mac": item["mac"],
                    "slot": item["slot"],
                },
            )
        if listed["mac"] == item["mac"]:
            return Classification("not_applied")
        return Classification(
            "needs_attention",
            reason=f"adapter {item['adapter_uuid']} of LPAR {lpar_uuid} now "
            "carries another MAC; inspect it",
        )

    return classify


async def observed_networks(
    ctx: OperationContext, hmc: HMCClient, networks: NetworkPlan | None, lpar_uuid: str
) -> tuple[bool, dict[str, Any]]:
    """Whether every add is listed and every removal gone, and the MACs read."""
    planned = _planned(networks)
    if not planned["add"] and not planned["remove"]:
        return True, {}
    adapters = await current_adapters(hmc, lpar_uuid)
    uuids = {a["adapter_uuid"] for a in adapters}
    added = []
    for index in range(len(planned["add"])):
        recorded = ctx.recorded(f"network_add:{index}")
        added.append(
            ((recorded.identity or {}) if recorded else {}).get("adapter_uuid")
        )
    done = all(uuid in uuids for uuid in added) and not any(
        item["adapter_uuid"] in uuids for item in planned["remove"]
    )
    return done, {"networks": sorted(str(a["mac"]) for a in adapters)}


def warnings(
    ctx: OperationContext,
    networks: NetworkPlan | None,
    lpar_uuid: str,
    saved: bool,
    *,
    save_profile: str | None,
) -> list[str]:
    """Network warnings: a delete error after removal, and changes left unsaved."""
    planned = _planned(networks)
    notes = []
    for index in range(len(planned["remove"])):
        recorded = ctx.recorded(f"network_remove:{index}")
        status = ((recorded.identity or {}) if recorded else {}).get("error_status")
        if status is not None:
            notes.append(
                f"the HMC answered HTTP {status} to the removal of adapter "
                f"{planned['remove'][index]['adapter_uuid']} after deleting it; inspect "
                f"LPAR {lpar_uuid}"
            )
    touched = any(
        (r := ctx.recorded(key)) is not None and r.status != "not_applied"
        for key, _, _ in steps(networks, lpar_uuid)
    )
    if touched and not saved:
        notes.append(
            "the network changes are in the current configuration only: resume to save "
            f"them into profile {save_profile}; abandon leaves the profile as it was, "
            "so a profile activation drops the added adapters and restores the removed ones"
        )
    return notes
