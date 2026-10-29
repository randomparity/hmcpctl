"""LPAR boot-order read and mutation operations."""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation

from ...client.client_lpars import _exact_partition_path
from ...documents.boot import select_boot_string
from ...documents.partition_profile import N
from ...errors import HMCError, LparUpdateError
from ...resource_identity import optional_system_selector, resolve_lpar_uuid
from .errors import translate_lpar_write_error

_logger = logging.getLogger(__name__)

_CLEAR_REFUSAL = (
    "Refusing to clear the pending boot order: no value tried on a V10R3 HMC clears it "
    "(an empty value fails with REST0126), so nothing was written. A profile "
    "activation consumed the pending boot order when observed on V10R3; other activation "
    "paths are unverified. Replace it with set-boot-order."
)


def _boot_text(value: object) -> str | None:
    """A boot field's text; an empty field parses as its attribute dict."""
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) and value else None


async def _write_pending_boot_string(
    hmc: HMCClient, lpar_uuid: str, boot_string: str
) -> dict[str, Any] | None:
    try:
        return await hmc.set_pending_boot_string(lpar_uuid, boot_string)
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc


async def read_lpar_boot_order(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> dict[str, Any]:
    """Read current, pending, and last-used boot-device state for an LPAR."""
    selector = optional_system_selector(system_name_or_uuid)
    if selector is None:
        raise ValueError("system_name_or_uuid is required to read a boot order")
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=selector
    )
    try:
        lpar = await hmc.get_uom("LogicalPartition", lpar_uuid, group="Advanced")
    except HMCError as error:
        raise HMCError(
            "Pending boot observation unavailable; inspect before retrying",
            error.status_code,
        ) from None
    if not lpar:
        raise ValueError(f"LPAR {lpar_uuid!r} not found")

    resource = lpar.get("Resource") or {}
    boot_list_info = resource.get("BootListInformation") or {}

    try:
        document, _ = await hmc.read_logical_partition_document(lpar_uuid)
    except HMCError as error:
        raise HMCError(
            "Pending boot observation unavailable; inspect before retrying",
            error.status_code,
        ) from None
    identity = _exact_partition_path(document, "Metadata/Atom/AtomID")
    name = _exact_partition_path(document, "PartitionName")
    pending = _exact_partition_path(document, "BootListInformation/PendingBootString")
    if (
        document.tag != N + "LogicalPartition"
        or identity is None
        or (identity.text or "").casefold() != lpar_uuid.casefold()
        or name is None
        or name.text != resource.get("PartitionName")
        or pending is None
        or len(pending)
    ):
        raise HMCError(
            "Pending boot observation unavailable or identity differs; inspect before retrying"
        )
    return {
        "lpar_uuid": lpar_uuid,
        "lpar_name": resource.get("PartitionName"),
        "pending_boot_string": pending.text or None,
        "boot_device_list": _boot_text(boot_list_info.get("BootDeviceList")),
        "last_booted_device_string": _boot_text(
            boot_list_info.get("LastBootedDeviceString")
        ),
    }


async def set_lpar_boot_order(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    devices: list[str] | None = None,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
    boot_string: str | None = None,
) -> dict[str, Any] | None:
    """Set the pending boot order used on the LPAR's next activation."""
    requested = select_boot_string(devices, boot_string)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )

    before = await read_lpar_boot_order(
        hmc, lpar_uuid, system_name_or_uuid=system_name_or_uuid
    )
    if boot_string is not None:
        document, _ = await hmc.read_logical_partition_document(lpar_uuid)
        environment = _exact_partition_path(document, "PartitionType")
        identity = _exact_partition_path(document, "Metadata/Atom/AtomID")
        if identity is None or (identity.text or "").casefold() != lpar_uuid.casefold():
            raise HMCError(
                "Raw boot admission identity unavailable; nothing was submitted"
            )
        if environment is None or environment.text != "AIX/Linux":
            raise ValueError("Raw boot input requires AIX/Linux; OS400 is unsupported")
    try:
        updated = await _write_pending_boot_string(hmc, lpar_uuid, requested)
    except HMCError as error:
        submission = (
            error.submission if isinstance(error, LparUpdateError) else "unknown"
        )
        raise LparUpdateError(
            "Pending boot write unverified; inspect contained state before retrying. No retry or rollback performed",
            submission,
            error.status_code,
        ) from None
    observed = await read_lpar_boot_order(
        hmc, lpar_uuid, system_name_or_uuid=system_name_or_uuid
    )
    if (
        observed["pending_boot_string"] != requested
        or observed["lpar_uuid"] != before["lpar_uuid"]
        or observed["lpar_name"] != before["lpar_name"]
    ):
        raise HMCError(
            "Pending boot write verification mismatch; inspect before retrying, no rollback performed"
        )
    _logger.info("Pending boot order independently verified")
    return updated


async def clear_lpar_boot_order(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> NoReturn:
    """Refuse to clear the LPAR's pending boot order after authorizing the caller.

    V10R3 accepts no clearing value; the probes are in the #1048 design spec.
    """
    await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    raise HMCError(_CLEAR_REFUSAL)
