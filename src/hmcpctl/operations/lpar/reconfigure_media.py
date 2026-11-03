"""``hmc_reconfigure_lpar``'s installer-media detach and root-disk boot legs (ADR 0321).

Both act only on what this partition's provision recorded in the operation ledger: the
``installer_media`` entry names the medium to unmap, the ``virtual_disk`` entry the root
disk to boot. Planning reads the ledger and the HMC once and the plan records the result,
so a replay never re-reads the ledger. Messages name partitions by UUID only.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ...client.core import HMCClient
from ...errors import HMCError, LparUpdateError
from ...xmlutil import leaf_text
from ..logical import engine, store
from ..logical.engine import (
    Classification,
    EffectNotApplied,
    OperationContext,
    OperationFailed,
)
from ..logical.models import EffectRecord
from ..storage.resources import storage_mapping, unmount_optical_media
from .boot_order import read_lpar_boot_order, set_lpar_boot_order

# Persisted contract (ADR 0195 Consequences): never change while an operation can be open.
BOOT_KEY, BOOT_KIND = "boot", "lpar.boot_order"
MEDIA_KEY, MEDIA_KIND = "media_detach", "vios.optical_unmount"
# Ledger kinds and identity fields provision writes (ADR 0313, ADR 0319).
PARTITION_LEDGER = "partition"
MEDIA_LEDGER = "installer_media"
DISK_LEDGER = "virtual_disk"
_FIELDS = {
    MEDIA_LEDGER: ("vios_uuid", "media_name"),
    DISK_LEDGER: ("vios_uuid", "disk_name"),
}
_LUN = re.compile(r"0x([0-9a-fA-F]{1,16})")
_SLOT_BASE = 0x30000000
Write = Callable[[], Awaitable[Mapping[str, Any]]]
PlanOf = Callable[[OperationContext], Mapping[str, Any]]
Opener = Callable[[str], Any]


def provision_owned(system_uuid: str, lpar_uuid: str, kind: str) -> dict[str, Any]:
    """The *kind* identity this partition's provision recorded (ADR 0321 Decision 1)."""
    with store.read_session() as conn:
        if conn is None:
            raise OperationFailed(
                f"no operation store exists, so no provision owns LPAR {lpar_uuid}"
            )
        partitions = store.ledger_entries(conn, PARTITION_LEDGER, system_uuid)
        entries = store.ledger_entries(conn, kind, system_uuid)
    owners = {
        op for op, part, _ in partitions if (part or "").lower() == lpar_uuid.lower()
    }
    if len(owners) != 1:
        raise OperationFailed(
            f"LPAR {lpar_uuid} has {len(owners)} provision records in the operation "
            "ledger, not one; only a partition hmcpctl provisioned can use this field"
        )
    (owner,) = owners
    hits = [
        identity
        for op, part, identity in entries
        if op == owner and (part or "").lower() == lpar_uuid.lower()
    ]
    usable = len(hits) == 1 and all(
        isinstance(hits[0].get(field), str) and hits[0][field]
        for field in _FIELDS[kind]
    )
    if not usable:
        raise OperationFailed(
            f"the provision of LPAR {lpar_uuid} recorded {len(hits)} {kind} ledger "
            "entries, not one usable entry; nothing was written"
        )
    return {field: hits[0][field] for field in _FIELDS[kind]}


def _child(node: object, name: str) -> object:
    return node.get(name) if isinstance(node, Mapping) else None


async def _media_paths(
    hmc: HMCClient, vios_uuid: str, lpar_uuid: str, media_name: str
) -> list[Mapping[str, Any]]:
    """The partition's optical mappings on *vios_uuid* that hold *media_name*."""
    return [
        mapping
        for mapping in await hmc.list_optical_mappings(vios_uuid, lpar_uuid)
        if leaf_text(
            _child(
                _child(_child(mapping, "Storage"), "VirtualOpticalMedia"), "MediaName"
            )
        )
        == media_name
    ]


def _disk_path(mapping: Mapping[str, Any]) -> str | None:
    """The Open Firmware path of a vSCSI mapping's client device, or None.

    ``/vdevice/v-scsi@<0x30000000 + client slot>/disk@<LUN without 0x>``
    (docs/recipes/lpar-iso-install.md, section 5).
    """
    resource = mapping.get("Resource", mapping)
    slot = leaf_text(_child(_child(resource, "ClientAdapter"), "VirtualSlotNumber"))
    target = _child(resource, "TargetDevice")
    luns = [
        leaf_text(_child(device, "LogicalUnitAddress"))
        for device in (target.values() if isinstance(target, Mapping) else ())
    ]
    lun = (
        _LUN.fullmatch(luns[0].strip())
        if len(luns) == 1 and isinstance(luns[0], str)
        else None
    )
    if not isinstance(slot, str) or not slot.strip().isdigit() or lun is None:
        return None
    return f"/vdevice/v-scsi@{_SLOT_BASE + int(slot):x}/disk@{lun.group(1).lower()}"


async def root_disk_path(
    hmc: HMCClient, lpar_uuid: str, disk: Mapping[str, Any]
) -> str:
    """The device path of the root disk's one mapping to *lpar_uuid*."""
    paths = []
    for entry in await hmc.list_storage_mappings(disk["vios_uuid"]):
        found = storage_mapping(entry)
        if (
            found.backing_kind == "VirtualDisk"
            and found.backing_name == disk["disk_name"]
            and (found.lpar_uuid or "").lower() == lpar_uuid
        ):
            paths.append(_disk_path(entry))
    if len(paths) != 1 or paths[0] is None:
        raise OperationFailed(
            f"the root disk of LPAR {lpar_uuid} is mapped {len(paths)} times or its "
            "mapping has no client slot and LUN; nothing was written"
        )
    return paths[0]


async def _pending(hmc: HMCClient, lpar_uuid: str, system_uuid: str) -> str | None:
    observed = await read_lpar_boot_order(
        hmc, lpar_uuid, system_name_or_uuid=system_uuid
    )
    return observed["pending_boot_string"]


async def plan_media(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The media to unmap and its change, or ``(None, None)`` when already unmapped."""
    media = provision_owned(system_uuid, lpar_uuid, MEDIA_LEDGER)
    try:
        found = await _media_paths(
            hmc, media["vios_uuid"], lpar_uuid, media["media_name"]
        )
    except HMCError as exc:
        raise OperationFailed(
            f"the optical mappings of LPAR {lpar_uuid} could not be read "
            f"({type(exc).__name__})"
        ) from exc
    if not found:
        return None, None
    if len(found) > 1:
        raise OperationFailed(
            f"the installer media of LPAR {lpar_uuid} is mapped {len(found)} times; "
            "nothing was written"
        )
    change = {
        "field": "installer_media",
        "before": {"current": "mounted"},
        "after": "detached",
        "applied": "live",
        "reason": None,
    }
    return media, change


async def plan_boot(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The boot-string change to the root disk, or ``(None, None)`` when already set."""
    disk = provision_owned(system_uuid, lpar_uuid, DISK_LEDGER)
    try:
        path = await root_disk_path(hmc, lpar_uuid, disk)
        before = await _pending(hmc, lpar_uuid, system_uuid)
    except (HMCError, ValueError, LookupError) as exc:
        raise OperationFailed(
            f"the root disk mapping or pending boot order of LPAR {lpar_uuid} could not "
            f"be read ({type(exc).__name__})"
        ) from exc
    if before == path:
        return None, None
    change = {
        "field": "boot",
        "before": {"current": before},
        "after": path,
        "applied": "next_activation",
        "reason": None,
    }
    return {"before": before, "after": path}, change


def boot_write(hmc: HMCClient, plan: Mapping[str, Any]) -> Write:
    """Set the pending boot string; ``set_lpar_boot_order`` verifies its own write."""

    async def write() -> Mapping[str, Any]:
        after = plan["boot"]["after"]
        try:
            await set_lpar_boot_order(
                hmc,
                plan["lpar_uuid"],
                [after],
                system_name_or_uuid=plan["system_uuid"],
            )
        except LparUpdateError as exc:
            if exc.submission == "rejected":
                raise EffectNotApplied(
                    f"the HMC refused the boot order write to LPAR {plan['lpar_uuid']}"
                ) from exc
            raise
        except (ValueError, LookupError, PermissionError) as exc:
            raise EffectNotApplied(
                f"the boot order write was refused before submission "
                f"({type(exc).__name__})"
            ) from exc
        return {"pending_boot_string": after}

    return write


def media_write(hmc: HMCClient, plan: Mapping[str, Any]) -> Write:
    """Remove the installer media's mapping; a refusal is classified by one listing."""
    media, lpar_uuid = plan["media"], plan["lpar_uuid"]
    identity = {"vios_uuid": media["vios_uuid"], "media_name": media["media_name"]}

    async def write() -> Mapping[str, Any]:
        try:
            await unmount_optical_media(
                hmc,
                media["vios_uuid"],
                lpar_uuid,
                system_name_or_uuid=plan["system_uuid"],
                media_name=media["media_name"],
            )
        except (ValueError, LookupError, PermissionError) as exc:
            raise EffectNotApplied(
                f"the installer media detach was refused before writing "
                f"({type(exc).__name__})"
            ) from exc
        except HMCError as exc:
            try:
                left = await _media_paths(
                    hmc, media["vios_uuid"], lpar_uuid, media["media_name"]
                )
            except HMCError:
                raise exc from None
            if left:
                raise EffectNotApplied(
                    f"the HMC refused to detach the installer media from LPAR "
                    f"{lpar_uuid} (HTTP {exc.status_code}) and it is still mapped. "
                    "Retry, or power the partition off and then resume a paused "
                    "operation or submit a new request"
                ) from exc
        return identity

    return write


def boot_classifier(plan_of: PlanOf, opener: Opener) -> engine.Classifier:
    """``lpar.boot_order`` by the pending boot string (ADR 0321 Decision 5)."""

    async def classify(ctx: OperationContext, _effect: EffectRecord) -> Classification:
        plan = plan_of(ctx)
        async with opener(ctx.connection) as hmc:
            pending = await _pending(hmc, plan["lpar_uuid"], plan["system_uuid"])
        if pending == plan["boot"]["after"]:
            return Classification("applied", {"pending_boot_string": pending})
        if pending == plan["boot"]["before"]:
            return Classification("not_applied")
        return Classification(
            "needs_attention",
            reason=f"LPAR {plan['lpar_uuid']}'s pending boot order is neither the "
            "planned nor the earlier one; inspect it",
        )

    return classify


def media_classifier(plan_of: PlanOf, opener: Opener) -> engine.Classifier:
    """``vios.optical_unmount`` by the media's mappings (ADR 0321 Decision 5)."""

    async def classify(ctx: OperationContext, _effect: EffectRecord) -> Classification:
        plan = plan_of(ctx)
        media = plan["media"]
        async with opener(ctx.connection) as hmc:
            left = await _media_paths(
                hmc, media["vios_uuid"], plan["lpar_uuid"], media["media_name"]
            )
        if not left:
            return Classification(
                "applied",
                {"vios_uuid": media["vios_uuid"], "media_name": media["media_name"]},
            )
        if len(left) == 1:
            return Classification("not_applied")
        return Classification(
            "needs_attention",
            reason=f"the installer media of LPAR {plan['lpar_uuid']} is mapped "
            f"{len(left)} times; inspect it",
        )

    return classify


async def observed(
    hmc: HMCClient, plan: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Whether the boot and media legs read as planned, and what was read."""
    seen: dict[str, Any] = {}
    done = True
    if plan.get("boot"):
        seen["boot"] = await _pending(hmc, plan["lpar_uuid"], plan["system_uuid"])
        done = seen["boot"] == plan["boot"]["after"]
    if plan.get("media"):
        media = plan["media"]
        left = await _media_paths(
            hmc, media["vios_uuid"], plan["lpar_uuid"], media["media_name"]
        )
        seen["installer_media"] = "mounted" if left else "detached"
        done = done and not left
    return done, seen
