"""Guarded SSH-backed LPAR configuration mutations."""

from __future__ import annotations

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import (
    _authorize_system_lpar_profile_restore,
    _resolve_system_name,
    authorize_lpar_mutation,
    resolve_and_authorize_lpar_names,
)
from hmcpctl.resource_identity import (
    ResourceNotFoundError,
    _hosts_partition,
    is_uuid,
    resolve_system_uuid,
)

from ...ssh.profiles import (
    ProfileRestoreType,
    ProfileSyncMode,
    restore_lpar_profiles,
    set_lpar_msp,
    set_lpar_proc_compat,
    sync_lpar_profile,
    validate_profile_sync_mode,
)
from .core import ProcessorCompatibilityMode


async def restore_system_lpar_profiles(
    hmc: HMCClient,
    system_name_or_uuid: str,
    file_path: str,
    restore_type: ProfileRestoreType,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize and restore every LPAR profile on one managed system."""
    system_name = await _authorize_system_lpar_profile_restore(
        hmc,
        system_name_or_uuid,
        ownership_override=ownership_override,
    )
    return await restore_lpar_profiles(hmc.config, system_name, file_path, restore_type)


async def synchronize_lpar_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool = False,
    mode: ProfileSyncMode = "enable",
) -> str:
    """Authorize and set an LPAR's ``sync_curr_profile`` setting (ADR 0201)."""
    validate_profile_sync_mode(mode)
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    return await sync_lpar_profile(hmc.config, system_name, lpar_name, mode)


async def configure_lpar_msp(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    enabled: bool,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize a VIOS and set its migratable-service-partition flag (ADR 0092 §1)."""
    system_name, vios_name = await _resolve_and_authorize_vios_names(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    return await set_lpar_msp(hmc.config, system_name, vios_name, enabled)


async def _resolve_and_authorize_vios_names(
    hmc: HMCClient,
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    *,
    ownership_override: bool,
) -> tuple[str, str]:
    """Resolve a VIOS the selected system hosts, authorize it, and return CLI names.

    ADR 0092 §1: ``msp`` is valid only on a Virtual I/O Server, so its target
    crosses the same description-stamp rule as an LPAR. The system's own
    ``VirtualIOServer`` feed establishes containment and supplies the name.
    """
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    entries = await hmc.list_vios(system_uuid)
    if is_uuid(vios_name_or_uuid):
        matches = [e for e in entries if _hosts_partition([e], vios_name_or_uuid)]
    else:
        matches = [
            e
            for e in entries
            if (e.get("Resource") or {}).get("PartitionName") == vios_name_or_uuid
        ]
    vios_name = (
        (matches[0].get("Resource") or {}).get("PartitionName") if matches else None
    )
    if not isinstance(vios_name, str) or not vios_name.strip():
        raise ResourceNotFoundError(
            "VIOS",
            vios_name_or_uuid,
            f"No Virtual I/O Server (lpar_env=vioserver) {vios_name_or_uuid!r} on "
            f"managed system {system_uuid}. Use hmc_list_vios to list its VIOS "
            "partitions.",
        )
    system_name = await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    await authorize_lpar_mutation(
        hmc, system_name, vios_name, ownership_override=ownership_override
    )
    return system_name, vios_name


async def configure_lpar_processor_compatibility(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    mode: ProcessorCompatibilityMode,
    *,
    profile_name: str | None = None,
    ownership_override: bool = False,
) -> str:
    """Authorize and set the processor compatibility mode on an LPAR profile.

    Returns a sentence naming the profile changed (the default profile when
    *profile_name* is omitted).
    """
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    profile = await set_lpar_proc_compat(
        hmc.config, system_name, lpar_name, mode, profile_name
    )
    return f"Set lpar_proc_compat_mode={mode} on profile {profile} of {lpar_name}"
