"""``delete_owned`` storage cleanup for ``hmc_decommission_lpar`` (ADR 0192, ADR 0323).

Ownership is the ADR 0190 resource ledger and nothing else: a disk or medium is a
candidate only when a ledger entry names it for the decommissioned partition, and it is
deleted only when one read pass over every VIOS shows no mapping to another client.
Messages name UUIDs and resource names only (``engine`` module note).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from typing_extensions import TypedDict

from ...client.client_storage import mapping_lpar_uuid, storage_mapping_id
from ...client.core import HMCClient
from ...errors import HMCError
from ...resource_identity import is_uuid
from ...xmlutil import leaf_text
from ..logical import engine, store
from ..logical.engine import Classification, EffectNotApplied, OperationContext
from ..logical.models import EffectRecord
from .decommission import (
    EFFECT_KINDS,
    Write,
    _records,
    _text,
    _vscsi_detach_inventory,
    unmap_key,
)

ClientFactory = Callable[[str], Any]
DISK_LEDGER = "virtual_disk"
MEDIA_LEDGER = "installer_media"
# Persisted contract (ADR 0323 Decision 3): never change while an operation can be open.
DELETE_KINDS: Mapping[str, str] = {
    DISK_LEDGER: "vios.virtual_disk_delete",
    MEDIA_LEDGER: "vios.optical_media_delete",
}
_KEY_PREFIX = {DISK_LEDGER: "disk", MEDIA_LEDGER: "media"}
_STORAGE_SECTIONS = (
    ("VirtualDisk", "DiskName", DISK_LEDGER),
    ("VirtualOpticalMedia", "MediaName", MEDIA_LEDGER),
    ("PhysicalVolume", "VolumeName", None),
    ("LogicalUnit", "UnitName", None),
)
INCOMPLETE = "inventory_incomplete"


class DecommissionStorage(TypedDict):
    """What ``delete_owned`` deleted, retained (with a reason) and has yet to delete."""

    deleted: tuple[dict[str, str], ...]
    retained: tuple[dict[str, str], ...]
    pending: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class OwnedItem:
    """One ledgered disk (``vg_uuid`` set) or medium (``vg_uuid`` None) of a partition."""

    kind: str
    vios_uuid: str
    name: str
    vg_uuid: str | None = None

    def identity(self) -> dict[str, str]:
        """The ADR 0313 Decision 6 identity, which the ledger rows hold."""
        if self.kind == DISK_LEDGER:
            return {
                "vios_uuid": self.vios_uuid,
                "vg_uuid": self.vg_uuid or "",
                "disk_name": self.name,
            }
        return {"vios_uuid": self.vios_uuid, "media_name": self.name}

    def entry(self, **extra: str) -> dict[str, str]:
        return {"kind": self.kind, **self.identity(), **extra}


@dataclass(frozen=True)
class Plan:
    """An item the pass found deletable: its group and this partition's mappings to it."""

    item: OwnedItem
    vg_uuid: str
    unmaps: tuple[str, ...]


@dataclass(frozen=True)
class Assessment:
    plans: tuple[Plan, ...]
    retained: tuple[dict[str, str], ...]


class CleanupJournal(Protocol):
    """The journal cleanup writes through; ``OperationContext`` is one."""

    def recorded(self, key: str) -> EffectRecord | None: ...

    async def effect(
        self, key: str, kind: str, target: str, write: Write
    ) -> Mapping[str, Any]: ...

    def ledger_remove(
        self,
        kind: str,
        system_uuid: str,
        partition_uuid: str,
        match: Mapping[str, Any],
    ) -> int: ...


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def delete_key(item: OwnedItem) -> str:
    """The effect key of one delete, keyed by the resource (ADR 0323 Decision 3)."""
    if item.kind == DISK_LEDGER:
        return f"disk.{_digest(item.vios_uuid, item.vg_uuid or '', item.name)}"
    return f"media.{_digest(item.vios_uuid, item.name)}"


def is_cleanup_key(key: str) -> bool:
    """Whether *key* is a cleanup delete, which only ``storage`` reports."""
    return key.startswith(tuple(f"{prefix}." for prefix in _KEY_PREFIX.values()))


def _target(item: OwnedItem, vg_uuid: str) -> str:
    noun = "disk" if item.kind == DISK_LEDGER else "media"
    return f"vios:{item.vios_uuid}/vg:{vg_uuid}/{noun}:{item.name}"


def _item(kind: str, identity: Mapping[str, Any]) -> OwnedItem | None:
    vios = identity.get("vios_uuid")
    if kind == DISK_LEDGER:
        name, group = identity.get("disk_name"), identity.get("vg_uuid")
    else:
        name, group = identity.get("media_name"), None
    if not isinstance(vios, str) or not isinstance(name, str) or not name:
        return None
    if kind == DISK_LEDGER and not isinstance(group, str):
        return None
    return OwnedItem(kind, vios.lower(), name, group.lower() if group else None)


def owned_items(
    system_uuid: str, partition_uuid: str
) -> tuple[tuple[OwnedItem, ...], tuple[dict[str, str], ...]]:
    """The partition's ledgered items, and those retained because another partition's
    entry names them too (ADR 0323 Decision 1). No store means nothing is owned."""
    partition = partition_uuid.lower()
    with store.read_session() as conn:
        rows = (
            []
            if conn is None
            else [
                (kind, entry)
                for kind in (DISK_LEDGER, MEDIA_LEDGER)
                for entry in store.ledger_entries(conn, kind, system_uuid)
            ]
        )
    mine: dict[OwnedItem, None] = {}
    elsewhere: set[OwnedItem] = set()
    unreadable: list[dict[str, str]] = []
    for kind, (_operation, lpar, identity) in rows:
        item = _item(kind, identity)
        ours = (lpar or "").lower() == partition
        if item is None:
            if ours:
                unreadable.append({"kind": kind, "reason": "unreadable_record"})
            continue
        if ours:
            mine[item] = None
        else:
            elsewhere.add(item)
    retained = [i.entry(reason="ledgered_elsewhere") for i in mine if i in elsewhere]
    owned = tuple(i for i in mine if i not in elsewhere)
    return owned, (*unreadable, *retained)


def _reference(storage: Any) -> tuple[str | None, str, str | None] | None:
    """(ledger kind or None, name, linked group UUID) of a mapping's backing storage;
    None when the storage element names nothing this pass can classify."""
    if not isinstance(storage, dict):
        return None
    for section, field, kind in _STORAGE_SECTIONS:
        device = storage.get(section)
        if not isinstance(device, dict):
            continue
        name = _text(leaf_text(device.get(field)))
        if name is None:
            return None
        group = device.get("VolumeGroup")
        link = group.get("href", "") if isinstance(group, dict) else ""
        tail = link.rstrip("/").rpartition("/")[2].lower() if link else None
        return kind, name, tail or None
    return None


def _references(
    details: Mapping[str, dict[str, Any]], partition_uuid: str
) -> dict[tuple[str, str, str], list[tuple[str, str | None, str | None]]] | None:
    """Every vSCSI mapping's reference, keyed (VIOS, kind, name) with (client, mapping
    id, linked group); None when a mapping is too sparse to classify (ADR 0192 D4).

    Teardown's completeness check comes first, so a malformed collection, an unverified
    client or a duplicate mapping id of this partition is incomplete here too.
    """
    found: dict[tuple[str, str, str], list[tuple[str, str | None, str | None]]] = {}
    for vios_uuid, resource in details.items():
        if _vscsi_detach_inventory(resource, vios_uuid, partition_uuid)[1]:
            return None
        block = resource["VirtualSCSIMappings"]
        members = block.get("VirtualSCSIMapping") if isinstance(block, dict) else None
        for mapping in _records(members, "VirtualSCSIMapping"):
            client = mapping_lpar_uuid(mapping) or ""
            storage = mapping.get("Storage")
            if storage in (None, "", {}):
                continue
            reference = _reference(storage)
            if reference is None:
                return None
            kind, name, group = reference
            if kind is not None:
                found.setdefault((vios_uuid, kind, name), []).append(
                    (client.lower(), storage_mapping_id(mapping), group)
                )
    return found


async def _vios_details(
    hmc: HMCClient, system_uuid: str
) -> dict[str, dict[str, Any]] | None:
    """Every VIOS's storage detail, or None when any one is unreadable."""
    try:
        listed = await hmc.list_vios(system_uuid)
        details = {}
        for vios in listed:
            uuid = _text(vios.get("UUID"))
            if uuid is None or not is_uuid(uuid):
                return None
            detail = await hmc.get_vios_storage_detail(uuid)
            resource = (detail or {}).get("Resource")
            if not isinstance(resource, dict):
                return None
            details[uuid.lower()] = resource
    except HMCError:
        return None
    return details


def _disk_count(group: Mapping[str, Any] | None, name: str) -> int:
    resource = (group or {}).get("Resource")
    disks = resource.get("VirtualDisks") if isinstance(resource, dict) else None
    return sum(
        leaf_text(disk.get("DiskName")) == name
        for disk in _records(disks, "VirtualDisk")
    )


def _holds_repository(group: Mapping[str, Any]) -> bool:
    resource = group.get("Resource")
    repositories = (
        resource.get("MediaRepositories") if isinstance(resource, dict) else None
    )
    return isinstance(repositories, dict) and "VirtualMediaRepository" in repositories


async def repository_group(hmc: HMCClient, vios_uuid: str) -> str | None:
    """The VIOS's one repository-bearing volume group, or None for none or several."""
    groups = [
        str(group.get("UUID") or "").lower()
        for group in await hmc.list_volume_groups(vios_uuid)
        if _holds_repository(group)
    ]
    return groups[0] if len(groups) == 1 and groups[0] else None


async def _media_count(hmc: HMCClient, vios_uuid: str, vg_uuid: str, name: str) -> int:
    media = await hmc.list_optical_media(vios_uuid, vg_uuid)
    return sum(leaf_text(item.get("MediaName")) == name for item in media)


async def _locate(hmc: HMCClient, item: OwnedItem) -> tuple[str | None, int]:
    """The item's group and how many entries carry its name there (group None:
    no single repository). Raises HMCError when the group cannot be read."""
    if item.kind == DISK_LEDGER:
        assert item.vg_uuid is not None
        group = await hmc.get_volume_group(item.vios_uuid, item.vg_uuid)
        if group is None:
            raise HMCError(f"volume group {item.vg_uuid} returned nothing")
        return item.vg_uuid, _disk_count(group, item.name)
    repository = await repository_group(hmc, item.vios_uuid)
    if repository is None:
        return None, 0
    return repository, await _media_count(hmc, item.vios_uuid, repository, item.name)


def _verdict(
    item: OwnedItem,
    group: str | None,
    count: int,
    refs: list[tuple[str, str | None, str | None]],
    partition: str,
) -> Plan | dict[str, str] | None:
    """A plan, a retained entry, or None when a mapping to this partition has no id."""
    if group is None:
        return item.entry(reason="repository_unresolved")
    if count != 1:
        return item.entry(reason="absent" if count == 0 else "ambiguous")
    unmaps = []
    for client, mapping_id, linked in refs:
        if item.kind == DISK_LEDGER and linked is not None and linked != group:
            continue
        if client != partition:
            return item.entry(reason="referenced")
        if mapping_id is None:
            return None
        unmaps.append(mapping_id)
    return Plan(item, group, tuple(unmaps))


async def assess(
    hmc: HMCClient, system_uuid: str, partition_uuid: str, items: Iterable[OwnedItem]
) -> Assessment:
    """The single pass of ADR 0323 Decision 2 over *items*."""
    items = tuple(items)
    if not items:
        return Assessment((), ())
    incomplete = Assessment((), tuple(i.entry(reason=INCOMPLETE) for i in items))
    details = await _vios_details(hmc, system_uuid)
    if details is None or any(i.vios_uuid not in details for i in items):
        return incomplete
    references = _references(details, partition_uuid)
    if references is None:
        return incomplete
    plans: list[Plan] = []
    retained: list[dict[str, str]] = []
    for item in items:
        try:
            group, count = await _locate(hmc, item)
        except HMCError:
            return incomplete
        verdict = _verdict(
            item,
            group,
            count,
            references.get((item.vios_uuid, item.kind, item.name), []),
            partition_uuid.lower(),
        )
        if verdict is None:
            return incomplete
        if isinstance(verdict, Plan):
            plans.append(verdict)
        else:
            retained.append(verdict)
    return Assessment(tuple(plans), tuple(retained))


def _delete_write(hmc: HMCClient, plan: Plan) -> Write:
    item = plan.item

    async def write() -> Mapping[str, Any]:
        try:
            if item.kind == DISK_LEDGER:
                await hmc.delete_virtual_disk(item.vios_uuid, plan.vg_uuid, item.name)
            else:
                await hmc.delete_optical_media(item.vios_uuid, plan.vg_uuid, item.name)
        except HMCError as exc:
            if exc.status_code == 412:
                raise EffectNotApplied(
                    f"the volume group {plan.vg_uuid} changed before the delete (HTTP 412)"
                ) from exc
            raise
        return {**item.identity(), "vg_uuid": plan.vg_uuid}

    return write


def _unmap_write(
    hmc: HMCClient, vios_uuid: str, mapping_id: str, partition_uuid: str
) -> Write:
    async def write() -> Mapping[str, Any]:
        await hmc.delete_storage_mapping(vios_uuid, mapping_id, partition_uuid)
        return {"vios_uuid": vios_uuid, "mapping_id": mapping_id}

    return write


async def clean_storage(
    hmc: HMCClient,
    journal: CleanupJournal,
    system_uuid: str,
    partition_uuid: str,
    *,
    authorized_vios: frozenset[str] | None,
) -> tuple[dict[str, str], ...]:
    """Delete the partition's deletable owned items; return the retained ones.

    Raises ``engine.OperationFailed`` after every item was tried when a delete got HTTP
    412 (ADR 0323 Decision 3); any other HMC error propagates with its effect open.
    """
    items, retained = owned_items(system_uuid, partition_uuid)
    done = [i for i in items if _applied(journal.recorded(delete_key(i)))]
    for item in done:
        # A run that stopped between the delete and the ledger write: finish the write.
        journal.ledger_remove(item.kind, system_uuid, partition_uuid, item.identity())
    items = tuple(i for i in items if i not in done)
    allowed = tuple(
        i for i in items if authorized_vios is None or i.vios_uuid in authorized_vios
    )
    kept = [*retained]
    kept.extend(i.entry(reason="not_authorized") for i in items if i not in allowed)
    assessment = await assess(hmc, system_uuid, partition_uuid, allowed)
    kept.extend(assessment.retained)
    refused = []
    for plan in assessment.plans:
        item = plan.item
        for mapping_id in plan.unmaps:
            await journal.effect(
                unmap_key(item.vios_uuid, mapping_id),
                EFFECT_KINDS["unmap"],
                f"vios:{item.vios_uuid}/mapping:{mapping_id}",
                _unmap_write(hmc, item.vios_uuid, mapping_id, partition_uuid),
            )
        try:
            await journal.effect(
                delete_key(item),
                DELETE_KINDS[item.kind],
                _target(item, plan.vg_uuid),
                _delete_write(hmc, plan),
            )
        except EffectNotApplied:
            refused.append(item)
            kept.append(item.entry(reason="precondition_failed"))
            continue
        journal.ledger_remove(item.kind, system_uuid, partition_uuid, item.identity())
    if refused:
        names = ", ".join(f"{i.name} on VIOS {i.vios_uuid}" for i in refused)
        raise engine.OperationFailed(
            f"precondition_failed: the HMC refused deleting {names} (HTTP 412); remove "
            "it with hmc_delete_virtual_disk or hmc_delete_optical_media"
        )
    return tuple(kept)


def _applied(effect: EffectRecord | None) -> bool:
    return effect is not None and effect.status == "applied"


async def plan_storage(
    hmc: HMCClient, system_uuid: str, partition_uuid: str
) -> DecommissionStorage:
    """The dry-run storage plan: deletable items as ``pending``, the rest retained."""
    items, retained = owned_items(system_uuid, partition_uuid)
    assessment = await assess(hmc, system_uuid, partition_uuid, items)
    return {
        "deleted": (),
        "retained": (*retained, *assessment.retained),
        "pending": tuple(p.item.entry() for p in assessment.plans),
    }


def deleted_from_effects(effects: Iterable[EffectRecord]) -> tuple[dict[str, str], ...]:
    """Every applied cleanup delete of the operation, across all of its runs."""
    kinds = {kind: ledger for ledger, kind in DELETE_KINDS.items()}
    return tuple(
        {"kind": kinds[e.kind], **{k: str(v) for k, v in (e.identity or {}).items()}}
        for e in effects
        if e.kind in kinds and e.status == "applied"
    )


def storage_from_effects(effects: Iterable[EffectRecord]) -> DecommissionStorage:
    """The storage block rebuilt from the journal alone: open deletes are pending."""
    effects = tuple(effects)
    kinds = {kind: ledger for ledger, kind in DELETE_KINDS.items()}
    return {
        "deleted": deleted_from_effects(effects),
        "retained": (),
        "pending": tuple(
            {"kind": kinds[e.kind], "target": e.target}
            for e in effects
            if e.kind in kinds and e.status in ("intended", "uncertain")
        ),
    }


def _parse_target(effect: EffectRecord) -> tuple[str, str, str]:
    """(VIOS, group, name) from ``vios:<uuid>/vg:<uuid>/<noun>:<name>``."""
    vios, group, named = effect.target.split("/", 2)
    return (
        vios.partition(":")[2],
        group.partition(":")[2],
        named.partition(":")[2],
    )


def storage_classifiers(
    client_factory: ClientFactory,
) -> Mapping[str, engine.Classifier]:
    """The live checks for the two delete kinds (ADR 0323 Decision 3)."""

    async def classify(
        ctx: OperationContext, effect: EffectRecord, kind: str
    ) -> Classification:
        vios, group, name = _parse_target(effect)
        async with client_factory(ctx.connection) as hmc:
            if kind == DISK_LEDGER:
                entry = await hmc.get_volume_group(vios, group)
                if entry is None:
                    raise HMCError(f"volume group {group} returned nothing")
                count = _disk_count(entry, name)
                identity = {"vios_uuid": vios, "vg_uuid": group, "disk_name": name}
            else:
                count = await _media_count(hmc, vios, group, name)
                identity = {"vios_uuid": vios, "vg_uuid": group, "media_name": name}
        if count == 0:
            return Classification("applied", identity)
        if count == 1:
            return Classification("not_applied")
        return Classification(
            "needs_attention", reason=f"{count} entries in group {group} share the name"
        )

    async def disk(ctx: OperationContext, effect: EffectRecord) -> Classification:
        return await classify(ctx, effect, DISK_LEDGER)

    async def media(ctx: OperationContext, effect: EffectRecord) -> Classification:
        return await classify(ctx, effect, MEDIA_LEDGER)

    return {DELETE_KINDS[DISK_LEDGER]: disk, DELETE_KINDS[MEDIA_LEDGER]: media}
