"""Source-backed selected-system evidence and pure partition policy admission."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

from pydantic import TypeAdapter

from ...client.core import HMCClient
from ...documents.lpar_security_policy import (
    LparSecurityPolicy,
    SecurityPolicyResult,
    SecurityPolicyState,
)
from ...errors import HMCError
from ...resource_identity import (
    resolve_lpar_uuid,
    resolve_system_uuid,
    resolve_vios_uuid,
)
from ...ssh.commands import build_attribute_record
from ...ssh.lpar import set_lpar_security_policy
from ...ssh.profiles import get_proc_compat_modes
from ...xmlutil import console_version, leaf_text
from ..partition_state import PARTITION_STATES
from .configuration import _resolve_and_authorize_vios_names
from .ownership import resolve_and_authorize_lpar_names


@dataclass(frozen=True, slots=True)
class SecurityPolicyCapabilities:
    """Positive selected-system advertisements and independently observed releases."""

    secure_boot: bool
    keystore: bool
    dynamic_key: bool
    minimum_kbytes: int | None
    maximum_kbytes: int | None
    power11: bool
    hmc_version: tuple[int, int, int] | None
    firmware_level: int | None


def _integer(value: object, name: str) -> int:
    text = leaf_text(value)
    if not isinstance(text, str) or re.fullmatch(r"0|[1-9][0-9]*", text) is None:
        raise ValueError(
            f"Security policy read: malformed {name}; inspect target configuration"
        )
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"Security policy read: malformed {name}; inspect target configuration"
        ) from None


def parse_security_policy(resource: dict[str, object]) -> SecurityPolicyState:
    """Extract fixed REST leaves; absent policy remains unknown, never false."""
    environment = leaf_text(resource.get("PartitionType"))
    state = leaf_text(resource.get("PartitionState"))
    if not isinstance(environment, str) or environment not in (
        "AIX/Linux",
        "Virtual IO Server",
        "OS400",
    ):
        raise ValueError(
            "Security policy read requires a recognized partition environment"
        )
    if not isinstance(state, str) or state not in PARTITION_STATES:
        raise ValueError("Security policy read requires a recognized partition state")
    values: dict[str, object] = {}
    for native, rest in (
        ("secure_boot", "PendingSecureBoot"),
        ("keystore_kbytes", "KeyStoreSize"),
        ("keystore_signed_updates", "KeyStoreSignedUpdatesEnabled"),
        ("linux_dynamic_key_secure_boot", "LinuxDynamicKeySecureBootEnabled"),
        (
            "keystore_signed_updates_without_verification",
            "KeyStoreSignedUpdatesWithoutVerificationAllowed",
        ),
    ):
        if rest not in resource:
            continue
        if native in ("secure_boot", "keystore_kbytes"):
            values[native] = _integer(resource[rest], rest)
        else:
            text = leaf_text(resource[rest])
            if text not in ("true", "false"):
                raise ValueError(
                    f"Security policy read: malformed {rest}; inspect target configuration"
                )
            values[native] = text == "true"
    current = (
        _integer(resource["CurrentSecureBoot"], "CurrentSecureBoot")
        if "CurrentSecureBoot" in resource
        else None
    )
    if current is not None and current > 9:
        raise ValueError("Security policy read: CurrentSecureBoot must be 0 to 9")
    return SecurityPolicyState(
        environment,
        state,
        TypeAdapter(LparSecurityPolicy).validate_python(values),
        current,
    )


def _advertised_bound(resource: dict[str, object], name: str) -> int | None:
    try:
        return _integer(resource.get(name), name)
    except ValueError:
        return None


async def read_security_policy_capabilities(
    hmc: HMCClient, system_name_or_uuid: str, *, need_power11: bool = False
) -> SecurityPolicyCapabilities:
    """Use the selected system's associated flags; query modes only when needed."""
    if type(need_power11) is not bool:
        raise TypeError("need_power11 must be a boolean")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    entry = await hmc.get_managed_system(system_uuid)
    resource = (entry or {}).get("Resource")
    if not isinstance(resource, dict):
        raise ValueError(  # noqa: TRY004 — malformed remote evidence, not a caller type error
            "Security policy requires readable selected-system capability evidence"
        )
    caps = resource.get("AssociatedSystemCapabilities")
    if not isinstance(caps, dict):
        caps = {}
    console = await hmc.get_console_info()
    console_resource = (console or {}).get("Resource")
    version = (
        console_version(console_resource)
        if isinstance(console_resource, dict)
        else None
    )
    firmware = leaf_text(resource.get("SystemFirmware"))
    match = (
        re.fullmatch(
            r"[A-Z]{2}[0-9]{3,4}_[Ff][Ww]([0-9]{3,4})\.[A-Za-z0-9]+ \([0-9]+\)",
            firmware,
        )
        if isinstance(firmware, str)
        else None
    )
    power11 = False
    if need_power11:
        system = leaf_text(resource.get("SystemName"))
        if not isinstance(system, str) or not system.strip():
            raise ValueError(
                "Security policy processor-mode query requires selected-system name"
            )
        power11 = "POWER11" in await get_proc_compat_modes(hmc.config, system)
    return SecurityPolicyCapabilities(
        leaf_text(caps.get("PartitionSecureBootCapable")) == "true",
        leaf_text(caps.get("PartitionKeyStoreCapable")) == "true",
        leaf_text(caps.get("PartitionDynamicKeySecureBootCapable")) == "true",
        _advertised_bound(resource, "MinimumKeyStoreSize"),
        _advertised_bound(resource, "MaximumkeyStoreSize"),
        power11,
        version,
        int(match[1]) if match else None,
    )


def _require_release(
    capabilities: SecurityPolicyCapabilities, *, power11: bool
) -> None:
    version, firmware = capabilities.hmc_version, capabilities.firmware_level
    floor = (11, 1, 1110) if power11 else (9, 2, 950)
    if (
        (power11 and not capabilities.power11)
        or version is None
        or version < floor
        or firmware is None
        or firmware < floor[2]
    ):
        label = "Power11" if power11 else "PKS"
        raise ValueError(
            f"{label} release evidence is missing or below the required HMC/firmware floor"
        )


def admit_security_policy(
    settings: LparSecurityPolicy,
    capabilities: SecurityPolicyCapabilities,
    *,
    environment: str,
    before: SecurityPolicyState | None = None,
) -> None:
    """Refuse unsupported, irreversible or state-incompatible supplied changes."""
    if not settings.pairs():
        return
    if before is None:
        settings.validate_creation(environment)
    if environment not in ("AIX/Linux", "Virtual IO Server", "OS400") or (
        before is not None and before.environment != environment
    ):
        raise ValueError(
            "Security policy requires a matching recognized partition environment"
        )
    if (
        settings.linux_dynamic_key_secure_boot is not None
        and environment != "AIX/Linux"
    ):
        raise ValueError(
            "linux_dynamic_key_secure_boot requires AIX/Linux and declares Linux intent only"
        )
    if settings.secure_boot is not None:
        if not capabilities.secure_boot:
            raise ValueError("Selected system lacks positive secure-boot capability")
        if environment == "OS400":
            _require_release(capabilities, power11=True)
    if settings.keystore_kbytes is not None or any(
        getattr(settings, name) is not None
        for name in (
            "keystore_signed_updates",
            "linux_dynamic_key_secure_boot",
            "keystore_signed_updates_without_verification",
        )
    ):
        if not capabilities.keystore:
            raise ValueError("Selected system lacks positive keystore capability")
        _require_release(capabilities, power11=False)
    if (
        any(
            getattr(settings, name) is not None
            for name in (
                "keystore_signed_updates",
                "linux_dynamic_key_secure_boot",
                "keystore_signed_updates_without_verification",
            )
        )
        and not capabilities.dynamic_key
    ):
        raise ValueError("Selected system lacks positive dynamic-key capability")
    old = before.settings if before is not None else LparSecurityPolicy()
    store = (
        settings.keystore_kbytes
        if settings.keystore_kbytes is not None
        else old.keystore_kbytes
    )
    signed = (
        settings.keystore_signed_updates
        if settings.keystore_signed_updates is not None
        else old.keystore_signed_updates
    )
    inactive_needed = False
    if (size := settings.keystore_kbytes) is not None:
        if size > 0:
            minimum, maximum = capabilities.minimum_kbytes, capabilities.maximum_kbytes
            if minimum is None or maximum is None or minimum <= 0 or maximum < minimum:
                raise ValueError("Selected system lacks usable keystore bounds")
            if not minimum <= size <= maximum:
                raise ValueError("keystore_kbytes is outside selected-system bounds")
        if before is not None:
            if old.keystore_kbytes is None and size > 0:
                raise ValueError(
                    "Keystore transition requires known current size; inspect before configuring"
                )
            if old.keystore_kbytes is not None:
                inactive_needed = size > old.keystore_kbytes
                if 0 < size < old.keystore_kbytes:
                    _require_release(capabilities, power11=True)
    for name in ("keystore_signed_updates", "linux_dynamic_key_secure_boot"):
        value, previous = getattr(settings, name), getattr(old, name)
        if before is not None and value is False:
            if previous is True:
                raise ValueError(
                    f"{name} enablement is irreversible; disabling is refused"
                )
            if previous is None:
                raise ValueError(
                    f"{name} current value is unknown; inspect before configuring False"
                )
        if value is True:
            if store is None or store <= 0:
                raise ValueError(
                    f"{name} requires effective nonzero keystore_kbytes; configure explicitly"
                )
            inactive_needed |= previous is not True
    if (
        settings.linux_dynamic_key_secure_boot is True
        or settings.keystore_signed_updates_without_verification is True
    ) and (signed is not True or store is None or store <= 0):
        raise ValueError(
            "Linux enablement or PK bypass requires effective signed infrastructure and nonzero store; configure explicitly"
        )
    if before is not None:
        if before.state not in ("running", "not activated"):
            raise ValueError(
                "Security policy requires a stable running or not activated partition"
            )
        if inactive_needed and before.state != "not activated":
            raise ValueError(
                "Store enable/increase and signed/Linux enablement require not activated state"
            )


def _selection(vios: bool, ownership_override: bool = False) -> None:
    if type(vios) is not bool or type(ownership_override) is not bool:
        raise TypeError("vios and ownership_override must be booleans")


async def _policy_snapshot(
    hmc: HMCClient, uuid: str, *, vios: bool, expected_name: str | None = None
) -> tuple[str, SecurityPolicyState]:
    entry = (
        await hmc.get_uom("VirtualIOServer", uuid)
        if vios
        else await hmc.get_logical_partition(uuid)
    )
    resource = (entry or {}).get("Resource")
    if (
        not isinstance(resource, dict)
        or str((entry or {}).get("UUID", "")).casefold() != uuid.casefold()
    ):
        raise ValueError(
            "Security policy requires matching immutable partition identity"
        )
    name = leaf_text(resource.get("PartitionName"))
    if (
        not isinstance(name, str)
        or not name.strip()
        or (expected_name is not None and name != expected_name)
    ):
        raise ValueError("Security policy requires matching partition name")
    state = parse_security_policy(resource)
    if (state.environment == "Virtual IO Server") != vios:
        raise ValueError("Security policy requires matching LPAR or VIOS target kind")
    return name, state


async def read_lpar_security_policy(
    hmc: HMCClient, name: str, *, system_name_or_uuid: str, vios: bool = False
) -> SecurityPolicyState:
    """Read strict REST policy only after selected-system containment proof."""
    _selection(vios)
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    resolver = resolve_vios_uuid if vios else resolve_lpar_uuid
    uuid = await resolver(hmc, name, system_name_or_uuid=system_uuid)
    _, state = await _policy_snapshot(hmc, uuid, vios=vios)
    return state


def _policy_result(
    settings: LparSecurityPolicy,
    before: SecurityPolicyState,
    after: SecurityPolicyState,
    changed: tuple[str, ...],
) -> SecurityPolicyResult:
    mismatches = []
    warnings = []
    for item in fields(settings):
        name = item.name
        requested, previous = getattr(settings, name), getattr(before.settings, name)
        expected = requested if requested is not None else previous
        if expected is not None and getattr(after.settings, name) != expected:
            mismatches.append(name)
        if expected is None:
            warnings.append(f"Preserved {name} was unknown before submission.")
    if before.current_secure_boot is not None:
        if after.current_secure_boot != before.current_secure_boot:
            mismatches.append("current_secure_boot")
    else:
        warnings.append("Preserved current_secure_boot was unknown before submission.")
    if after.environment != before.environment:
        mismatches.append("environment")
    if (
        after.settings.secure_boot is not None
        and after.current_secure_boot is not None
        and after.settings.secure_boot != after.current_secure_boot
    ):
        warnings.append(
            "Pending secure boot differs from current; restart required. Runtime enforcement is unverified."
        )
    if settings.secure_boot is not None and settings.secure_boot >= 3:
        warnings.append(
            "Secure-boot values 3–9 require OS-specific operator knowledge; guest semantics are unverified."
        )
    if before.environment == "OS400" and settings.secure_boot is not None:
        warnings.append(
            "Installed IBM i 7.6 is an operator prerequisite and is unverified."
        )
    if settings.linux_dynamic_key_secure_boot is not None:
        warnings.append(
            "Linux intent is declared; guest OS and key readiness are unverified."
        )
    if mismatches:
        warnings.append(
            "Readback differs for "
            + ", ".join(mismatches)
            + "; inspect before retrying."
        )
    return SecurityPolicyResult(
        "mismatch" if mismatches else "verified",
        settings,
        after,
        changed,
        tuple(warnings),
    )


async def configure_lpar_security_policy(
    hmc: HMCClient,
    name: str,
    settings: LparSecurityPolicy,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
    vios: bool = False,
) -> SecurityPolicyResult:
    """Authorize, admit, submit once and compare pending and preserved evidence."""
    _selection(vios, ownership_override)
    if not settings.pairs():
        raise ValueError("Supply at least one security policy setting")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    resolver = resolve_vios_uuid if vios else resolve_lpar_uuid
    uuid = await resolver(hmc, name, system_name_or_uuid=system_uuid)
    if vios:
        system, partition = await _resolve_and_authorize_vios_names(
            hmc,
            system_uuid,
            uuid,
            ownership_override=ownership_override,
        )
    else:
        system, partition = await resolve_and_authorize_lpar_names(
            hmc,
            uuid,
            system_name_or_uuid=system_uuid,
            ownership_override=ownership_override,
        )
    # Refresh containment after the authorization helper's independent lookup.
    if await resolver(hmc, partition, system_name_or_uuid=system_uuid) != uuid:
        raise ValueError(
            "Security policy partition identity changed during authorization"
        )
    _, before = await _policy_snapshot(hmc, uuid, vios=vios, expected_name=partition)
    system_entry = await hmc.get_managed_system(system_uuid)
    if (
        leaf_text(((system_entry or {}).get("Resource") or {}).get("SystemName"))
        != system
    ):
        raise ValueError(
            "Security policy managed-system name changed during authorization"
        )
    need_power11 = (
        before.environment == "OS400" and settings.secure_boot is not None
    ) or (
        settings.keystore_kbytes is not None
        and before.settings.keystore_kbytes is not None
        and 0 < settings.keystore_kbytes < before.settings.keystore_kbytes
    )
    capabilities = await read_security_policy_capabilities(
        hmc, system_uuid, need_power11=need_power11
    )
    admit_security_policy(
        settings, capabilities, environment=before.environment, before=before
    )
    build_attribute_record([("name", partition), *settings.pairs()])
    changed = tuple(
        field
        for field, _ in settings.pairs()
        if getattr(settings, field) != getattr(before.settings, field)
    )
    try:
        await set_lpar_security_policy(hmc.config, system, partition, settings)
    except HMCError:
        return SecurityPolicyResult(
            "write-outcome-unknown",
            settings,
            None,
            changed,
            (
                "Submission failed (HMCError); inspect before retrying. No replay attempted.",
            ),
        )
    try:
        _, after = await _policy_snapshot(hmc, uuid, vios=vios, expected_name=partition)
    except (HMCError, ValueError, TypeError):
        return SecurityPolicyResult(
            "readback-unavailable",
            settings,
            None,
            changed,
            (
                "Readback failed (policy evidence unavailable); inspect before retrying.",
            ),
        )
    return _policy_result(settings, before, after, changed)
