"""Selected-system evidence and admission for Linux management roles."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ...client.core import HMCClient
from ...documents.lpar import LparSettings
from ...errors import HMCError
from ...ssh.lpar import complete_create_resources
from ...ssh.profiles import get_proc_compat_modes
from ...xmlutil import leaf_text

if TYPE_CHECKING:
    from .core import LparCreation

ROLE_FIELDS = {
    "powervm_mgmt_capable": "PowerVMManagementCapable",
    "kvm_capable": "KvmCapable",
}


@dataclass(frozen=True, slots=True)
class RoleCapabilities:
    """Authority and independently observed KVM prerequisites."""

    controller_mode: bool
    kvm: bool
    hmc_version: tuple[int, int, int, int] | None
    firmware: tuple[int, int] | None
    power11: bool


def roles_requested(settings: LparSettings) -> bool:
    """Selection, including false, declares Linux intent."""
    return any(getattr(settings, name) is not None for name in ROLE_FIELDS)


def _role_console_version(resource: object) -> tuple[int, int, int, int] | None:
    if not isinstance(resource, dict):
        return None
    info = resource.get("VersionInfo")
    if not isinstance(info, dict):
        return None
    parts = [
        leaf_text(info.get(name))
        for name in ("Version", "Release", "ServicePackName", "Maintenance")
    ]
    if not all(
        isinstance(part, str) and re.fullmatch(r"[0-9]{1,4}", part) for part in parts
    ):
        return None
    version, release, service_pack, maintenance = (int(str(part)) for part in parts)
    return version, release, service_pack, maintenance


async def read_role_capabilities(
    hmc: HMCClient, system_uuid: str, *, need_kvm: bool
) -> RoleCapabilities:
    """Read authority first; management-only selection adds no KVM queries."""
    try:
        entry = await hmc.get_managed_system(system_uuid)
    except HMCError as error:
        raise type(error)(
            "Linux role preflight could not read selected-system authority; "
            "no changes were submitted. Check connectivity and read permissions, then retry.",
            status_code=error.status_code,
        ) from None
    resource = (entry or {}).get("Resource")
    if not isinstance(resource, dict):
        raise ValueError(  # noqa: TRY004 — malformed remote evidence, not caller input
            "Linux roles require readable selected-system authority"
        )
    authority = tuple(
        leaf_text(resource.get(name))
        for name in ("IsClassicHMCManagement", "IsHMCPowerVMManagementController")
    )
    if authority not in (("true", "false"), ("false", "true")):
        raise ValueError(
            "Linux roles require explicit classic or current-HMC controller authority; "
            "inspect selected-system management mode"
        )
    controller = authority == ("false", "true")
    if not need_kvm:
        return RoleCapabilities(controller, False, None, None, False)
    caps = resource.get("AssociatedSystemCapabilities")
    kvm = (
        isinstance(caps, dict) and leaf_text(caps.get("KvmOnPowerVMCapable")) == "true"
    )
    try:
        console = await hmc.get_console_info()
    except HMCError as error:
        raise type(error)(
            "Linux role preflight could not read console release evidence; "
            "no changes were submitted. Check connectivity and read permissions, then retry.",
            status_code=error.status_code,
        ) from None
    version = _role_console_version((console or {}).get("Resource"))
    firmware = leaf_text(resource.get("SystemFirmware"))
    match = (
        re.fullmatch(
            r"[A-Z]{2}[0-9]{3,4}_[Ff][Ww]([0-9]{3,4})\.([0-9]{1,2}) \([0-9]+\)",
            firmware,
        )
        if isinstance(firmware, str)
        else None
    )
    system = leaf_text(resource.get("SystemName"))
    modes = []
    if isinstance(system, str) and system.strip():
        try:
            modes = await get_proc_compat_modes(hmc.config, system)
        except HMCError:
            # Missing generation invalidates enablement evidence, not disabling.
            modes = []
    return RoleCapabilities(
        controller,
        kvm,
        version,
        (int(match[1]), int(match[2]))
        if match and ("POWER10" in modes or "POWER11" in modes)
        else None,
        "POWER11" in modes,
    )


def admit_role_update(
    settings: LparSettings, caps: RoleCapabilities, resource: dict[str, object]
) -> None:
    """Refuse unknown prerequisites without changing CPU, key or guest settings."""
    if not roles_requested(settings):
        return
    if leaf_text(resource.get("PartitionType")) != "AIX/Linux":
        raise ValueError(
            "Linux role field selection requires AIX/Linux and declares Linux intent"
        )
    if settings.kvm_capable is None:
        return
    if not caps.kvm or caps.hmc_version is None or caps.hmc_version < (10, 3, 1060, 1):
        raise ValueError(
            "KVM selection requires positive system capability and HMC >=10.3.1060.1"
        )
    if (
        leaf_text(resource.get("PartitionState")) != "not activated"
        or leaf_text(resource.get("IsOperationInProgress")) != "false"
    ):
        raise ValueError(
            "KVM selection requires not activated state and no operation in progress"
        )
    if settings.kvm_capable is False:
        return
    floor = (1110, 0) if caps.power11 else (1060, 10)
    if caps.firmware is None or caps.firmware < floor:
        raise ValueError(
            "KVM enablement requires supported Power10 FW1060.10 or Power11 FW1110"
        )
    processors = resource.get("PartitionProcessorConfiguration")
    if (
        leaf_text(resource.get("PendingProcessorCompatibilityMode")) != "POWER10"
        or not isinstance(processors, dict)
        or leaf_text(processors.get("HasDedicatedProcessors")) != "true"
        or leaf_text(processors.get("SharingMode")) != "keep idle procs"
    ):
        raise ValueError(
            "KVM enablement requires pending POWER10 and configured dedicated never-share CPUs"
        )
    # The policy owner imports configuration for its existing VIOS operation.
    from .security_policy import parse_security_policy

    if parse_security_policy(resource).settings.keystore_kbytes != 0:
        raise ValueError("KVM enablement requires known-zero platform keystore")
    memory = resource.get("AssociatedPersistentMemoryConfiguration")
    if (
        not isinstance(memory, dict)
        or leaf_text(memory.get("CurrentPersistentMemoryVolumes")) != "0"
    ):
        raise ValueError(
            "KVM enablement requires known-zero current persistent-memory volumes"
        )


def admit_role_creation(creation: LparCreation, caps: RoleCapabilities) -> None:
    """Admit one native creation record without substituting requested resources."""
    if not roles_requested(creation.settings):
        return
    if creation.partition_type != "AIX/Linux":
        raise ValueError(
            "Linux role field selection requires AIX/Linux and declares Linux intent"
        )
    if creation.keylock is not None:
        raise ValueError(
            "Linux roles cannot be combined with explicit keylock in native creation"
        )
    if caps.controller_mode:
        if creation.apply_profile is True:
            raise ValueError(
                "Controller creation requires apply_profile=False; use classic management mode for profile apply"
            )
        if creation.physical_io.requested or creation.creation_sriov:
            raise ValueError(
                "Initial-profile physical I/O and creation SR-IOV require classic management mode"
            )
    if creation.settings.kvm_capable is None:
        return
    if not caps.kvm or caps.hmc_version is None or caps.hmc_version < (10, 3, 1060, 1):
        raise ValueError(
            "KVM selection requires positive system capability and HMC >=10.3.1060.1"
        )
    if creation.settings.kvm_capable is False:
        return
    floor = (1110, 0) if caps.power11 else (1060, 10)
    if caps.firmware is None or caps.firmware < floor:
        raise ValueError(
            "KVM enablement requires supported Power10 FW1060.10 or Power11 FW1110"
        )
    resources = complete_create_resources(creation.resources)
    if (
        creation.resources.dedicated is not True
        or resources is None
        or resources.sharing_mode != "keep_idle_procs"
    ):
        raise ValueError(
            "KVM creation requires explicit dedicated never-share CPU resources; supply compatible resources"
        )
    if creation.security_policy.keystore_kbytes not in (None, 0):
        raise ValueError(
            "KVM creation requires zero platform keystore; remove the positive keystore request"
        )


def with_role_evidence(
    settings: LparSettings, resource: dict[str, object], requested: LparSettings
) -> LparSettings:
    """Merge readable roles, preserving unknown omissions and strict requested evidence."""
    values = {}
    for name, rest in ROLE_FIELDS.items():
        text = leaf_text(resource.get(rest))
        if text not in ("true", "false") and getattr(requested, name) is not None:
            raise ValueError(
                "Linux role readback requires readable requested or preserved role values"
            )
        values[name] = text == "true" if text in ("true", "false") else None
    return replace(settings, **values)
