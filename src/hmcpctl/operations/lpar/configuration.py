"""Guarded SSH-backed LPAR configuration mutations."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.linux_roles import (
    admit_role_update,
    read_role_capabilities,
    roles_requested,
    with_role_evidence,
)
from hmcpctl.operations.lpar.ownership import (
    _authorize_system_lpar_profile_restore,
    _resolve_contained_lpar_names,
    _resolve_system_name,
    authorize_lpar_mutation,
    resolve_and_authorize_lpar_names,
)
from hmcpctl.resource_identity import (
    ResourceNotFoundError,
    _hosts_partition,
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_uuid,
)

from ...documents.lpar import LparReportingSettings, LparSettings
from ...ssh.commands import build_attribute_record
from ...ssh.lpar import (
    get_lpar_reporting_settings,
    get_lpar_settings,
    set_lpar_reporting_settings,
    set_lpar_settings,
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
from ...ssh.transport import HMCCLIError
from ...xmlutil import leaf_text

ProcessorCompatibilityMode = Literal[
    "default",
    "POWER5",
    "POWER6",
    "POWER6+",
    "POWER7",
    "POWER8",
    "POWER9_Base",
    "POWER9_base",
    "POWER9",
    "POWER10",
    "POWER11",
]


PROCESSOR_COMPATIBILITY_MODES: frozenset[ProcessorCompatibilityMode] = frozenset(
    {
        "default",
        "POWER5",
        "POWER6",
        "POWER6+",
        "POWER7",
        "POWER8",
        "POWER9_Base",
        "POWER9_base",
        "POWER9",
        "POWER10",
        "POWER11",
    }
)


async def restore_system_lpar_profiles(
    hmc: HMCClient,
    system_name_or_uuid: str,
    file_path: str,
    restore_type: ProfileRestoreType,
    *,
    ownership_override: bool = False,
    force_keystore: bool = False,
) -> str:
    """Authorize and restore every LPAR profile on one managed system."""
    system_name = await _authorize_system_lpar_profile_restore(
        hmc,
        system_name_or_uuid,
        ownership_override=ownership_override,
    )
    return await restore_lpar_profiles(
        hmc.config, system_name, file_path, restore_type, force_keystore=force_keystore
    )


async def synchronize_lpar_profile(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
    mode: ProfileSyncMode = "enable",
) -> str:
    """Authorize and set an LPAR's ``sync_curr_profile`` setting (ADR 0201)."""
    validate_profile_sync_mode(mode)
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    return await sync_lpar_profile(hmc.config, system_name, lpar_name, mode)


async def configure_lpar_msp(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    enabled: bool,
    *,
    system_name_or_uuid: str,
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
    lpar_name_or_uuid: str,
    mode: ProcessorCompatibilityMode,
    *,
    system_name_or_uuid: str,
    profile_name: str | None = None,
    ownership_override: bool = False,
) -> str:
    """Authorize and set the processor compatibility mode on an LPAR profile.

    Returns a sentence naming the profile changed (the default profile when
    *profile_name* is omitted).
    """
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    profile = await set_lpar_proc_compat(
        hmc.config, system_name, lpar_name, mode, profile_name
    )
    return f"Set lpar_proc_compat_mode={mode} on profile {profile} of {lpar_name}"


@dataclass(frozen=True)
class LparSettingsResult:
    """Observed outcome of a single non-retried settings write."""

    status: Literal[
        "verified", "mismatch", "readback-unavailable", "write-outcome-unknown"
    ]
    requested: LparSettings
    observed: LparSettings | None
    warnings: tuple[str, ...]


async def read_lpar_settings(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> LparSettings:
    """Read settings after establishing target containment."""
    system, name = await _resolve_contained_lpar_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await get_lpar_settings(hmc.config, system, name)


async def _role_resource(hmc: HMCClient, uuid: str, name: str) -> dict[str, object]:
    entry = await hmc.get_logical_partition(uuid)
    resource = (entry or {}).get("Resource")
    if (
        not isinstance(resource, dict)
        or str((entry or {}).get("UUID", "")).casefold() != uuid.casefold()
        or leaf_text(resource.get("PartitionName")) != name
    ):
        raise ValueError(
            "Linux roles require matching immutable partition identity and name"
        )
    return resource


async def configure_lpar_settings(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    settings: LparSettings,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> LparSettingsResult:
    """Authorize, write once and verify supplied and known-preserved settings.

    A running time-reference partition may govern hypervisor/service-processor
    time. This operation does not activate a partition or change a guest clock.
    """
    if not settings.pairs():
        raise ValueError("Supply at least one partition setting")
    role_selection = roles_requested(settings)
    try:
        uuid = None
        system_uuid = None
        if role_selection:
            system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
            uuid = await resolve_lpar_uuid(
                hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
            )
        system, name = await resolve_and_authorize_lpar_names(
            hmc,
            uuid if uuid is not None else lpar_name_or_uuid,
            system_name_or_uuid=system_uuid
            if system_uuid is not None
            else system_name_or_uuid,
            ownership_override=ownership_override,
        )
        resource = None
        if uuid is not None and system_uuid is not None:
            contained = await resolve_lpar_uuid(
                hmc, name, system_name_or_uuid=system_uuid
            )
            if contained.casefold() != uuid.casefold():
                raise ValueError(
                    "Linux role partition identity changed during authorization"
                )
            resource = await _role_resource(hmc, uuid, name)
            caps = await read_role_capabilities(
                hmc,
                system_uuid,
                need_kvm=settings.kvm_capable is not None,
                need_management=settings.powervm_mgmt_capable is not None,
            )
            admit_role_update(settings, caps, resource)
        build_attribute_record([("name", name), *settings.pairs()])
        before = await get_lpar_settings(hmc.config, system, name)
    except HMCError as error:
        if not role_selection:
            raise
        raise type(error)(
            "Linux role preflight could not read required configuration evidence; "
            "no changes were submitted. Check connectivity and read permissions, then retry.",
            status_code=error.status_code,
        ) from None
    if resource is not None:
        before = with_role_evidence(before, resource, LparSettings())
    expected = replace(
        before, **{name: getattr(settings, name) for name, _ in settings.pairs()}
    )
    try:
        await set_lpar_settings(hmc.config, system, name, settings)
    except HMCError as error:
        if not role_selection and not isinstance(error, HMCCLIError):
            raise
        return LparSettingsResult(
            "write-outcome-unknown",
            settings,
            None,
            (
                "Submission failed; inspect before retrying. No replay attempted."
                if role_selection
                else f"Write did not confirm completion: {error}. Inspect before retrying.",
            ),
        )
    try:
        after = await get_lpar_settings(hmc.config, system, name)
        if uuid is not None:
            after = with_role_evidence(
                after, await _role_resource(hmc, uuid, name), expected
            )
    except (HMCError, ValueError, TypeError) as error:
        if not role_selection and not isinstance(error, HMCCLIError):
            raise
        return LparSettingsResult(
            "readback-unavailable",
            settings,
            None,
            (
                "Write returned successfully but role readback is unavailable; inspect before retrying."
                if role_selection
                else f"Write returned successfully but readback failed: {error}",
            ),
        )
    if any(
        getattr(expected, field.name) is not None
        and getattr(after, field.name) != getattr(expected, field.name)
        for field in fields(expected)
    ):
        return LparSettingsResult(
            "mismatch",
            settings,
            after,
            (
                "Readback differs from requested or preserved settings; inspect before retrying.",
            ),
        )
    return LparSettingsResult("verified", settings, after, ())


@dataclass(frozen=True)
class LparReportingSettingsResult:
    """Outcome of one C02 settings write; configuration is not guest-runtime proof."""

    status: Literal[
        "verified", "mismatch", "readback-unavailable", "write-outcome-unknown"
    ]
    requested: LparReportingSettings
    observed: LparReportingSettings | None
    warnings: tuple[str, ...]


async def read_lpar_reporting_settings(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> LparReportingSettings:
    """Read applicable reporting settings on a contained partition."""
    system, name = await _resolve_contained_lpar_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await get_lpar_reporting_settings(hmc.config, system, name)


async def configure_lpar_reporting_settings(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    settings: LparReportingSettings,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> LparReportingSettingsResult:
    """Authorize, admit partition type, write once and compare supplied/omitted values."""
    settings.validate_context("update")
    if not settings.pairs():
        raise ValueError("Supply at least one reporting setting")
    system, name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        system_name_or_uuid=system_name_or_uuid,
        ownership_override=ownership_override,
    )
    build_attribute_record([("name", name), *settings.pairs()])
    before = await get_lpar_reporting_settings(
        hmc.config, system, name, require_ibmi=settings.requires_ibmi()
    )
    try:
        await set_lpar_reporting_settings(hmc.config, system, name, settings)
    except HMCCLIError as error:
        return LparReportingSettingsResult(
            "write-outcome-unknown",
            settings,
            None,
            (f"Write did not confirm completion: {error}. Inspect before retrying.",),
        )
    try:
        after = await get_lpar_reporting_settings(
            hmc.config, system, name, require_ibmi=settings.requires_ibmi()
        )
    except HMCCLIError as error:
        return LparReportingSettingsResult(
            "readback-unavailable",
            settings,
            None,
            (f"Write returned successfully but readback failed: {error}",),
        )
    expected = replace(
        before, **{name: getattr(settings, name) for name, _ in settings.pairs()}
    )
    return LparReportingSettingsResult(
        "verified" if after == expected else "mismatch",
        settings,
        after,
        ()
        if after == expected
        else (
            "Reporting readback differs from requested/preserved values; inspect before retrying",
        ),
    )
