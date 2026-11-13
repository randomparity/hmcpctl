"""MCP tools for virtual persistent-memory volumes (#643, ADR 0373)."""

from __future__ import annotations

from pydantic import StrictBool, StrictInt

from ..._app import run_limited_collection, with_client
from ...documents.persistent_memory import PersistentMemoryVolume
from ...operations.lpar.memory.persistent import (
    PersistentMemoryVolumeResult,
    create_persistent_memory_volume,
    delete_persistent_memory_volume,
    get_persistent_memory_volume,
    list_persistent_memory_volumes,
    rename_persistent_memory_volume,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="pmem.list_volumes", target_kind="managed_system")
def hmc_list_persistent_memory_volumes(
    lpar_name_or_uuid: str | None = None,
    limit: int | None = None,
    *,
    system_name_or_uuid: str,
    profile: str | None = None,
) -> list[PersistentMemoryVolume]:
    """List a managed system's virtual persistent-memory volumes.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Only volumes assigned to this partition, when given.
        limit: Maximum entries returned after the complete HMC feed is transferred
            and parsed; omitted returns all entries. This client-side cap does not
            reduce HMC work or network transfer.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """
    return run_limited_collection(
        lambda hmc: list_persistent_memory_volumes(
            hmc, system_name_or_uuid, lpar_name_or_uuid
        ),
        limit,
        profile=profile,
    )


@tool(effect="read", operation="pmem.get_volume", target_kind="managed_system")
def hmc_get_persistent_memory_volume(
    system_name_or_uuid: str, volume: str, profile: str | None = None
) -> PersistentMemoryVolume:
    """Read one virtual persistent-memory volume by UUID or exact name.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        volume: Volume UUID, or an exact name that matches one volume.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """
    return with_client(
        lambda hmc: get_persistent_memory_volume(hmc, system_name_or_uuid, volume),
        profile=profile,
    )


@tool(effect="mutate", operation="pmem.create_volume", target_kind="lpar")
def hmc_create_persistent_memory_volume(
    lpar_name_or_uuid: str,
    name: str,
    size_mib: StrictInt,
    affinity: StrictBool = False,
    ownership_override: StrictBool = False,
    *,
    system_name_or_uuid: str,
    profile: str | None = None,
) -> PersistentMemoryVolumeResult:
    """Create a DRAM persistent-memory volume assigned to an inactive partition.

    The partition must be an AIX/Linux partition in the ``not activated`` state
    with a spare DRAM volume slot, and the size a multiple of the system's
    advertised block size. Submitted once; inspect an uncertain outcome before
    retrying.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition the volume is assigned to.
        name: New volume name, 1-64 characters of A-Z a-z 0-9 _ . -, not shaped
            like a UUID, unique on the system.
        size_mib: Volume size in MiB.
        affinity: Request memory affinity for the volume.
        ownership_override: Bypass ownership protection after operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """
    return with_client(
        lambda hmc: create_persistent_memory_volume(
            hmc,
            lpar_name_or_uuid,
            name,
            size_mib,
            system_name_or_uuid=system_name_or_uuid,
            affinity=affinity,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="pmem.rename_volume", target_kind="lpar")
def hmc_rename_persistent_memory_volume(
    lpar_name_or_uuid: str,
    volume: str,
    new_name: str,
    ownership_override: StrictBool = False,
    *,
    system_name_or_uuid: str,
    profile: str | None = None,
) -> PersistentMemoryVolumeResult:
    """Rename a persistent-memory volume assigned to the named partition.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition the volume is assigned to.
        volume: Volume UUID, or an exact name that matches one volume.
        new_name: 1-64 characters of A-Z a-z 0-9 _ . -, not shaped like a UUID,
            unique on the system.
        ownership_override: Bypass ownership protection after operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """
    return with_client(
        lambda hmc: rename_persistent_memory_volume(
            hmc,
            lpar_name_or_uuid,
            volume,
            new_name,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="pmem.delete_volume", target_kind="lpar")
def hmc_delete_persistent_memory_volume(
    lpar_name_or_uuid: str,
    volume: str,
    acknowledge_data_loss: StrictBool = False,
    ownership_override: StrictBool = False,
    *,
    system_name_or_uuid: str,
    profile: str | None = None,
) -> PersistentMemoryVolumeResult:
    """Delete a persistent-memory volume; the data it holds is destroyed.

    The partition must be inactive (``not activated``) and the volume assigned
    to it. Requires ``acknowledge_data_loss=True``; there is no undo.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition the volume is assigned to.
        volume: Volume UUID, or an exact name that matches one volume.
        acknowledge_data_loss: Consent to destroying the volume's data.
        ownership_override: Bypass ownership protection after operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """
    return with_client(
        lambda hmc: delete_persistent_memory_volume(
            hmc,
            lpar_name_or_uuid,
            volume,
            system_name_or_uuid=system_name_or_uuid,
            acknowledge_data_loss=acknowledge_data_loss,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )
