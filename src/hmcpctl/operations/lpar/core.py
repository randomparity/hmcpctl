"""Authorized LPAR deletion and renaming."""

from __future__ import annotations

from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import partition_updates
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.errors import translate_lpar_write_error
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_mutation
from hmcpctl.xmlutil import escape_xml


async def delete_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> str:
    """Authorize and delete a powered-off LPAR, returning its UUID."""
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    state = await hmc.get_quick_property(
        "LogicalPartition", lpar_uuid, "PartitionState"
    )
    if state != "not activated":
        raise HMCError(
            f"Cannot delete LPAR {lpar_uuid} — current state is {state!r}; "
            "it must be 'not activated' to delete. Power off the partition "
            "and verify its state before retrying.",
            status_code=409,
        )
    await hmc.delete_logical_partition(lpar_uuid)
    return lpar_uuid


async def rename_lpar(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    new_name: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve, authorize, and rename one LPAR by whole-partition read-modify-write."""
    escape_xml(new_name)
    lpar_uuid = await resolve_and_authorize_lpar_mutation(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    try:
        updated = await hmc.update_logical_partition(
            lpar_uuid,
            lambda lpar: partition_updates(lpar, name=new_name),
            "the partition name",
        )
    except HMCError as exc:
        translated = translate_lpar_write_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    return lpar_uuid, updated
