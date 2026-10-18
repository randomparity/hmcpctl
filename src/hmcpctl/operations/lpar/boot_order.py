"""LPAR boot-order read and mutation operations."""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation

from ...documents.boot import join_boot_device_paths
from ...errors import HMCError
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
    lpar = await hmc.get_uom("LogicalPartition", lpar_uuid, group="Advanced")
    if not lpar:
        raise ValueError(f"LPAR {lpar_uuid!r} not found")

    resource = lpar.get("Resource") or {}
    boot_list_info = resource.get("BootListInformation") or {}

    return {
        "lpar_uuid": lpar_uuid,
        "lpar_name": resource.get("PartitionName"),
        "pending_boot_string": _boot_text(boot_list_info.get("PendingBootString")),
        "boot_device_list": _boot_text(boot_list_info.get("BootDeviceList")),
        "last_booted_device_string": _boot_text(
            boot_list_info.get("LastBootedDeviceString")
        ),
    }


async def set_lpar_boot_order(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    devices: list[str],
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Set the pending boot order used on the LPAR's next activation."""
    boot_string = join_boot_device_paths(devices)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )

    updated = await _write_pending_boot_string(hmc, lpar_uuid, boot_string)

    _logger.info(
        "Set boot order for LPAR %s (%s) to: %s",
        lpar_name_or_uuid,
        lpar_uuid,
        boot_string,
    )

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
