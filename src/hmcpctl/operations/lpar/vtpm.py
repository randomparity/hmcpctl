"""Advertised, contained and explicitly acknowledged partition vTPM changes."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Literal

from ...client.core import HMCClient
from ...documents.lpar_vtpm import LparVtpmSettings, VtpmState
from ...errors import HMCError
from ...resource_identity import resolve_system_uuid, resolve_vios_uuid
from ...ssh.commands import build_attribute_record
from ...ssh.lpar_vtpm import (
    get_lpar_vtpm_state,
    get_vtpm_advertisement,
    set_lpar_vtpm_settings,
)
from ...xmlutil import leaf_text
from .configuration import _resolve_and_authorize_vios_names
from .ownership import (
    _resolve_contained_lpar_names,
    _resolve_system_name,
    resolve_and_authorize_lpar_names,
)


@dataclass(frozen=True)
class VtpmCapabilities:
    """System-advertised values, not inferred from a processor generation."""

    versions: tuple[str, ...]
    encryptions: tuple[str, ...]
    version20_capable: bool


@dataclass(frozen=True)
class VtpmResult:
    """Requested/observed effects of one non-retried submission."""

    status: Literal[
        "verified", "mismatch", "readback-unavailable", "write-outcome-unknown"
    ]
    requested: LparVtpmSettings
    observed: LparVtpmSettings | None
    warnings: tuple[str, ...]


async def read_vtpm_capabilities(
    hmc: HMCClient, system_name_or_uuid: str
) -> VtpmCapabilities:
    """Require the selected system's positive capability and strict advertisement."""
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    entry = await hmc.get_managed_system(system_uuid)
    resource = (entry or {}).get("Resource") or {}
    caps = resource.get("SystemCapabilities") if isinstance(resource, dict) else None
    capability = (
        leaf_text(caps.get("VirtualTrustedPlatformModuleCapable"))
        if isinstance(caps, dict)
        else None
    )
    if capability is not True and capability != "true":
        raise ValueError(
            "Selected system does not positively advertise vTPM capability; nothing was changed"
        )
    system = await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    versions, encryptions = await get_vtpm_advertisement(hmc.config, system)
    version20 = (
        leaf_text(caps.get("VirtualTrustedPlatformModule20Capable"))
        if isinstance(caps, dict)
        else None
    )
    return VtpmCapabilities(
        versions, encryptions, version20 is True or version20 == "true"
    )


def admit_vtpm(
    settings: LparVtpmSettings,
    capabilities: VtpmCapabilities,
    before: LparVtpmSettings | None = None,
) -> None:
    """Validate supplied advertisement membership and known protocol compatibility."""
    version = settings.vtpm_version
    encryption = settings.vtpm_encryption
    if version is not None and version not in capabilities.versions:
        raise ValueError(
            f"vtpm_version is not advertised; supported versions: {capabilities.versions}"
        )
    if encryption is not None and encryption not in capabilities.encryptions:
        raise ValueError(
            f"vtpm_encryption is not advertised; supported levels: {capabilities.encryptions}"
        )
    effective_version = (
        version
        if version is not None
        else before.vtpm_version
        if before is not None
        else None
    )
    if effective_version is not None and effective_version not in {"1.2", "2.0"}:
        raise ValueError(
            "The advertised vTPM version has no supported compatibility contract; nothing was changed"
        )
    if effective_version == "2.0" and not capabilities.version20_capable:
        raise ValueError(
            "Selected system does not positively advertise vTPM 2.0 capability"
        )
    if encryption is not None and effective_version != "2.0":
        raise ValueError(
            "vtpm_encryption requires effective vtpm_version 2.0; supply an explicit compatible version during creation"
        )


async def read_lpar_vtpm(
    hmc: HMCClient, name: str, *, system_name_or_uuid: str, vios: bool = False
) -> VtpmState:
    """Read a contained AIX/Linux partition or VIOS without mutation authorization."""
    if type(vios) is not bool:
        raise TypeError("vios must be a boolean")
    if vios:
        system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
        target_uuid = await resolve_vios_uuid(
            hmc, name, system_name_or_uuid=system_uuid
        )
        entry = await hmc.get_vios(target_uuid)
        resource = (entry or {}).get("Resource")
        selected_name = (
            resource.get("PartitionName") if isinstance(resource, dict) else None
        )
        if not isinstance(selected_name, str) or not selected_name:
            raise ValueError("Contained VIOS has no readable partition name")
        system = await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    else:
        system, selected_name = await _resolve_contained_lpar_names(
            hmc, system_name_or_uuid, name
        )
    return await get_lpar_vtpm_state(hmc.config, system, selected_name, vios=vios)


def _require_inactive_transition(
    settings: LparVtpmSettings, before: VtpmState, acknowledge_data_loss: bool
) -> None:
    """Admit an explicit transition without replacing or resetting an enabled TPM."""
    if before.state != "Not Activated":
        raise ValueError(
            "vTPM updates require an inactive partition (Not Activated); nothing was changed"
        )
    if before.settings.vtpm_enabled is True:
        if any(
            getattr(settings, field) is not None
            and getattr(settings, field) != getattr(before.settings, field)
            for field in ("vtpm_version", "vtpm_encryption")
        ):
            raise ValueError(
                "Cannot replace vTPM version/encryption while enabled; explicitly disable and inspect first, then configure and enable separately"
            )
        if settings.vtpm_enabled is False and not acknowledge_data_loss:
            raise ValueError(
                "Disabling enabled vTPM can destroy keys and stored TPM data; back up/recover them first and explicitly set acknowledge_data_loss=True"
            )


async def configure_lpar_vtpm(
    hmc: HMCClient,
    name: str,
    settings: LparVtpmSettings,
    *,
    system_name_or_uuid: str,
    vios: bool = False,
    ownership_override: bool = False,
    acknowledge_data_loss: bool = False,
) -> VtpmResult:
    """Authorize an inactive target, submit once and compare requested/preserved fields."""
    if not isinstance(settings, LparVtpmSettings):
        raise TypeError("settings must be LparVtpmSettings")
    if any(
        type(value) is not bool
        for value in (vios, ownership_override, acknowledge_data_loss)
    ):
        raise TypeError(
            "vios, ownership_override and acknowledge_data_loss must be booleans"
        )
    if not settings.pairs():
        raise ValueError("Supply at least one vTPM setting")
    if vios:
        system, selected_name = await _resolve_and_authorize_vios_names(
            hmc,
            system_name_or_uuid,
            name,
            ownership_override=ownership_override,
        )
    else:
        system, selected_name = await resolve_and_authorize_lpar_names(
            hmc,
            name,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        )
    build_attribute_record([("name", selected_name), *settings.pairs()])
    capabilities = await read_vtpm_capabilities(hmc, system_name_or_uuid)
    before = await get_lpar_vtpm_state(hmc.config, system, selected_name, vios=vios)
    admit_vtpm(settings, capabilities, before.settings)
    _require_inactive_transition(settings, before, acknowledge_data_loss)
    try:
        await set_lpar_vtpm_settings(hmc.config, system, selected_name, settings)
    except HMCError as error:
        return VtpmResult(
            "write-outcome-unknown",
            settings,
            None,
            (
                f"Write did not confirm completion: {error}. Inspect before retrying; no replay performed.",
            ),
        )
    try:
        after = await get_lpar_vtpm_state(hmc.config, system, selected_name, vios=vios)
    except HMCError as error:
        return VtpmResult(
            "readback-unavailable",
            settings,
            None,
            (f"Write returned successfully but readback failed: {error}",),
        )
    expected = replace(
        before.settings,
        **{name: getattr(settings, name) for name, _ in settings.pairs()},
    )
    differences = tuple(
        item.name
        for item in fields(expected)
        if getattr(after.settings, item.name) != getattr(expected, item.name)
    )
    return VtpmResult(
        "mismatch" if differences else "verified",
        settings,
        after.settings,
        (
            f"Requested/preserved vTPM fields differ: {', '.join(differences)}; inspect before retrying",
        )
        if differences
        else (),
    )
